"""Things that must stay encrypted or erased on the user's computer."""

import imaplib
import ssl

import pytest

from kabala import imap_gmail
from kabala.store import Store


def test_imap_checks_googles_certificate(monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def fake_ssl(host, port, timeout=None, ssl_context=None):
        seen.update(host=host, port=port, context=ssl_context)
        raise Stop

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_ssl)
    with pytest.raises(Stop):
        imap_gmail.ImapGmailClient("dana@gmail.com", "abcdefghijklmnop")
    ctx = seen["context"]
    assert seen["host"] == "imap.gmail.com" and seen["port"] == 993
    assert ctx is not None and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_index_file_is_owner_only(tmp_path):
    path = tmp_path / "k.db"
    Store(path).close()
    assert path.stat().st_mode & 0o077 == 0


def test_delete_everything_really_erases_the_index(tmp_path):
    path = tmp_path / "k.db"
    store = Store(path)
    # on by default in some Linux builds of SQLite, off in the one Python ships on Windows: Store must set it
    assert store._db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    marker = "SecretCompanyLtd-4711"
    store.set_meta("dana@gmail.com", "note", marker * 50)
    store.wipe()
    store.close()
    for f in path.parent.glob("k.db*"):
        assert marker.encode() not in f.read_bytes(), f.name
