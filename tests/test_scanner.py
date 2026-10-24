"""End-to-end scan against a fake Gmail: files on disk, index rows, de-duplication, read-only access."""

import base64

import pytest

from kabala.detect.pipeline import Detector
from kabala.files import email_to_pdf, write_unique
from kabala.gmail import GmailClient, GmailError
from kabala.models import ScanOptions
from kabala.scanner import Scanner
from kabala.store import Store

PDF = b"%PDF-1.4\n% fake receipt\n%%EOF\n"
ACCOUNT = "me@gmail.com"


def pdf_for(gid: str) -> bytes:
    return PDF if gid in ("a2", "a4") else PDF + gid.encode()


def b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def message(gid, sender, subject, html=None, text=None, pdf_name=None, msg_id=None, day_ms="1792000000000"):
    parts = []
    if html:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html)}})
    if text:
        parts.append({"mimeType": "text/plain", "body": {"data": b64(text)}})
    if pdf_name:
        parts.append({"mimeType": "application/pdf", "filename": pdf_name, "body": {"attachmentId": f"att-{gid}", "size": len(PDF)}})
    return {
        "id": gid, "threadId": gid, "internalDate": day_ms,
        "payload": {"mimeType": "multipart/mixed", "headers": [
            {"name": "From", "value": sender}, {"name": "Subject", "value": subject},
            {"name": "Message-ID", "value": msg_id or f"<{gid}@mail>"}], "parts": parts},
    }


MAILBOX = {
    "a1": message("a1", "Netflix <info@account.netflix.com>", "הקבלה שלך מ-Netflix",
                  html="<p>תודה!</p><table><tr><td>סה\"כ</td><td>₪54.90</td></tr></table>"),
    "a2": message("a2", "בזק <invoice@bezeq.co.il>", "חשבונית מס מבזק", text="חשבונית מס מספר 4455667\nסה״כ לתשלום 129.00 ₪",
                  pdf_name="bezeq_invoice.pdf"),
    "a3": message("a3", "Wolt <promo@wolt.com>", "20% הנחה על ההזמנה הבאה", text="קופון WOLT20, המבצע השבוע בלבד"),
    # A forwarded copy of the Bezeq invoice: same PDF bytes, different message → must not be saved twice.
    "a4": message("a4", "Me <me@gmail.com>", "Fwd: חשבונית מס מבזק", text="חשבונית מס מספר 4455667\nסה״כ לתשלום 129.00 ₪",
                  pdf_name="bezeq_invoice.pdf"),
    # Receipt words but no amount → manual review.
    "a5": message("a5", "Shop <billing@shop.example.com>", "Your receipt", text="Thanks for your order. Receipt attached.",
                  pdf_name="receipt.pdf"),
}


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    """Implements only GET. Any attempt to modify the mailbox would raise AttributeError."""

    def __init__(self, mailbox, fail_first=0):
        self.mailbox = mailbox
        self.calls = []
        self.fail_first = fail_first

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if self.fail_first:
            self.fail_first -= 1
            return FakeResponse(429, headers={"Retry-After": "0"})
        path = url.split("/users/me/", 1)[1]
        if path == "messages":
            return FakeResponse(200, {"messages": [{"id": i} for i in self.mailbox]})
        if "/attachments/" in path:
            gid = path.split("/")[1]
            # a2 and its forwarded copy a4 carry the identical invoice; every other PDF is unique
            return FakeResponse(200, {"data": b64(pdf_for(gid))})
        return FakeResponse(200, self.mailbox[path.split("/")[1]])


@pytest.fixture
def env(tmp_path, qapp):
    store = Store(tmp_path / "index.db")
    session = FakeSession(dict(MAILBOX))
    scanner = Scanner(lambda: GmailClient(session, sleep=lambda s: None), store, Detector(None, 0.75), ACCOUNT, workers=2)
    options = ScanOptions(folder=str(tmp_path / "Receipts"), layout="company-first", threshold=0.75)
    yield scanner, store, session, options, tmp_path / "Receipts"
    store.close()


def run(scanner, options):
    events = []
    summary = scanner.run(options, events.append, lambda: False)
    return summary, events


def test_full_scan_saves_files_and_index(env):
    scanner, store, session, options, root = env
    summary, events = run(scanner, options)

    assert summary.receipts == 2 and summary.companies == 2
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.pdf"))
    assert len(files) == 2
    assert any(f.startswith("Netflix/") and f.endswith("_Netflix_54.90ILS.pdf") for f in files)
    assert any(f.startswith("בזק/") and f.endswith("_בזק_129.00ILS.pdf") for f in files)
    # the Bezeq file is the original attachment; the Netflix one was rendered from the HTML body
    bezeq = next(root.rglob("*בזק*.pdf"))
    assert bezeq.read_bytes() == PDF
    assert next(root.rglob("*Netflix*.pdf")).read_bytes().startswith(b"%PDF")

    assert sorted(r.company for r in store.receipts(ACCOUNT)) == sorted(["Netflix", "בזק"])
    assert all(r.file_path and r.amount > 0 for r in store.receipts(ACCOUNT))
    assert [i.reason for i in store.review_items(ACCOUNT)] == ["no_amount"]
    assert events[-1].phase == "done" and events[-1].processed == 5
    assert all(c.startswith("https://gmail.googleapis.com/gmail/v1/users/me/") for c in session.calls)


def test_rescan_is_incremental_and_creates_no_duplicates(env):
    scanner, store, session, options, root = env
    run(scanner, options)
    session.calls.clear()
    summary, events = run(scanner, options)
    assert summary.receipts == 0
    assert len(list(root.rglob("*.pdf"))) == 2
    assert not any("/messages/a" in c for c in session.calls)  # nothing re-downloaded


def test_rate_limit_is_retried(tmp_path, qapp):
    session = FakeSession(dict(MAILBOX), fail_first=2)
    client = GmailClient(session, sleep=lambda s: None)
    assert list(client.search("q")) == list(MAILBOX)


def test_auth_error_stops_the_scan(tmp_path, qapp):
    class Expired(FakeSession):
        def get(self, url, params=None, timeout=None):
            return FakeResponse(401)

    store = Store(tmp_path / "i.db")
    scanner = Scanner(lambda: GmailClient(Expired({})), store, Detector(None), ACCOUNT)
    with pytest.raises(GmailError) as e:
        scanner.run(ScanOptions(str(tmp_path), "company-first", 0.75), lambda p: None, lambda: False)
    assert e.value.code == "auth"
    store.close()


def test_approve_from_review_files_the_receipt(env):
    scanner, store, session, options, root = env
    run(scanner, options)
    item = store.review_items(ACCOUNT)[0]
    item.amount = 80.0
    receipt = scanner.approve(item, "company-first", root)
    assert receipt and receipt.file_path.endswith("_80.00ILS.pdf")
    assert store.review_items(ACCOUNT) == []


def test_free_plan_quota_stops_saving_and_upgrade_picks_up_the_rest(env):
    scanner, store, session, options, root = env
    scanner.quota = 1
    summary, _ = run(scanner, options)
    assert summary.receipts == 1 and summary.limited
    assert store.get_meta(ACCOUNT, "last_scan") is None  # search window not advanced
    assert len(store.receipts(ACCOUNT)) == 1

    # after upgrading (no quota) the next scan files the receipt that was held back
    scanner.quota, scanner.limited = None, False
    summary, _ = run(scanner, options)
    assert summary.receipts == 1 and not summary.limited
    assert len(store.receipts(ACCOUNT)) == 2
    assert store.saved_this_month(ACCOUNT) == 2


def test_write_unique_handles_name_clash_and_identical_file(tmp_path):
    segs = ["Netflix", "2026", "2026-10", "2026-10-14_Netflix_54.90ILS.pdf"]
    a = write_unique(tmp_path, segs, b"%PDF one")
    assert write_unique(tmp_path, segs, b"%PDF one") == a  # identical → same file
    b = write_unique(tmp_path, segs, b"%PDF two")  # different content, same name → _2
    assert b.name == "2026-10-14_Netflix_54.90ILS_2.pdf"
    assert not list(tmp_path.rglob("*.tmp"))


def test_email_to_pdf_renders_hebrew(qapp):
    from kabala.detect.message import ParsedEmail

    email = ParsedEmail("x", "x", "<x>", "קבלה", "Wolt", "info@wolt.com", "2026-10-14",
                        'סה"כ ₪142.50', '<p>סה"כ <b>₪142.50</b></p><img src="https://tracker.example/pixel.gif">')
    data = email_to_pdf(email)
    assert data.startswith(b"%PDF") and len(data) > 1000
    assert b"tracker.example" not in data
