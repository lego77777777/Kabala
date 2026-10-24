"""
Minimal read-only Gmail API client (GET requests only) with paging and polite retries.

Gmail allows ~250 quota units per user per second; messages.get costs 5, so we stay well under it
and back off on 429 / 5xx using Retry-After when Google sends one.
"""

from __future__ import annotations

import random
import time
from typing import Callable, Iterator

from kabala.detect.message import b64url_decode

API = "https://gmail.googleapis.com/gmail/v1/users/me"
RETRY_STATUS = {429, 500, 502, 503, 504}


class GmailError(Exception):
    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class GmailClient:
    def __init__(self, session, max_retries: int = 5, sleep: Callable[[float], None] = time.sleep) -> None:
        """`session` is a google.auth.transport.requests.AuthorizedSession (refreshes tokens itself)."""
        self.session = session
        self.max_retries = max_retries
        self.sleep = sleep

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.get(f"{API}/{path}", params=params, timeout=30)
            except Exception as e:  # noqa: BLE001 - connection reset, DNS, timeout
                if attempt == self.max_retries:
                    raise GmailError("network") from e
                self.sleep(min(30.0, 2**attempt + random.random()))
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt + random.random()
                self.sleep(min(60.0, delay))
                continue
            if resp.status_code == 401:
                raise GmailError("auth", 401)
            if resp.status_code == 403:
                raise GmailError("forbidden", 403)
            if resp.status_code == 404:
                raise GmailError("not_found", 404)
            raise GmailError("http", resp.status_code)
        raise GmailError("retries_exhausted")

    def profile(self) -> dict:
        return self._get("profile")

    def search(self, query: str, limit: int | None = None, page_size: int = 100) -> Iterator[str]:
        """Yields message ids matching a Gmail search query, newest first."""
        token: str | None = None
        count = 0
        while True:
            params = {"q": query, "maxResults": page_size}
            if token:
                params["pageToken"] = token
            data = self._get("messages", params)
            for m in data.get("messages", []):
                yield m["id"]
                count += 1
                if limit and count >= limit:
                    return
            token = data.get("nextPageToken")
            if not token:
                return

    def estimate(self, query: str) -> int:
        return int(self._get("messages", {"q": query, "maxResults": 1}).get("resultSizeEstimate", 0))

    def message(self, message_id: str) -> dict:
        return self._get(f"messages/{message_id}", {"format": "full"})

    def attachment(self, message_id: str, attachment_id: str) -> bytes:
        data = self._get(f"messages/{message_id}/attachments/{attachment_id}")
        return b64url_decode(data["data"])
