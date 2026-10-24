"""
Read-only Gmail access over IMAP with a Google *app password*: the alternative to the OAuth sign-in
for users who don't want to pass Google's "unverified app" screen (that screen exists only because
gmail.readonly is a restricted scope; IMAP with an app password never shows it).

Same interface as kabala.gmail.GmailClient, so the scanner doesn't care which one it gets:
* search uses Gmail's own search syntax through the X-GM-RAW extension, so build_query() works as is,
* message ids are Gmail's X-GM-MSGID in hex, which is exactly the Gmail API id: switching between
  the two sign-in methods never rescans or duplicates receipts,
* messages are returned in the Gmail API "format=full" shape, so parse_gmail_message() works as is.

Read-only by construction: the mailbox is opened with EXAMINE (readonly=True) and bodies are fetched
with BODY.PEEK, so nothing is marked as read, moved or deleted.
The app password lives only in the OS credential vault (Windows Credential Manager / macOS Keychain).
"""

from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import json
import re
import time
from email.message import Message
from typing import Callable, Iterator

import keyring
import keyring.errors

from kabala.gmail import GmailError

IMAP_HOST = "imap.gmail.com"
KEYRING_SERVICE = "Kabala"
KEYRING_USER = "imap"
APP_PASSWORDS_URL = "https://myaccount.google.com/apppasswords"
FETCH_BATCH = 500

_LIST_NAME = re.compile(rb'\) "[^"]*" (.+)$')
_MSGID = re.compile(rb"X-GM-MSGID (\d+)")
_THRID = re.compile(rb"X-GM-THRID (\d+)")
_UID = re.compile(rb"UID (\d+)")


def normalize_app_password(password: str) -> str:
    """Google shows app passwords as 'abcd efgh ijkl mnop'; the spaces are not part of it."""
    return re.sub(r"\s+", "", password)


def _error_code(e: Exception) -> str:
    text = str(e).lower()
    if "application-specific password" in text or "web login required" in text:
        return "app_password"  # the normal Google password was used instead of an app password
    if "authenticationfailed" in text or "invalid credentials" in text:
        return "auth"
    return "network"


class ImapGmailClient:
    def __init__(self, address: str, password: str, connect: Callable[[], imaplib.IMAP4] | None = None) -> None:
        self.address = address
        try:
            self._imap = (connect or (lambda: imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=30)))()
            self._imap.login(address, normalize_app_password(password))
            typ, _ = self._imap.select(self._all_mail(), readonly=True)
        except imaplib.IMAP4.error as e:
            raise GmailError(_error_code(e)) from e
        except OSError as e:
            raise GmailError("network") from e
        if typ != "OK":
            raise GmailError("not_found")
        self._uids: dict[str, bytes] = {}

    def close(self) -> None:
        try:
            self._imap.logout()
        except Exception:  # noqa: BLE001 - already gone
            pass

    def _all_mail(self) -> str:
        """'[Gmail]/All Mail' has a localized name in some languages; find it by its \\All flag."""
        typ, lines = self._imap.list()
        for line in lines if typ == "OK" else []:
            if isinstance(line, bytes) and b"\\All" in line and (m := _LIST_NAME.search(line)):
                name = m.group(1).decode("ascii", "replace")
                return name if name.startswith('"') else f'"{name}"'
        return '"[Gmail]/All Mail"'

    def _cmd(self, *args: str) -> list:
        try:
            typ, data = self._imap.uid(*args)
        except (imaplib.IMAP4.error, OSError) as e:
            raise GmailError(_error_code(e)) from e
        if typ != "OK":
            raise GmailError("http")
        return data

    # ---- GmailClient interface ---------------------------------------------------

    def profile(self) -> dict:
        return {"emailAddress": self.address}

    def _search_uids(self, query: str) -> list[bytes]:
        # Hebrew keywords: the query goes as a UTF-8 literal, which Gmail's X-GM-RAW accepts.
        self._imap.literal = query.encode("utf-8")
        data = self._cmd("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
        return data[0].split() if data and data[0] else []

    def search(self, query: str, limit: int | None = None, page_size: int = 100) -> Iterator[str]:
        """Yields Gmail message ids (hex X-GM-MSGID) matching a Gmail search query, newest first."""
        uids = self._search_uids(query)[::-1]  # IMAP returns oldest first
        if limit:
            uids = uids[:limit]
        for start in range(0, len(uids), FETCH_BATCH):
            batch = uids[start:start + FETCH_BATCH]
            ids: dict[bytes, str] = {}
            for item in self._cmd("FETCH", b",".join(batch).decode(), "(X-GM-MSGID)"):
                line = item[0] if isinstance(item, tuple) else item
                if isinstance(line, bytes) and (mid := _MSGID.search(line)) and (uid := _UID.search(line)):
                    ids[uid.group(1)] = format(int(mid.group(1)), "x")
            for uid in batch:  # keep newest-first order
                if gid := ids.get(uid):
                    self._uids[gid] = uid
                    yield gid

    def estimate(self, query: str) -> int:
        return len(self._search_uids(query))

    def _uid_for(self, message_id: str) -> bytes:
        if uid := self._uids.get(message_id):
            return uid
        data = self._cmd("SEARCH", "X-GM-MSGID", str(int(message_id, 16)))
        found = data[0].split() if data and data[0] else []
        if not found:
            raise GmailError("not_found", 404)
        self._uids[message_id] = found[0]
        return found[0]

    def message(self, message_id: str) -> dict:
        uid = self._uid_for(message_id)
        for item in self._cmd("FETCH", uid.decode(), "(X-GM-THRID INTERNALDATE BODY.PEEK[])"):
            if isinstance(item, tuple) and len(item) == 2:
                meta, raw = item
                stamp = imaplib.Internaldate2tuple(meta)
                thread = _THRID.search(meta)
                return to_api_message(
                    raw,
                    message_id,
                    thread_id=format(int(thread.group(1)), "x") if thread else message_id,
                    internal_ms=int(time.mktime(stamp) * 1000) if stamp else 0,
                )
        raise GmailError("not_found", 404)

    def attachment(self, message_id: str, attachment_id: str) -> bytes:
        # Never needed: to_api_message() puts every attachment's bytes inline.
        raise GmailError("not_found", 404)


# ---- RFC 822 → Gmail API "format=full" shape --------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _part(msg: Message) -> dict:
    headers = [{"name": k, "value": str(v)} for k, v in msg.items()]
    part: dict = {"mimeType": msg.get_content_type(), "filename": msg.get_filename() or "", "headers": headers}
    if msg.is_multipart():
        part["body"] = {"size": 0}
        part["parts"] = [_part(p) for p in msg.get_payload()]  # type: ignore[union-attr]
    else:
        data = msg.get_payload(decode=True) or b""
        part["body"] = {"size": len(data), "data": _b64(data)}
    return part


def to_api_message(raw: bytes, message_id: str, thread_id: str = "", internal_ms: int = 0) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return {
        "id": message_id,
        "threadId": thread_id or message_id,
        "internalDate": str(internal_ms),
        "labelIds": [],
        "snippet": "",
        "payload": _part(msg),
    }


# ---- Stored app password --------------------------------------------------------------


def save_login(address: str, password: str) -> None:
    blob = json.dumps({"email": address, "password": normalize_app_password(password)})
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, blob)
    except keyring.errors.KeyringError as e:
        raise GmailError("keyring") from e


def load_login() -> tuple[str, str] | None:
    try:
        raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        data = json.loads(raw) if raw else None
    except (keyring.errors.KeyringError, ValueError):
        return None
    return (data["email"], data["password"]) if data else None


def forget_login() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except keyring.errors.KeyringError:
        pass


__all__ = ["ImapGmailClient", "to_api_message", "save_login", "load_login", "forget_login", "APP_PASSWORDS_URL"]
