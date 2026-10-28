"""After paying with PayPal, Pro turns on by itself: no license key to type."""

import threading

from kabala import google_auth, imap_gmail, licensing


class SlowServer:
    """The plan server: says Free for the first `free_checks` checks (PayPal confirmation still on its way)."""

    def __init__(self, free_checks):
        self.free_checks = free_checks
        self.checks = 0
        self.token = "S1"
        self.keys_typed = 0

    def status(self):
        self.checks += 1
        pro = self.checks > self.free_checks
        return licensing.Plan(managed=True, plan="pro" if pro else "free", monthly_receipts=None if pro else 50)

    def activate_key(self, key):
        self.keys_typed += 1
        raise AssertionError("no key should be needed after paying")


def backend(tmp_path, monkeypatch, server):
    from kabala.backend import Backend
    from kabala.store import Store

    monkeypatch.setattr(google_auth, "load_stored", lambda: None)
    monkeypatch.setattr(imap_gmail, "load_login", lambda: None)
    b = Backend(store=Store(tmp_path / "k.db"), license_url=None)
    b.license = server
    for name in ("plan_changed", "payment_activated"):
        getattr(b, name).connect(lambda *_a: None)
    return b


def wait(qapp, event, rounds=50):
    while not event.is_set() and rounds:
        qapp.processEvents()
        event.wait(0.1)
        rounds -= 1
    qapp.processEvents()
    return event.is_set()


def test_payment_success_link_keeps_checking_until_pro(tmp_path, monkeypatch, qapp):
    server = SlowServer(free_checks=2)
    b = backend(tmp_path, monkeypatch, server)
    b.PAYMENT_RETRY_S = (0, 0, 0, 0)
    done = threading.Event()
    b.payment_activated.connect(done.set)
    b.handle_deep_link("kabala://payment-success?status=pro")
    assert wait(qapp, done)
    assert b.plan.is_pro and server.checks == 3 and server.keys_typed == 0


def test_gives_up_quietly_if_the_server_never_confirms(tmp_path, monkeypatch, qapp):
    server = SlowServer(free_checks=99)
    b = backend(tmp_path, monkeypatch, server)
    activated = []
    b.payment_activated.connect(lambda: activated.append(1))
    finished = threading.Event()
    waits = []

    def fake_sleep(s):
        waits.append(s)
        if len(waits) == len(b.PAYMENT_RETRY_S):
            threading.Timer(0.2, finished.set).start()

    b.activate_after_payment(sleep=fake_sleep)
    assert wait(qapp, finished)
    assert waits == list(b.PAYMENT_RETRY_S) and server.checks == len(b.PAYMENT_RETRY_S)
    assert not activated and not b.plan.is_pro
    assert sum(b.PAYMENT_RETRY_S) <= 60  # about a minute at most
