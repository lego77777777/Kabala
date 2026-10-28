"""
Automatic receipts for Stripe payments (docs/stripe-receipts-prompt.md), like the PayPal server's.

    Stripe invoice.paid (webhook, verified) ─▶ receipt row (pending, numbered) ─▶ PDF (receipts_pdf.py, encrypted)
      ─▶ generated ─▶ email to the customer (+ hidden copy) ─▶ sent

The database is the queue: run_due() does whatever is due, from a background thread every few seconds and
right after each webhook (kick()). Every step gets 5 attempts with growing waits; then the receipt is failed,
the admin is alerted, and (when "retry automatically" is on) it gets up to 3 more rounds an hour apart.
Settings ("Automatic sending" in the admin panel): on/off, hidden copy, subject and opening line (chevron
templates, receipt_templates.py), automatic retries.

Numbering: a running number in its own range (RECEIPT_NUMBER_START, default 500001), taken inside a
transaction, never reused. Confirm the arrangement with your accountant.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from kabala_server import email_login
from kabala_server.config import Config
from kabala_server.crypto import box
from kabala_server.db import Database
from kabala_server.receipt_templates import LIMITS, VARIABLES, TemplateError, check_template, render_template
from kabala_server.receipts_pdf import ReceiptData, Seller, build_pdf, period_text, title

log = logging.getLogger("kabala_server.receipts")

MAX_ATTEMPTS = 5
BACKOFF_S = (30, 60, 120, 240)  # wait after attempt 1, 2, 3, 4
AUTO_RETRY = {"rounds": 3, "everyMs": 3600 * 1000}
AUTOMATIC = ("auto", "retry")
TZ = ZoneInfo("Asia/Jerusalem")
SYMBOLS = {"ILS": "₪", "USD": "$", "EUR": "€", "GBP": "£"}
MONTHS_HE = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]
DEFAULTS = {
    "he": {"subject": "הקבלה שלך מ-{{business}} – {{month}}", "intro": "תודה שבחרת ב-Kabala Pro! מצורפת הקבלה על התשלום.",
           "hello": "שלום {name},", "hello_anon": "שלום,", "summary": "פרטי התשלום", "number": "מספר", "amount": "סכום",
           "plan": {"month": "Kabala Pro – מנוי חודשי", "year": "Kabala Pro – מנוי שנתי"}, "period": "תקופה",
           "signoff": "תודה, צוות Kabala", "why": "קיבלת את המייל הזה כי שילמת על מנוי Kabala Pro."},
    "en": {"subject": "Your {{business}} receipt – {{month}}", "intro": "Thank you for choosing Kabala Pro! Your receipt is attached.",
           "hello": "Hi {name},", "hello_anon": "Hi,", "summary": "Payment details", "number": "Number", "amount": "Amount",
           "plan": {"month": "Kabala Pro – monthly", "year": "Kabala Pro – yearly"}, "period": "Period",
           "signoff": "Thanks, the Kabala team", "why": "You received this email because you paid for Kabala Pro."},
}
SETTING_FIELDS = ("autoSend", "autoRetry", "copyTo", "subjectHe", "subjectEn", "introHe", "introEn")
COLUMNS = {"autoSend": "auto_send", "autoRetry": "auto_retry", "copyTo": "copy_to", "subjectHe": "subject_he",
           "subjectEn": "subject_en", "introHe": "intro_he", "introEn": "intro_en"}


@dataclass
class Mail:
    to: str
    subject: str
    text: str
    html: str
    bcc: list[str] = field(default_factory=list)
    attachments: list[tuple[str, bytes]] = field(default_factory=list)  # (file name, PDF bytes)


class ReceiptMailer(Protocol):
    def send_mail(self, mail: Mail) -> None: ...


def receipt_message(sender: str, mail: Mail) -> EmailMessage:
    """Text and HTML versions plus the PDF. No Bcc header: each mailer delivers the hidden copy its own way."""
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, mail.to, mail.subject
    msg.set_content(mail.text)
    msg.add_alternative(mail.html, subtype="html")
    for name, data in mail.attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg


def money(minor: int, currency: str) -> str:
    return f"{SYMBOLS.get(currency.upper(), currency.upper() + ' ')}{minor / 100:,.2f}"


def local_date(value: str | int | float | None) -> str:
    if value in (None, ""):
        return ""
    dt = datetime.fromtimestamp(value, timezone.utc) if isinstance(value, (int, float)) else datetime.fromisoformat(str(value))
    return dt.astimezone(TZ).strftime("%d/%m/%Y")


def month_name(iso: str, locale: str) -> str:
    dt = datetime.fromisoformat(iso).astimezone(TZ)
    return f"{MONTHS_HE[dt.month - 1]} {dt.year}" if locale == "he" else dt.strftime("%B %Y")


def is_mailbox(value: str) -> bool:
    """One plain address, same rules as the PayPal server: no display name, no , ; < > \" or line breaks."""
    return bool(email_login.normalize_email(value)) and not any(c in value for c in ',;<>"\r\n ')


class Receipts:
    def __init__(self, db: Database, config: Config, mailer: ReceiptMailer | None, clock: Callable[[], float] = time.time) -> None:
        self.db, self.config, self.mailer, self.clock = db, config, mailer, clock
        self.pdf_box = box(config.secrets, "receipt-pdf")
        self.seller = Seller(config.seller_name, config.seller_tax_id, config.seller_address, config.seller_email,
                             config.business_type, config.vat_rate_percent)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()

    # ---- settings -----------------------------------------------------------------------------

    def settings(self) -> dict:
        row = self.db.one("SELECT * FROM receipt_settings WHERE id=1") or {}
        out = {k: row.get(c) for k, c in COLUMNS.items()}
        out["autoSend"] = bool(row.get("auto_send", 1))
        out["autoRetry"] = bool(row.get("auto_retry", 1))
        out.update(updatedAt=row.get("updated_at"), updatedBy=row.get("updated_by"))
        return out

    def check_patch(self, patch: dict) -> dict:
        out = {k: v for k, v in patch.items() if k in SETTING_FIELDS}
        for key, kind in (("subjectHe", "subject"), ("subjectEn", "subject"), ("introHe", "intro"), ("introEn", "intro")):
            if isinstance(out.get(key), str):
                out[key] = out[key].strip() or None
                if out[key]:
                    check_template(kind, out[key])
        if isinstance(out.get("copyTo"), str):
            c = out["copyTo"].strip()
            if c and not is_mailbox(c):
                raise TemplateError("copyTo", "כתובת המייל לעותק לא תקינה")
            out["copyTo"] = c.lower() or None
        return out

    def update_settings(self, patch: dict, actor: str) -> dict:
        clean = self.check_patch(patch)
        self.db.run("INSERT OR IGNORE INTO receipt_settings(id) VALUES (1)")
        for key, value in clean.items():
            self.db.run(f"UPDATE receipt_settings SET {COLUMNS[key]}=? WHERE id=1", (int(value) if isinstance(value, bool) else value,))
        self.db.run("UPDATE receipt_settings SET updated_at=?, updated_by=? WHERE id=1", (self._now_iso(), actor))
        return self.settings()

    # ---- trigger --------------------------------------------------------------------------------

    def on_invoice_paid(self, invoice: dict, user_id: int, customer_email: str) -> int | None:
        """A verified, paid Stripe invoice → a numbered receipt (once per invoice). None for €0 invoices and repeats."""
        amount = int(invoice.get("amount_paid") or 0)
        if amount <= 0:
            return None
        line = ((invoice.get("lines") or {}).get("data") or [{}])[0]
        period = line.get("period") or {}
        recurring = ((line.get("price") or {}).get("recurring") or (line.get("plan") or {}))
        interval = recurring.get("interval") if isinstance(recurring, dict) else None
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM receipts WHERE stripe_invoice_id=?", (invoice["id"],)).fetchone():
                return None
            last = conn.execute("SELECT MAX(number) FROM receipts").fetchone()[0]
            number = max(int(last or 0), self.config.receipt_number_start - 1) + 1
            cur = conn.execute(
                "INSERT INTO receipts(number, user_id, stripe_invoice_id, amount_minor, currency, plan_interval, period_start, "
                "period_end, customer_email, customer_name, locale, issued_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (number, user_id, invoice["id"], amount, (invoice.get("currency") or "ils").upper(), interval,
                 period.get("start"), period.get("end"), customer_email, invoice.get("customer_name"), "he", self._now_iso()),
            )
            receipt_id = cur.lastrowid
        log.info("receipt %s issued for a Stripe invoice", number)
        self.kick()
        return receipt_id

    # ---- the worker ---------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="receipts", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def kick(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(10)
            self._wake.clear()
            try:
                self.run_due()
            except Exception:  # noqa: BLE001 - keep the worker alive; the error is logged
                log.exception("receipt worker round failed")

    def run_due(self) -> dict:
        """One round: make due PDFs, queue automatic emails, send due emails, retry failed receipts."""
        with self._run_lock:
            done = self._round()
            if done["retried"]:  # the retried ones go straight away, in a second round under the same lock
                done = {k: v + done[k] for k, v in self._round().items()}
            return done

    def _round(self) -> dict:
        now = self.clock()
        settings = self.settings()
        done = {"generated": 0, "queued": 0, "sent": 0, "retried": 0}
        for r in self.db.rows("SELECT * FROM receipts WHERE status='pending' AND next_at<=?", (now,)):
            done["generated"] += self._generate(r)
        if settings["autoSend"]:
            for r in self.db.rows("SELECT * FROM receipts WHERE status='generated'"):
                if not self.db.one("SELECT 1 FROM receipt_emails WHERE receipt_id=? AND trigger IN ('auto','retry') "
                                   "AND status IN ('queued','sent')", (r["id"],)):
                    self._queue(r, "auto")
                    done["queued"] += 1
        for e in self.db.rows("SELECT * FROM receipt_emails WHERE status='queued' AND next_at<=? ORDER BY id", (now,)):
            done["sent"] += self._send(e)
        if settings["autoRetry"]:
            for r in self.db.rows("SELECT * FROM receipts WHERE status='failed' AND retry_rounds<? AND next_at<=?",
                                  (AUTO_RETRY["rounds"], now - AUTO_RETRY["everyMs"] / 1000)):
                self.db.run("UPDATE receipts SET retry_rounds=retry_rounds+1 WHERE id=?", (r["id"],))
                self._retry(r)
                done["retried"] += 1
        return done

    def _generate(self, r: dict) -> int:
        try:
            pdf = build_pdf(self._view(r), self.seller)
        except Exception as e:  # noqa: BLE001
            self._step_failed("receipts", r, r["attempts"] + 1, e, final_hook=lambda msg: self._receipt_failed(r, "pdf", msg))
            return 0
        encrypted = self.pdf_box.encrypt(base64.b64encode(pdf).decode())
        self.db.run("UPDATE receipts SET pdf=?, status='generated', attempts=attempts+1, last_error=NULL, next_at=0 WHERE id=?",
                    (encrypted, r["id"]))
        return 1

    def _queue(self, r: dict, trigger: str, to: str | None = None, draft: dict | None = None) -> None:
        settings = self.settings()
        copy = (settings["copyTo"] or self.config.receipt_copy_to or "").strip().lower()
        to_addr = to or r["customer_email"]
        bcc = copy if trigger != "test" and copy and copy != to_addr else ""
        now = self._now_iso()
        self.db.run("INSERT INTO receipt_emails(receipt_id, trigger, to_addresses, draft, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (r["id"], trigger, ",".join(a for a in (to_addr, bcc) if a), json.dumps(draft) if draft else None, now, now))

    def _send(self, e: dict) -> int:
        r = self.db.one("SELECT * FROM receipts WHERE id=?", (e["receipt_id"],))
        if not r or not r["pdf"]:
            return 0
        attempt = e["attempts"] + 1
        try:
            if not self.mailer or not getattr(self.mailer, "ready", True):
                raise RuntimeError("Gmail is not connected (admin panel → Gmail)")
            to, *bcc = e["to_addresses"].split(",")
            settings = {**self.settings(), **(json.loads(e["draft"]) if e["draft"] else {})}
            mail = self.compose(r, settings)
            if e["trigger"] == "test":
                mail.subject = f"[בדיקה / test] {mail.subject}"
            mail.to, mail.bcc = to, bcc
            mail.attachments = [(f"kabala-receipt-{r['number']}.pdf", self.pdf_bytes(r))]
            self.mailer.send_mail(mail)
        except Exception as ex:  # noqa: BLE001
            def final(msg: str) -> None:
                if e["trigger"] in AUTOMATIC:
                    self._receipt_failed(r, "email", msg)
            self._step_failed("receipt_emails", e, attempt, ex, final_hook=final)
            return 0
        now = self._now_iso()
        self.db.run("UPDATE receipt_emails SET status='sent', attempts=?, error=NULL, updated_at=? WHERE id=?", (attempt, now, e["id"]))
        if e["trigger"] != "test":
            self.db.run("UPDATE receipts SET status='sent', sent_at=COALESCE(sent_at, ?), last_error=NULL WHERE id=?", (now, r["id"]))
        log.info("receipt %s emailed (%s)", r["number"], e["trigger"])
        return 1

    def _step_failed(self, table: str, row: dict, attempt: int, error: Exception, final_hook: Callable[[str], None]) -> None:
        msg = f"{type(error).__name__}: {error}"[:500]
        final = attempt >= MAX_ATTEMPTS
        wait = BACKOFF_S[min(attempt, len(BACKOFF_S)) - 1]
        column = "last_error" if table == "receipts" else "error"
        extra = ", updated_at=?" if table == "receipt_emails" else ""
        args: tuple = (attempt, msg, self.clock() + wait) + ((self._now_iso(),) if extra else ()) + (row["id"],)
        if final and table == "receipt_emails":
            self.db.run(f"UPDATE receipt_emails SET status='failed', attempts=?, {column}=?, next_at=?{extra} WHERE id=?", args)
        else:
            self.db.run(f"UPDATE {table} SET attempts=?, {column}=?, next_at=?{extra} WHERE id=?", args)
        log.warning("receipt step failed (attempt %s): %s", attempt, type(error).__name__)
        if final:
            final_hook(msg)

    def _receipt_failed(self, r: dict, step: str, msg: str) -> None:
        self.db.run("UPDATE receipts SET status='failed', last_error=?, next_at=? WHERE id=?", (f"{step}: {msg}", self.clock(), r["id"]))
        log.error("receipt %s failed after all attempts (%s)", r["number"], step)
        if self.config.admin_email and self.mailer:
            try:
                self.mailer.send(self.config.admin_email, f"[Kabala billing] receipt {r['number']} failed",
                                 f"Stripe receipt {r['number']} (id {r['id']}) failed at {step} after {MAX_ATTEMPTS} attempts.\n"
                                 f"Customer: {r['customer_email']}\nStripe invoice: {r['stripe_invoice_id']}\nError: {msg}\n\n"
                                 "Retry it from the admin panel (Customer receipts) once the cause is fixed.")
            except Exception:  # noqa: BLE001
                log.exception("admin alert email failed too")

    def _retry(self, r: dict) -> None:
        if r["pdf"]:
            self.db.run("UPDATE receipts SET status='generated', last_error=NULL, next_at=0 WHERE id=?", (r["id"],))
            self._queue(r, "retry")
        else:
            self.db.run("UPDATE receipts SET status='pending', attempts=0, last_error=NULL, next_at=0 WHERE id=?", (r["id"],))

    # ---- content --------------------------------------------------------------------------------------

    def pdf_bytes(self, r: dict) -> bytes:
        raw = self.pdf_box.decrypt(r["pdf"]) if r.get("pdf") else None
        if not raw:
            raise RuntimeError("PDF missing or unreadable")
        return base64.b64decode(raw)

    def _view(self, r: dict) -> ReceiptData:
        net = vat = None
        if self.seller.business_type == "licensed" and self.seller.vat_rate_percent > 0:
            net_minor = round(r["amount_minor"] / (1 + self.seller.vat_rate_percent / 100))
            net, vat = money(net_minor, r["currency"]), money(r["amount_minor"] - net_minor, r["currency"])
        period = period_text(local_date(r["period_start"]), local_date(r["period_end"]), r["locale"]) if r["period_start"] else ""
        return ReceiptData(r["number"], local_date(r["issued_at"]), r["customer_name"] or "", r["customer_email"], r["plan_interval"],
                           period, money(r["amount_minor"], r["currency"]), net, vat, r["stripe_invoice_id"], r["locale"])

    def values(self, r: dict) -> dict[str, str]:
        t = DEFAULTS[r["locale"]]
        return {"name": r["customer_name"] or "", "number": str(r["number"]), "amount": money(r["amount_minor"], r["currency"]),
                "month": month_name(r["issued_at"], r["locale"]), "plan": t["plan"].get(r["plan_interval"] or "month", t["plan"]["month"]),
                "business": self.seller.name}

    def texts(self, r: dict, settings: dict) -> tuple[str, str]:
        loc = r["locale"]
        suffix = "He" if loc == "he" else "En"
        values = self.values(r)
        subject = render_template("subject", settings.get(f"subject{suffix}") or DEFAULTS[loc]["subject"], values)
        intro = render_template("intro", settings.get(f"intro{suffix}") or DEFAULTS[loc]["intro"], values)
        return subject, intro

    def compose(self, r: dict, settings: dict) -> Mail:
        t = DEFAULTS[r["locale"]]
        subject, intro = self.texts(r, settings)
        v = self.values(r)
        hello = t["hello"].format(name=v["name"]) if v["name"] else t["hello_anon"]
        rows = [(title(self.seller, r["locale"]), v["number"]), (t["amount"], v["amount"]), ("", v["plan"])]
        if r["period_start"]:
            rows.append((t["period"], period_text(local_date(r["period_start"]), local_date(r["period_end"]), r["locale"])))
        text = "\n".join([hello, "", intro, "", f"{t['summary']}:", *(f"  {a}: {b}" if a else f"  {b}" for a, b in rows), "",
                          t["signoff"], "", "--", t["why"], self.seller.name])
        esc = html.escape
        direction = "rtl" if r["locale"] == "he" else "ltr"
        table = "".join(f'<tr><td style="color:#64646e;padding:6px 0">{esc(a)}</td><td style="padding:6px 0;font-weight:700">'
                        f'<span dir="ltr">{esc(b)}</span></td></tr>' if a else f'<tr><td colspan="2" style="padding:6px 0">{esc(b)}</td></tr>'
                        for a, b in rows)
        body = (f'<div dir="{direction}" style="font-family:Heebo,Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.6;'
                f'color:#18181b;max-width:560px;margin:auto">'
                f'<p style="font-size:22px;font-weight:700;color:#4f46e5;margin:0 0 16px">{esc(self.seller.name)}</p>'
                f'<p style="font-weight:700;font-size:18px;margin:0 0 6px">{esc(hello)}</p>'
                f'<p style="white-space:pre-line;margin:0 0 18px">{esc(intro)}</p>'
                f'<p style="color:#64646e;font-weight:700;font-size:13px;margin:0 0 4px">{esc(t["summary"])}</p>'
                f'<table style="width:100%;border-top:1px solid #e4e4e7;border-bottom:1px solid #e4e4e7">{table}</table>'
                f'<p style="margin:18px 0 0">{esc(t["signoff"])}</p>'
                f'<p style="color:#64646e;font-size:12px;margin-top:24px">{esc(t["why"])}<br>{esc(self.seller.name)}</p></div>')
        return Mail(to=r["customer_email"], subject=subject, text=text, html=body)

    # ---- the admin panel ------------------------------------------------------------------------------

    def list(self, q: str, status: str, page: int, size: int = 25) -> tuple[list[dict], int]:
        where, args = [], []
        if status in ("pending", "generated", "sent", "failed"):
            where.append("status=?")
            args.append(status)
        if q:
            where.append("customer_email LIKE ? ESCAPE '\\'")
            args.append("%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        sql_where = f"WHERE {' AND '.join(where)}" if where else ""
        total = self.db.one(f"SELECT COUNT(*) AS n FROM receipts {sql_where}", tuple(args))["n"]
        rows = self.db.rows(f"SELECT * FROM receipts {sql_where} ORDER BY id DESC LIMIT ? OFFSET ?", (*args, size, (page - 1) * size))
        return rows, total

    def emails(self, receipt_id: int) -> list[dict]:
        return self.db.rows("SELECT * FROM receipt_emails WHERE receipt_id=? ORDER BY id DESC", (receipt_id,))

    def get(self, receipt_id: int) -> dict | None:
        return self.db.one("SELECT * FROM receipts WHERE id=?", (receipt_id,))

    def admin_send(self, receipt_id: int, to: str | None) -> str:
        r = self.get(receipt_id)
        if not r:
            return "not_found"
        if not r["pdf"]:
            return "not_ready"
        if to and not is_mailbox(to):
            return "invalid_address"
        self._queue(r, "admin", to.strip().lower() if to else None)
        self.kick()
        return "queued"

    def retry(self, receipt_id: int) -> str:
        r = self.get(receipt_id)
        if not r:
            return "not_found"
        if r["status"] != "failed":
            return "not_failed"
        self._retry(r)
        self.kick()
        return "queued"

    def preview(self, draft: dict, locale: str = "he") -> dict:
        settings = {**self.settings(), **self.check_patch(draft)}
        r = self.db.one("SELECT * FROM receipts ORDER BY id DESC LIMIT 1") or {
            "number": self.config.receipt_number_start, "amount_minor": 2900, "currency": "ILS", "plan_interval": "month",
            "customer_name": "דנה כהן", "issued_at": self._now_iso()}
        subject, intro = self.texts({**r, "locale": locale}, settings)
        return {"subject": subject, "intro": intro}

    def send_test(self, to: str, draft: dict) -> str:
        r = self.db.one("SELECT * FROM receipts WHERE pdf IS NOT NULL ORDER BY id DESC LIMIT 1")
        if not r:
            return "no_receipts"
        self._queue(r, "test", to, self.check_patch(draft))
        self.kick()
        return "queued"

    @staticmethod
    def meta() -> dict:
        return {"variables": VARIABLES, "limits": LIMITS, "retry": AUTO_RETRY}

    def _now_iso(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")

