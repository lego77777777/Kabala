"""Turns a Gmail API message (format=full) into plain fields: headers, readable text, attachments."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
from html import unescape
from html.parser import HTMLParser


@dataclass
class Attachment:
    filename: str
    mime_type: str
    attachment_id: str | None
    size: int
    data: bytes | None = None  # small attachments arrive inline


@dataclass
class ParsedEmail:
    id: str
    thread_id: str
    message_id: str  # RFC 822 Message-ID, used for de-duplication
    subject: str
    sender_name: str
    sender_email: str
    date: str  # YYYY-MM-DD (local time of the mailbox)
    text: str
    html: str | None
    attachments: list[Attachment] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    @property
    def sender_domain(self) -> str:
        return self.sender_email.rsplit("@", 1)[-1].lower() if "@" in self.sender_email else ""

    @property
    def pdfs(self) -> list[Attachment]:
        return [a for a in self.attachments if a.mime_type == "application/pdf" or a.filename.lower().endswith(".pdf")]


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class _TextExtractor(HTMLParser):
    """HTML → readable text, keeping table rows and paragraphs on their own lines."""

    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "head", "title"):
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append("  ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head", "title"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # noqa: BLE001 - malformed HTML: fall back to tag stripping
        text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _charset(part: dict) -> str:
    for h in part.get("headers", []):
        if h["name"].lower() == "content-type":
            if m := re.search(r'charset="?([\w-]+)"?', h["value"], re.IGNORECASE):
                return m.group(1)
    return "utf-8"


def _decode(part: dict) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = b64url_decode(data)
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _walk(part: dict, plain: list[str], html: list[str], attachments: list[Attachment]) -> None:
    mime = part.get("mimeType", "")
    filename = part.get("filename") or ""
    body = part.get("body", {})
    if filename or body.get("attachmentId"):
        inline = b64url_decode(body["data"]) if body.get("data") else None
        attachments.append(Attachment(filename, mime, body.get("attachmentId"), int(body.get("size", 0)), inline))
    elif mime == "text/plain":
        plain.append(_decode(part))
    elif mime == "text/html":
        html.append(_decode(part))
    for child in part.get("parts", []) or []:
        _walk(child, plain, html, attachments)


def parse_gmail_message(msg: dict) -> ParsedEmail:
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    plain: list[str] = []
    html: list[str] = []
    attachments: list[Attachment] = []
    _walk(payload, plain, html, attachments)

    html_doc = "\n".join(html) or None
    # Prefer the HTML rendering for text: many receipts put the totals table only in HTML.
    text = html_to_text(html_doc) if html_doc else "\n".join(plain).strip()
    if not text:
        text = unescape(msg.get("snippet", ""))

    name, email = parseaddr(headers.get("from", ""))
    millis = int(msg.get("internalDate", "0") or 0)
    day = datetime.fromtimestamp(millis / 1000, tz=timezone.utc).astimezone().date() if millis else None

    return ParsedEmail(
        id=msg.get("id", ""),
        thread_id=msg.get("threadId", ""),
        message_id=headers.get("message-id", f"<{msg.get('id', '')}@gmail>").strip(),
        subject=headers.get("subject", "").strip(),
        sender_name=name.strip().strip('"'),
        sender_email=email.lower(),
        date=day.isoformat() if day else "",
        text=text,
        html=html_doc,
        attachments=attachments,
        labels=list(msg.get("labelIds", [])),
    )
