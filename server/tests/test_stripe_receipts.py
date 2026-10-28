"""Automatic receipts after Stripe payments (docs/stripe-receipts-prompt.md)."""

import json
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace as ns

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kabala_server.app import create_app  # noqa: E402
from kabala_server.config import Config  # noqa: E402
from kabala_server.receipts import AUTO_RETRY, MAX_ATTEMPTS  # noqa: E402

OWNER = "owner@kabala.app"
H = {"X-Kabala-Admin": "1"}


class Mailer:
    """Sign-in codes and alerts (send) and receipts (send_mail). `down` makes receipt emails fail."""

    def __init__(self):
        self.codes, self.alerts, self.receipts, self.down = [], [], [], False

    def send(self, to, subject, text):
        (self.alerts if subject.startswith("[Kabala billing]") else self.codes).append((to, subject, text))

    def send_mail(self, mail):
        if self.down:
            raise ConnectionError("Gmail API 503 UNAVAILABLE")
        self.receipts.append(mail)

    def code_for(self, to):
        return re.search(r"\b(\d{6})\b", [t for (a, _s, t) in self.codes if a == to][-1]).group(1)


class FakeStripe:
    v1 = ns(subscriptions=ns(retrieve=lambda sid: {"id": sid, "status": "active", "current_period_end": 1900000000}))

    def construct_event(self, payload, sig, secret):
        return json.loads(payload)


class Clock:
    def __init__(self):
        self.t = 1_793_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    cfg = Config(secret="x" * 40, google_client_id="cid", db_path=str(tmp_path / "s.db"), owner_emails=(OWNER,),
                 stripe_secret_key="sk_test", stripe_price_pro="price", stripe_webhook_secret="whsec",
                 seller_name="Kabala", seller_tax_id="123456789", admin_email="alerts@kabala.app")
    mailer = Mailer()
    app = create_app(cfg, lambda tok: {"email": tok, "email_verified": True}, FakeStripe(), mailer, receipt_worker=False)
    clock = Clock()
    app.state.receipts.clock = clock
    c = TestClient(app)
    return ns(app=app, c=c, mailer=mailer, clock=clock, rc=app.state.receipts, path=tmp_path / "s.db", n=iter(range(1, 10_000)))


def hook(env, kind, obj, event_id=None):
    event = {"id": event_id or f"evt_{next(env.n)}", "type": kind, "data": {"object": obj}}
    return env.c.post("/api/stripe/webhook", content=json.dumps(event), headers={"Stripe-Signature": "x"})


def customer(env, email="dana@gmail.com", cus="cus_1"):
    env.c.post("/api/session", json={"id_token": email})
    uid = env.app.state.receipts.db.by_email(email).id
    hook(env, "checkout.session.completed", {"client_reference_id": str(uid), "customer": cus, "subscription": "sub_1"})
    return uid


def invoice(inv="in_1", cus="cus_1", amount=2900, email="dana@gmail.com", name="דנה כהן", interval="month"):
    return {"id": inv, "customer": cus, "customer_email": email, "customer_name": name, "amount_paid": amount, "currency": "ils",
            "lines": {"data": [{"period": {"start": 1_793_000_000, "end": 1_795_600_000}, "price": {"recurring": {"interval": interval}}}]}}


def sign_in(env, email=OWNER):
    env.c.post("/admin/api/login/start", json={"email": email})
    assert env.c.post("/admin/api/login/verify", json={"email": email, "code": env.mailer.code_for(email)}).status_code == 200
    return env.c


def test_a_paid_invoice_gives_one_numbered_receipt_emailed_with_the_pdf(env):
    customer(env)
    assert hook(env, "invoice.paid", invoice(), "evt_pay").status_code == 200
    assert hook(env, "invoice.paid", invoice(), "evt_pay").json().get("duplicate")  # Stripe delivers twice
    assert hook(env, "invoice.paid", invoice(), "evt_other_id").status_code == 200  # same invoice, another event id
    env.rc.run_due()
    [mail] = env.mailer.receipts
    assert mail.to == "dana@gmail.com" and mail.subject.startswith("הקבלה שלך מ-Kabala")
    assert "שלום דנה כהן," in mail.text and "500001" in mail.text
    [(name, pdf)] = mail.attachments
    assert name == "kabala-receipt-500001.pdf" and pdf.startswith(b"%PDF")
    r = env.rc.db.one("SELECT * FROM receipts")
    assert r["status"] == "sent" and r["number"] == 500001 and r["amount_minor"] == 2900 and r["plan_interval"] == "month"


def test_renewals_get_the_next_number_and_free_invoices_none(env):
    customer(env)
    for inv in ("in_1", "in_2"):
        hook(env, "invoice.paid", invoice(inv))
    hook(env, "invoice.paid", invoice("in_trial", amount=0))
    env.rc.run_due()
    assert [r["number"] for r in env.rc.db.rows("SELECT number FROM receipts ORDER BY id")] == [500001, 500002]


def test_numbers_are_unique_even_when_payments_arrive_together(env):
    uid = customer(env)
    threads = [threading.Thread(target=env.rc.on_invoice_paid, args=(invoice(f"in_{i}"), uid, "dana@gmail.com")) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    numbers = sorted(r["number"] for r in env.rc.db.rows("SELECT number FROM receipts"))
    assert numbers == list(range(500001, 500021))


def test_an_invoice_before_the_customer_is_linked_is_delivered_again(env):
    env.c.post("/api/session", json={"id_token": "new@gmail.com"})
    early = hook(env, "invoice.paid", invoice(cus="cus_new", email="other@x.com"), "evt_early")
    assert early.status_code == 503
    uid = env.rc.db.by_email("new@gmail.com").id
    hook(env, "checkout.session.completed", {"client_reference_id": str(uid), "customer": "cus_new"})
    assert hook(env, "invoice.paid", invoice(cus="cus_new", email="other@x.com"), "evt_early").status_code == 200  # Stripe's retry
    assert env.rc.db.one("SELECT user_id FROM receipts")["user_id"] == uid


def test_the_pdf_is_encrypted_in_the_database(env):
    customer(env)
    hook(env, "invoice.paid", invoice())
    env.rc.run_due()
    assert b"%PDF" not in env.path.read_bytes()
    assert env.rc.pdf_bytes(env.rc.db.one("SELECT * FROM receipts")).startswith(b"%PDF")


def test_with_automatic_sending_off_the_pdf_waits_for_a_manual_send(env):
    customer(env)
    env.rc.update_settings({"autoSend": False}, OWNER)
    hook(env, "invoice.paid", invoice())
    env.rc.run_due()
    env.rc.run_due()
    assert env.mailer.receipts == [] and env.rc.db.one("SELECT status FROM receipts")["status"] == "generated"
    admin = sign_in(env)
    rid = admin.get("/admin/api/stripe/receipts").json()["receipts"][0]["id"]
    assert admin.post(f"/admin/api/stripe/receipts/{rid}/send", json={}, headers=H).json() == {"ok": True}
    env.rc.run_due()
    assert len(env.mailer.receipts) == 1


def test_failing_email_retries_then_fails_alerts_and_retries_automatically(env):
    customer(env)
    env.mailer.down = True
    hook(env, "invoice.paid", invoice())
    for _ in range(MAX_ATTEMPTS + 2):
        env.rc.run_due()
        env.clock.t += 300  # past every backoff wait
    r = env.rc.db.one("SELECT * FROM receipts")
    assert r["status"] == "failed" and "Gmail API 503" in r["last_error"]
    assert [a[0] for a in env.mailer.alerts] == ["alerts@kabala.app"]
    for _ in range(AUTO_RETRY["rounds"] + 2):  # each round fails again, an hour apart, and they stop at the limit
        env.clock.t += AUTO_RETRY["everyMs"] / 1000 + 60
        for _ in range(MAX_ATTEMPTS + 1):
            env.rc.run_due()
            env.clock.t += 300
    retries = env.rc.db.rows("SELECT * FROM receipt_emails WHERE trigger='retry'")
    assert len(retries) == AUTO_RETRY["rounds"]


def test_the_mail_server_coming_back_delivers_on_the_automatic_retry(env):
    customer(env)
    env.mailer.down = True
    hook(env, "invoice.paid", invoice())
    for _ in range(MAX_ATTEMPTS + 1):
        env.rc.run_due()
        env.clock.t += 300
    env.mailer.down = False
    env.clock.t += AUTO_RETRY["everyMs"] / 1000 + 60
    env.rc.run_due()
    assert len(env.mailer.receipts) == 1 and env.rc.db.one("SELECT status FROM receipts")["status"] == "sent"


def test_custom_texts_and_hidden_copy_apply_to_the_next_receipt(env):
    customer(env)
    env.rc.update_settings({"subjectHe": "קבלה {{number}} – {{amount}}", "introHe": "היי {{name}}, תודה!", "copyTo": "CPA@office.co.il"}, OWNER)
    hook(env, "invoice.paid", invoice(name="Dana\r\nBcc: evil@x.com"))
    env.rc.run_due()
    [mail] = env.mailer.receipts
    assert mail.subject == "קבלה 500001 – ₪29.00" and mail.bcc == ["cpa@office.co.il"]
    assert "היי Dana" in mail.text and "Bcc: evil" not in mail.subject


def test_bad_templates_and_addresses_are_refused(env):
    admin = sign_in(env)
    for body in ({"introHe": "{{> ../../etc/passwd}}"}, {"subjectHe": "{{#name}}x{{/name}}"}, {"introHe": "שלום {{nope}}"},
                 {"copyTo": "a@b.co,evil@x.com"}):
        assert admin.post("/admin/api/stripe/receipt-settings", json=body, headers=H).status_code == 400, body
    assert admin.get("/admin/api/stripe/receipt-settings").json()["introHe"] is None


def test_panel_lists_sends_downloads_previews_and_tests(env):
    customer(env)
    hook(env, "invoice.paid", invoice())
    env.rc.run_due()
    admin = sign_in(env)
    [r] = admin.get("/admin/api/stripe/receipts").json()["receipts"]
    assert r["number"] == "500001" and r["status"] == "sent" and r["lastEmail"]["trigger"] == "auto"
    assert admin.post(f"/admin/api/stripe/receipts/{r['id']}/send", json={"to": "cpa@office.co.il"}, headers=H).json() == {"ok": True}
    assert admin.post(f"/admin/api/stripe/receipts/{r['id']}/send", json={"to": "a@b.co,x@y.co"}, headers=H).status_code == 409
    env.rc.run_due()
    assert env.mailer.receipts[-1].to == "cpa@office.co.il"
    assert admin.get(f"/admin/api/stripe/receipts/{r['id']}/pdf").content.startswith(b"%PDF")
    assert admin.post("/admin/api/stripe/receipt-settings/preview", json={"subjectHe": "טיוטה {{number}}"}, headers=H).json()["subject"] == "טיוטה 500001"
    assert admin.post("/admin/api/stripe/receipt-settings/test", json={"introHe": "טיוטה"}, headers=H).json()["to"] == OWNER
    env.rc.run_due()
    test = env.mailer.receipts[-1]
    assert test.to == OWNER and test.subject.startswith("[בדיקה / test]") and "טיוטה" in test.text and test.bcc == []
    detail = admin.get(f"/admin/api/stripe/receipts/{r['id']}").json()
    assert [e["trigger"] for e in detail["emails"]] == ["test", "admin", "auto"]
    actions = [e["action"] for e in admin.get("/admin/api/audit").json()["entries"]]
    assert {"receipt_sent", "receipt_downloaded", "receipt_test"} <= set(actions)


def test_viewers_see_masked_and_cannot_act(env):
    customer(env)
    hook(env, "invoice.paid", invoice())
    env.rc.run_due()
    sign_in(env).post("/admin/api/admins", json={"email": "view@kabala.app", "role": "viewer"}, headers=H)
    v = sign_in(env, "view@kabala.app")
    [r] = v.get("/admin/api/stripe/receipts").json()["receipts"]
    assert r["email"] == "da***@gmail.com" and r["lastEmail"]["to"] == "da***@gmail.com"
    assert v.get(f"/admin/api/stripe/receipts/{r['id']}/pdf").status_code == 403
    assert v.post(f"/admin/api/stripe/receipts/{r['id']}/send", json={}, headers=H).status_code == 403
    assert v.post("/admin/api/stripe/receipt-settings", json={"autoSend": False}, headers=H).status_code == 403
