import imaplib
import threading
from email.message import EmailMessage

import pytest

from kabala import google_auth, imap_gmail
from kabala.detect.message import parse_gmail_message
from kabala.detect.query import build_query
from kabala.gmail import GmailError
from kabala.imap_gmail import ImapGmailClient, normalize_app_password

PASSWORD = "abcdefghijklmnop"
ALL_MAIL = b'"[Gmail]/&BdsF3A- &BdQF0wXVBegF3A-"'  # Hebrew UI: the folder name is localized


def receipt_bytes() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Wolt <info@wolt.com>"
    msg["To"] = "dana@gmail.com"
    msg["Subject"] = "קבלה על ההזמנה שלך"
    msg["Message-ID"] = "<abc123@wolt.com>"
    msg.set_content("סה\"כ לתשלום 89.90 ₪")
    msg.add_alternative("<html><body><table><tr><td>סה\"כ</td><td>89.90 ₪</td></tr></table></body></html>", subtype="html")
    msg.add_attachment(b"%PDF-1.7 receipt", maintype="application", subtype="pdf", filename="קבלה-123.pdf")
    return msg.as_bytes()


class FakeIMAP:
    """Gmail's IMAP as far as Kabala uses it. Any command that could change the mailbox fails the test."""

    MSGIDS = {b"1": 1001, b"2": 1002, b"3": 1003}

    def __init__(self):
        self.literal = None
        self.queries: list[str] = []
        self.selected = None

    def login(self, user, password):
        if "@" not in user:
            raise imaplib.IMAP4.error(b"[ALERT] Application-specific password required")
        if password != PASSWORD:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        return "OK", [b"ok"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b"(\\All \\HasNoChildren) \"/\" " + ALL_MAIL]

    def select(self, name, readonly=False):
        assert readonly, "the mailbox must be opened read-only (EXAMINE)"
        self.selected = name
        return "OK", [b"3"]

    def logout(self):
        return "BYE", []

    def uid(self, command, *args):
        assert command in ("SEARCH", "FETCH"), f"{command} could change the mailbox"
        if command == "SEARCH" and "X-GM-RAW" in args:
            self.queries.append(self.literal.decode("utf-8"))
            self.literal = None
            return "OK", [b"1 2 3"]
        if command == "SEARCH" and args[0] == "X-GM-MSGID":
            uid = next((u for u, m in self.MSGIDS.items() if m == int(args[1])), None)
            return "OK", [uid or b""]
        uids, what = args
        assert "BODY[]" not in what, "bodies must be fetched with BODY.PEEK so nothing is marked as read"
        if what == "(X-GM-MSGID)":
            return "OK", [b"%d (X-GM-MSGID %d UID %s)" % (i, self.MSGIDS[u], u) for i, u in enumerate(uids.encode().split(b","), 1)]
        raw = receipt_bytes()
        meta = b'2 (X-GM-THRID 77 INTERNALDATE "17-Oct-2026 10:00:00 +0300" UID %s BODY[] {%d}' % (uids.encode(), len(raw))
        return "OK", [(meta, raw), b")"]


@pytest.fixture
def fake():
    return FakeIMAP()


def client(fake, password=PASSWORD, address="dana@gmail.com"):
    return ImapGmailClient(address, password, connect=lambda: fake)


def test_search_uses_gmail_query_and_api_ids_newest_first(fake):
    c = client(fake)
    assert fake.selected == ALL_MAIL.decode()
    query = build_query()
    ids = list(c.search(query))
    assert fake.queries == [query]  # Hebrew keywords sent intact as a UTF-8 literal
    assert ids == [format(1003, "x"), format(1002, "x"), format(1001, "x")]  # same ids as the Gmail API
    assert list(c.search(query, limit=1)) == [format(1003, "x")]


def test_message_converts_to_gmail_api_shape(fake):
    c = client(fake)
    email = parse_gmail_message(c.message(format(1002, "x")))
    assert email.subject == "קבלה על ההזמנה שלך"
    assert email.sender_email == "info@wolt.com" and email.sender_name == "Wolt"
    assert email.message_id == "<abc123@wolt.com>"
    assert email.date == "2026-10-17"
    assert "89.90" in email.text
    [pdf] = email.pdfs
    assert pdf.filename == "קבלה-123.pdf" and pdf.data.startswith(b"%PDF")  # inline: no attachment() round trip


@pytest.mark.parametrize(("address", "password", "code"), [
    ("dana@gmail.com", "wrong-password-x", "auth"),
    ("not-an-address", PASSWORD, "app_password"),
])
def test_login_errors_map_to_codes(fake, address, password, code):
    with pytest.raises(GmailError) as e:
        client(fake, password, address)
    assert e.value.code == code


def test_app_password_spaces_are_ignored(fake):
    client(fake, "abcd efgh ijkl mnop")
    assert normalize_app_password(" abcd efgh\tijkl mnop ") == PASSWORD


def test_backend_connects_over_imap_and_scans_with_it(tmp_path, qapp, monkeypatch):
    from kabala.backend import Backend
    from kabala.store import Store

    vault = {}
    monkeypatch.setattr(imap_gmail, "save_login", lambda a, p: vault.update(email=a, password=p))
    monkeypatch.setattr(imap_gmail, "load_login", lambda: (vault["email"], vault["password"]) if vault else None)
    monkeypatch.setattr(google_auth, "load_stored", lambda: None)
    fake = FakeIMAP()
    monkeypatch.setattr(imap_gmail, "ImapGmailClient", lambda a, p: ImapGmailClient(a, p, connect=lambda: fake))

    b = Backend(store=Store(tmp_path / "k.db"), license_url=None)
    done = threading.Event()
    b.connected.connect(lambda _acc: done.set())
    b.connect_imap("dana@gmail.com", "abcd efgh ijkl mnop")
    deadline = 50
    while not done.is_set() and deadline:
        qapp.processEvents()
        done.wait(0.1)
        deadline -= 1
    assert b.account.provider == "imap" and vault == {"email": "dana@gmail.com", "password": PASSWORD}
    scanner = b._make_scanner(0.75)
    assert isinstance(scanner.make_gmail(), ImapGmailClient)

    # next launch: restored from the vault without asking again
    again = Backend(store=Store(tmp_path / "k2.db"), license_url=None)
    assert again.account.email == "dana@gmail.com" and again.account.provider == "imap"


def test_second_click_does_not_open_a_second_consent_page(tmp_path, qapp, monkeypatch):
    from kabala.backend import Backend
    from kabala.store import Store

    monkeypatch.setattr(google_auth, "load_stored", lambda: None)
    monkeypatch.setattr(imap_gmail, "load_login", lambda: None)
    calls = []

    def fake_sign_in(cancel):
        calls.append(1)
        cancel.wait(5)
        raise google_auth.AuthError("canceled")

    monkeypatch.setattr(google_auth, "sign_in", fake_sign_in)
    b = Backend(store=Store(tmp_path / "k.db"), license_url=None)
    b.connect_account()
    b.connect_account()
    b.connect_account()
    b.cancel_connect()
    b._auth_thread.join(2)
    assert calls == [1]
