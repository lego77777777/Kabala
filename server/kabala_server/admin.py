"""
Admin panel at /admin: who signed up, who pays, who may manage the server. Opens in Chrome (or any browser).

Sign-in: email → 6-digit code by email (same HOTP flow as email_login.py, separate challenges) → an encrypted,
HttpOnly, SameSite=Strict cookie for 8 hours. Only addresses in the `admins` table get a code; the first owners
come from KABALA_OWNER_EMAILS. The role is re-read from the database on every request, so removing an admin
takes effect immediately.

Roles (each includes the one before): viewer sees stats and masked emails; admin sees full emails, exports
CSV, gives or takes manual Pro, lifts IP bans, reads the audit log; owner manages admins. Every sign-in,
failed code and action is written to admin_audit.
"""

from __future__ import annotations

import csv
import html
import io
import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from kabala_server import email_login
import re

from kabala_server.admin_billing import BillingClient, BillingNotFound, BillingRefused, BillingUnavailable
from kabala_server.config import Config
from kabala_server.crypto import box
from kabala_server.db import ROLES, Database
from kabala_server.receipt_templates import TemplateError
from kabala_server.shield import active_bans, lift_ban
from kabala_server.vendor.slowapi.util import client_ip

UI = Path(__file__).with_name("admin_ui")
COOKIE = "kabala_admin"
SESSION_S = 8 * 3600
CSRF_HEADER = "x-kabala-admin"
STATIC = {"admin.css": "text/css", "admin.js": "text/javascript", "Heebo-Regular.ttf": "font/ttf", "Heebo-Bold.ttf": "font/ttf"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class LoginStart(BaseModel):
    email: str = Field(max_length=254)


class LoginVerify(BaseModel):
    email: str = Field(max_length=254)
    code: str = Field(max_length=20)


class CompIn(BaseModel):
    on: bool


class AdminIn(BaseModel):
    email: str = Field(max_length=254)
    role: str


class EmailIn(BaseModel):
    email: str = Field(max_length=254)


class ReceiptSettingsIn(BaseModel):
    autoSend: bool | None = None
    autoRetry: bool | None = None
    copyTo: str | None = Field(default=None, max_length=254)
    subjectHe: str | None = Field(default=None, max_length=150)
    subjectEn: str | None = Field(default=None, max_length=150)
    introHe: str | None = Field(default=None, max_length=1000)
    introEn: str | None = Field(default=None, max_length=1000)
    locale: str | None = Field(default=None, pattern="^(he|en)$")


class SendIn(BaseModel):
    to: str | None = Field(default=None, max_length=254)  # empty: the customer (and their accountant copy)


class IpIn(BaseModel):
    ip: str = Field(max_length=64)


def mask(email: str) -> str:
    """dana.levi@gmail.com → da***@gmail.com (what a viewer sees)."""
    name, _, domain = email.partition("@")
    return f"{name[:2]}***@{domain}"


def csv_cell(value: Any) -> str:
    """Excel treats a cell starting with = + - @ as a formula: prefix it so an email can't run one."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _user_out(row: dict, full: bool) -> dict:
    return {
        "id": row["id"],
        "email": row["email"] if full else mask(row["email"]),
        "plan": row["plan"],
        "status": row["subscription_status"],
        "period_end": row["current_period_end"],
        "created_at": row["created_at"],
        "last_seen_at": row.get("last_seen_at"),
        "stripe_customer": row["stripe_customer_id"] if full else (bool(row["stripe_customer_id"]) or None),
    }


def install(app: FastAPI, db: Database, config: Config, mailer: email_login.Mailer | None, limiter: Any,
            jails: OrderedDict, clock: Callable[[], float] = time.monotonic, billing: BillingClient | None = None,
            receipts: Any = None, gmail: Any = None) -> None:
    sessions = box(config.secrets, "admin-session")
    if billing is None and config.billing_url and config.billing_admin_key:
        billing = BillingClient(config.billing_url, config.billing_admin_key)
    for email in config.owner_emails:
        db.ensure_owner(email)

    def ip_of(request: Request) -> str:
        return client_ip(request.scope, config.proxy_hops)

    @app.middleware("http")
    async def admin_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/admin" or request.url.path.startswith("/admin/"):
            for k, v in SECURITY_HEADERS.items():
                response.headers.setdefault(k, v)
        return response

    # ---- session ---------------------------------------------------------------------

    def current_admin(request: Request) -> dict:
        raw = sessions.decrypt(request.cookies.get(COOKIE, ""))
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        if not isinstance(data, dict) or data.get("exp", 0) < time.time():
            raise HTTPException(401, "sign in required")
        role = db.admin_role(data.get("email", ""))  # re-read: a removed or changed admin loses access at once
        if not role:
            raise HTTPException(401, "sign in required")
        if request.method != "GET" and request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "missing admin header")  # a cross-site form can't set a custom header
        return {"email": data["email"], "role": role, "ip": ip_of(request)}

    def need(role: str) -> Callable[..., dict]:
        def check(admin: dict = Depends(current_admin)) -> dict:
            if ROLES.index(admin["role"]) < ROLES.index(role):
                raise HTTPException(403, f"needs the {role} role")
            return admin

        return check

    viewer, admin_, owner = need("viewer"), need("admin"), need("owner")

    # ---- pages -------------------------------------------------------------------------

    @app.get("/admin", include_in_schema=False)
    @limiter.exempt
    def page() -> FileResponse:
        return FileResponse(UI / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/admin/static/{name}", include_in_schema=False)
    @limiter.exempt
    def static(name: str) -> FileResponse:
        if name not in STATIC:  # fixed list: no path tricks
            raise HTTPException(404)
        path = UI / ("fonts/" + name if name.endswith(".ttf") else name)
        return FileResponse(path, media_type=STATIC[name], headers={"Cache-Control": "public, max-age=3600"})

    # ---- sign-in -------------------------------------------------------------------------

    @app.post("/admin/api/login/start")
    @limiter.limit("5 per 10 minutes")
    def login_start(request: Request, body: LoginStart) -> dict:
        email = email_login.normalize_email(body.email)
        if not mailer or not getattr(mailer, "ready", True):
            raise HTTPException(503, "Gmail is not connected")
        # Same answer whether or not the address is an admin, so the panel can't be used to find out who is.
        if email and db.admin_role(email):
            issued = db.issue_email_code(f"admin:{email}", email_login.new_secret(), time.time(), email_login.CODE_TTL_S,
                                         email_login.MAX_SENDS, email_login.SEND_WINDOW_S)
            if issued:
                code = email_login.make_code(*issued)
                try:
                    mailer.send(email, f"קוד כניסה לפאנל הניהול של Kabala: {code}",
                                f"קוד הכניסה שלך לפאנל הניהול: {code}\nתקף ל-10 דקות. אם לא ביקשת אותו, מישהו מנסה להיכנס עם הכתובת שלך.\n\n"
                                f"Your Kabala admin sign-in code: {code}\nValid for 10 minutes.\n")
                except Exception as e:  # noqa: BLE001
                    raise HTTPException(502, "could not send the email") from e
        return {"ok": True}

    @app.post("/admin/api/login/verify")
    @limiter.limit("10/minute")
    def login_verify(request: Request, response: Response, body: LoginVerify) -> dict:
        email = email_login.normalize_email(body.email)
        challenge = db.take_email_attempt(f"admin:{email}", time.time(), email_login.MAX_ATTEMPTS) if email else None
        role = db.admin_role(email) if email else None
        if not challenge or not role or not email_login.check_code(*challenge, body.code):
            if email and role:
                db.audit(email, "login_failed", None, ip_of(request))
            raise HTTPException(401, "invalid or expired code")
        db.close_email_code(f"admin:{email}")
        sign_in(request, response, email)
        return {"email": email, "role": role}

    def sign_in(request: Request, response: Response, email: str, how: str = "login") -> None:
        token = sessions.encrypt(json.dumps({"email": email, "exp": int(time.time()) + SESSION_S}))
        response.set_cookie(COOKIE, token, max_age=SESSION_S, httponly=True, samesite="strict", path="/admin",
                            secure=request.url.scheme == "https" or config.proxy_hops > 0)
        db.audit(email, how, None, ip_of(request))

    @app.post("/admin/api/logout")
    def logout(response: Response, admin: dict = Depends(viewer)) -> dict:
        response.delete_cookie(COOKIE, path="/admin")
        db.audit(admin["email"], "logout", None, admin["ip"])
        return {"ok": True}

    @app.get("/admin/api/me")
    def me(admin: dict = Depends(viewer)) -> dict:
        return {"email": admin["email"], "role": admin["role"]}

    # ---- Gmail: the account every email is sent from (gmail_api.py) -------------------------

    def redirect_uri(request: Request) -> str:
        if config.server_url:
            return config.server_url.rstrip("/") + "/admin/api/gmail/callback"
        uri = str(request.url_for("gmail_callback"))
        return uri.replace("http://", "https://", 1) if config.proxy_hops > 0 else uri  # TLS ends at the proxy

    def gmail_page(title: str, detail: str = "", ok: bool = False) -> HTMLResponse:
        """Google redirects the browser here. A page that links back (not a redirect), so the SameSite=Strict cookie
        is sent again on the way to the panel."""
        color = "#15803d" if ok else "#b91c1c"
        body = (f'<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                f'<meta http-equiv="refresh" content="{2 if ok else 30};url=/admin#gmail"><title>Gmail</title>'
                f'<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;text-align:center">'
                f'<h1 style="color:{color};font-size:1.4rem">{html.escape(title)}</h1><p>{html.escape(detail)}</p>'
                f'<p><a href="/admin#gmail">חזרה לפאנל הניהול</a></p></body></html>')
        return HTMLResponse(body, status_code=200 if ok else 400)

    paypal_sync: dict = {"ok": None, "error": None, "at": None}  # the last time the account was sent to billing

    def sync_paypal(actor: str) -> None:
        """The PayPal billing server sends as the same Gmail account: give it the account (or tell it to forget it)."""
        if not billing or not gmail:
            return
        creds = gmail.credentials()
        try:
            if creds:
                billing.post("/api/admin/gmail", actor, "owner", creds)
            else:
                billing.post("/api/admin/gmail/disconnect", actor, "owner")
            paypal_sync.update(ok=True, error=None, at=int(time.time()))
        except (BillingUnavailable, BillingRefused, BillingNotFound) as e:
            paypal_sync.update(ok=False, error=f"{type(e).__name__}: {e}"[:200], at=int(time.time()))

    def paypal_gmail(admin: dict) -> dict | None:
        if not billing:
            return None
        try:
            st = billing.get("/api/admin/gmail", admin["email"], admin["role"])
        except (BillingUnavailable, BillingRefused, BillingNotFound) as e:
            return {"reachable": False, "error": str(e)[:200], "sync": paypal_sync}
        mine = gmail.status()["email"] if gmail else None
        return {"reachable": True, **st, "sameAccount": bool(st.get("connected") and st.get("email") == mine), "sync": paypal_sync}

    @app.get("/admin/api/mail-status")
    @limiter.limit("30/minute")
    def mail_status(request: Request) -> dict:
        """For the sign-in page: before Gmail is connected nobody can get a code, so it offers to connect it."""
        return {"ready": bool(gmail and gmail.ready), "clientConfigured": bool(gmail and gmail.client_configured)}

    @app.get("/admin/api/gmail")
    def gmail_status(request: Request, admin: dict = Depends(viewer)) -> dict:
        if not gmail:
            raise HTTPException(503, "Gmail is not available")
        return {**gmail.status(), "redirectUri": redirect_uri(request), "paypal": paypal_gmail(admin)}

    @app.post("/admin/api/gmail/connect")
    def gmail_connect(request: Request, admin: dict = Depends(owner)) -> dict:
        if not gmail or not gmail.client_configured:
            raise HTTPException(409, "set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET on the server first")
        return {"url": gmail.start(redirect_uri(request), admin["email"])}

    @app.post("/admin/api/gmail/setup")
    @limiter.limit("5 per 10 minutes")
    def gmail_setup(request: Request) -> dict:
        """First connection, from the sign-in page: only while nothing is connected; the Google account must be an owner's."""
        if not gmail or not gmail.client_configured:
            raise HTTPException(409, "set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET on the server first")
        if gmail.ready:
            raise HTTPException(409, "Gmail is already connected; sign in with a code")
        return {"url": gmail.start(redirect_uri(request), None)}

    def paypal_note() -> str:
        if not billing:
            return ""
        return " (גם משרת ה-PayPal)" if paypal_sync["ok"] else " (שרת ה-PayPal לא עודכן: אפשר לנסות שוב מדף Gmail)"

    @app.get("/admin/api/gmail/callback", name="gmail_callback")
    @limiter.limit("20/minute")
    def gmail_callback(request: Request, state: str = "", code: str = "", error: str = "") -> HTMLResponse:
        if not gmail:
            return gmail_page("Gmail לא זמין")
        if error or not code or not state:
            return gmail_page("החיבור בוטל", "לא אושרה גישה בחשבון Google.")
        try:
            address, started_by, token = gmail.finish(state, code)
        except ValueError as e:
            return gmail_page("החיבור נכשל", str(e))
        except Exception:  # noqa: BLE001 - Google unreachable
            return gmail_page("החיבור נכשל", "Google לא ענה, נסה שוב.")
        if started_by:  # from the panel: still an owner?
            if db.admin_role(started_by) != "owner":
                gmail._revoke(token)
                return gmail_page("אין הרשאה", "רק בעלים יכולים לחבר את Gmail.")
            gmail.connect(address, token, started_by)
            db.audit(started_by, "gmail_connect", address, ip_of(request))
            sync_paypal(started_by)
            return gmail_page("Gmail חובר", f"המיילים יישלחו מ-{address}{paypal_note()}.", ok=True)
        # first connection from the sign-in page: Google proved who this is; they must be an owner
        if gmail.ready or db.admin_role(address) != "owner":
            gmail._revoke(token)
            return gmail_page("אין הרשאה", f"{address} אינו בעלים של הפאנל. התחבר עם חשבון Google של בעלים "
                                           "(KABALA_OWNER_EMAILS), או היכנס עם קוד וחבר מדף Gmail.")
        gmail.connect(address, token, address)
        db.audit(address, "gmail_connect", address, ip_of(request))
        sync_paypal(address)
        response = gmail_page("Gmail חובר", f"המיילים יישלחו מ-{address}{paypal_note()}, ונכנסת לפאנל.", ok=True)
        sign_in(request, response, address, "login_google")
        return response

    @app.post("/admin/api/gmail/disconnect")
    def gmail_disconnect(admin: dict = Depends(owner)) -> dict:
        if not gmail:
            raise HTTPException(503, "Gmail is not available")
        before = gmail.status()["email"]
        gmail.disconnect()
        db.audit(admin["email"], "gmail_disconnect", before, admin["ip"])
        sync_paypal(admin["email"])
        return gmail.status()

    @app.post("/admin/api/gmail/sync-paypal")
    def gmail_sync_paypal(admin: dict = Depends(owner)) -> dict:
        if not billing:
            raise HTTPException(404, "the PayPal server is not connected to the panel")
        sync_paypal(admin["email"])
        db.audit(admin["email"], "gmail_sync_paypal", gmail.status()["email"] if gmail else None, admin["ip"])
        if not paypal_sync["ok"]:
            raise HTTPException(502, paypal_sync["error"] or "failed")
        return {"ok": True, "paypal": paypal_gmail(admin)}

    @app.post("/admin/api/gmail/test-paypal")
    @limiter.limit("5/minute")
    def gmail_test_paypal(request: Request, admin: dict = Depends(admin_)) -> dict:
        if not billing:
            raise HTTPException(404, "the PayPal server is not connected to the panel")
        try:
            r = billing.post("/api/admin/gmail/test", admin["email"], admin["role"])
        except BillingRefused as e:
            raise HTTPException(409, str(e)) from e
        except (BillingUnavailable, BillingNotFound) as e:
            raise HTTPException(502, "the PayPal server is unreachable") from e
        db.audit(admin["email"], "gmail_test_paypal", admin["email"], admin["ip"])
        return r

    @app.post("/admin/api/gmail/test")
    @limiter.limit("5/minute")
    def gmail_test(request: Request, admin: dict = Depends(admin_)) -> dict:
        if not gmail or not gmail.ready:
            raise HTTPException(409, "Gmail is not connected")
        try:
            gmail.send(admin["email"], "בדיקת שליחה מ-Kabala / Kabala test email",
                       "המייל הזה נשלח דרך Gmail API מפאנל הניהול. אם הוא הגיע, השליחה עובדת.\n\n"
                       "This email was sent through the Gmail API from the admin panel.\n")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, gmail.last_error or type(e).__name__) from e
        db.audit(admin["email"], "gmail_test", admin["email"], admin["ip"])
        return {"ok": True, "to": admin["email"]}

    # ---- data ------------------------------------------------------------------------------

    @app.get("/admin/api/stats")
    def stats(admin: dict = Depends(viewer)) -> dict:
        return db.stats()

    @app.get("/admin/api/users")
    def users(q: str = "", plan: str = "", status: str = "", paid: bool = False, page: int = 1,
              admin: dict = Depends(viewer)) -> dict:
        full = admin["role"] != "viewer"
        if not full:
            q = ""  # searching by address would reveal masked emails one guess at a time
        size, page = 25, max(1, page)
        rows, total = db.user_rows(q[:100], plan, status, paid, size, (page - 1) * size)
        return {"users": [_user_out(r, full) for r in rows], "total": total, "page": page, "pages": max(1, -(-total // size))}

    @app.get("/admin/api/users/{user_id}")
    def user(user_id: int, admin: dict = Depends(viewer)) -> dict:
        row = db.user_row(user_id)
        if not row:
            raise HTTPException(404, "no such user")
        return _user_out(row, admin["role"] != "viewer")

    @app.post("/admin/api/users/{user_id}/comp")
    def comp(user_id: int, body: CompIn, admin: dict = Depends(admin_)) -> dict:
        row = db.user_row(user_id)
        if not row:
            raise HTTPException(404, "no such user")
        if row["subscription_status"] not in (None, "comp", "canceled"):
            raise HTTPException(409, "this user has a Stripe subscription; manage it in Stripe")
        db.set_subscription(user_id, None, "comp" if body.on else None, None)
        db.audit(admin["email"], "comp_on" if body.on else "comp_off", row["email"], admin["ip"])
        return _user_out(db.user_row(user_id), True)

    @app.get("/admin/api/users.csv")
    def export(plan: str = "", status: str = "", paid: bool = False, admin: dict = Depends(admin_)) -> StreamingResponse:
        rows, _ = db.user_rows("", plan, status, paid, 1_000_000, 0)
        out = io.StringIO()
        out.write("﻿")  # BOM: Excel opens UTF-8 correctly
        w = csv.writer(out)
        w.writerow(["email", "plan", "status", "renews", "signed_up", "last_seen", "stripe_customer"])
        for r in rows:
            w.writerow([csv_cell(v) for v in (r["email"], r["plan"], r["subscription_status"], r["current_period_end"],
                                              r["created_at"], r["last_seen_at"], r["stripe_customer_id"])])
        db.audit(admin["email"], "export_csv", f"{len(rows)} users", admin["ip"])
        return StreamingResponse(iter([out.getvalue()]), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": 'attachment; filename="kabala-users.csv"', "Cache-Control": "no-store"})

    # ---- admins (owner) --------------------------------------------------------------------

    @app.get("/admin/api/admins")
    def admins(admin: dict = Depends(owner)) -> dict:
        return {"admins": db.admins()}

    @app.post("/admin/api/admins")
    def add_admin(body: AdminIn, admin: dict = Depends(owner)) -> dict:
        email = email_login.normalize_email(body.email)
        if not email or body.role not in ROLES:
            raise HTTPException(400, "invalid email or role")
        if email == admin["email"] and body.role != "owner":
            raise HTTPException(409, "you can't remove your own owner role")
        db.set_admin(email, body.role, admin["email"])
        db.audit(admin["email"], f"admin_set:{body.role}", email, admin["ip"])
        return {"admins": db.admins()}

    @app.post("/admin/api/admins/remove")
    def remove_admin(body: EmailIn, admin: dict = Depends(owner)) -> dict:
        email = (email_login.normalize_email(body.email) or "").lower()
        current = {a["email"]: a["role"] for a in db.admins()}
        if email not in current:
            raise HTTPException(404, "not an admin")
        if email == admin["email"]:
            raise HTTPException(409, "you can't remove yourself")
        if current[email] == "owner" and sum(r == "owner" for r in current.values()) <= 1:
            raise HTTPException(409, "can't remove the last owner")
        db.remove_admin(email)
        db.audit(admin["email"], "admin_removed", email, admin["ip"])
        return {"admins": db.admins()}

    # ---- security & audit -------------------------------------------------------------------

    @app.get("/admin/api/security")
    def security(admin: dict = Depends(viewer)) -> dict:
        return {"bans": active_bans(jails, clock), "proxy_hops": config.proxy_hops, "https_required": config.require_https}

    @app.post("/admin/api/security/unban")
    def unban(body: IpIn, admin: dict = Depends(admin_)) -> dict:
        if not lift_ban(jails, body.ip):
            raise HTTPException(404, "not banned")
        db.audit(admin["email"], "unban", body.ip, admin["ip"])
        return {"bans": active_bans(jails, clock)}

    @app.get("/admin/api/audit")
    def audit(page: int = 1, admin: dict = Depends(admin_)) -> dict:
        size, page = 50, max(1, page)
        rows, total = db.audit_rows(size, (page - 1) * size)
        return {"entries": rows, "total": total, "page": page, "pages": max(1, -(-total // size))}

    # ---- PayPal billing server (../billing) ------------------------------------------------

    def paypal(path: str, admin: dict, params: dict | None = None, method: str = "GET", body: dict | None = None) -> dict:
        if not billing:
            raise HTTPException(404, "PayPal billing is not connected (KABALA_BILLING_URL, KABALA_BILLING_ADMIN_KEY)")
        try:
            if method == "POST":
                data = billing.post(path, admin["email"], admin["role"], body)
            else:
                data = billing.get(path, admin["email"], admin["role"], params)
        except BillingNotFound as e:
            raise HTTPException(404, "not found") from e
        except BillingRefused as e:
            raise HTTPException(409, str(e)) from e
        except BillingUnavailable as e:
            raise HTTPException(502, str(e)) from e
        if admin["role"] == "viewer":  # the billing server masks too; never rely on one side alone
            rows = data.get("subscribers", []) + data.get("payments", []) + data.get("keys", []) + data.get("receipts", [])
            if "emails" in data:
                rows.append(data)
            for row in rows:
                row["email"] = mask(row["email"]) if "***" not in row["email"] else row["email"]
            for log in data.get("emails", []) + [r["lastEmail"] for r in data.get("receipts", []) if r.get("lastEmail")]:
                log["to"] = ", ".join(a if "***" in a else mask(a) for a in log["to"].split(", "))
        return data

    @app.get("/admin/api/paypal/status")
    def paypal_status(admin: dict = Depends(viewer)) -> dict:
        return {"connected": billing is not None}

    @app.get("/admin/api/paypal/stats")
    def paypal_stats(admin: dict = Depends(viewer)) -> dict:
        return paypal("/api/admin/stats", admin)

    @app.get("/admin/api/paypal/subscribers")
    def paypal_subscribers(q: str = "", status: str = "", page: int = 1, admin: dict = Depends(viewer)) -> dict:
        return paypal("/api/admin/subscribers", admin, {"q": q[:100] if admin["role"] != "viewer" else "", "status": status, "page": max(1, page)})

    @app.get("/admin/api/paypal/payments")
    def paypal_payments(page: int = 1, admin: dict = Depends(viewer)) -> dict:
        return paypal("/api/admin/payments", admin, {"page": max(1, page)})

    @app.get("/admin/api/paypal/keys")
    def paypal_keys(q: str = "", status: str = "", page: int = 1, admin: dict = Depends(viewer)) -> dict:
        return paypal("/api/admin/keys", admin, {"q": q[:100] if admin["role"] != "viewer" else "", "status": status, "page": max(1, page)})

    @app.post("/admin/api/paypal/keys/{key_id}/revoke")
    def paypal_revoke_key(key_id: str, admin: dict = Depends(admin_)) -> dict:
        if not re.fullmatch(r"[0-9a-f-]{36}", key_id):
            raise HTTPException(400, "invalid key id")
        result = paypal(f"/api/admin/keys/{key_id}/revoke", admin, method="POST")
        db.audit(admin["email"], "key_revoked", key_id, admin["ip"])
        return result

    # ---- receipts (PDF + email the billing server sends after each payment) -----------------------------

    receipt_id = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    def check_id(rid: str) -> str:
        if not receipt_id.fullmatch(rid):
            raise HTTPException(400, "invalid receipt id")
        return rid

    @app.get("/admin/api/paypal/receipts")
    def paypal_receipts(q: str = "", status: str = "", page: int = 1, admin: dict = Depends(viewer)) -> dict:
        return paypal("/api/admin/receipts", admin, {"q": q[:100] if admin["role"] != "viewer" else "", "status": status, "page": max(1, page)})

    @app.get("/admin/api/paypal/receipts/{rid}")
    def paypal_receipt(rid: str, admin: dict = Depends(viewer)) -> dict:
        return paypal(f"/api/admin/receipts/{check_id(rid)}", admin)

    @app.get("/admin/api/paypal/receipts/{rid}/pdf")
    def paypal_receipt_pdf(rid: str, admin: dict = Depends(admin_)) -> Response:
        if not billing:
            raise HTTPException(404, "PayPal billing is not connected")
        try:
            pdf = billing.get_bytes(f"/api/admin/receipts/{check_id(rid)}/pdf", admin["email"], admin["role"])
        except BillingNotFound as e:
            raise HTTPException(404, "the PDF isn't ready") from e
        except BillingUnavailable as e:
            raise HTTPException(502, str(e)) from e
        db.audit(admin["email"], "receipt_downloaded", rid, admin["ip"])
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="kabala-receipt-{rid[:8]}.pdf"', "Cache-Control": "no-store"})

    @app.post("/admin/api/paypal/receipts/{rid}/send")
    def paypal_receipt_send(rid: str, body: SendIn, admin: dict = Depends(admin_)) -> dict:
        to = (body.to or "").strip()
        payload = {"to": to} if to else {}
        result = paypal(f"/api/admin/receipts/{check_id(rid)}/send", admin, method="POST", body=payload)
        db.audit(admin["email"], "receipt_sent", f"{rid} → {to or 'customer'}", admin["ip"])
        return result

    @app.post("/admin/api/paypal/receipts/{rid}/retry")
    def paypal_receipt_retry(rid: str, admin: dict = Depends(admin_)) -> dict:
        result = paypal(f"/api/admin/receipts/{check_id(rid)}/retry", admin, method="POST", body={})
        db.audit(admin["email"], "receipt_retry", rid, admin["ip"])
        return result

    # ---- "Automatic sending" settings of receipts (stored on the billing server) ----------------------

    def settings_body(body: ReceiptSettingsIn) -> dict:
        return body.model_dump(exclude_unset=True)

    def paypal_refusal_detail(path: str, admin: dict, body: dict) -> dict:
        """Like paypal(), but a 400 from billing (a broken template) keeps its message for the form."""
        try:
            return billing.post(path, admin["email"], admin["role"], body)
        except BillingRefused as e:
            raise HTTPException(400, str(e)) from e
        except BillingNotFound as e:
            raise HTTPException(404, "not found") from e
        except BillingUnavailable as e:
            raise HTTPException(502, str(e)) from e

    @app.get("/admin/api/paypal/receipt-settings")
    def receipt_settings(admin: dict = Depends(viewer)) -> dict:
        return paypal("/api/admin/receipt-settings", admin)

    @app.post("/admin/api/paypal/receipt-settings")
    def save_receipt_settings(body: ReceiptSettingsIn, admin: dict = Depends(admin_)) -> dict:
        if not billing:
            raise HTTPException(404, "PayPal billing is not connected")
        data = settings_body(body)
        data.pop("locale", None)
        result = paypal_refusal_detail("/api/admin/receipt-settings", admin, data)
        db.audit(admin["email"], "receipt_settings", ", ".join(sorted(data)), admin["ip"])
        return result

    @app.post("/admin/api/paypal/receipt-settings/preview")
    def preview_receipt_settings(body: ReceiptSettingsIn, admin: dict = Depends(viewer)) -> dict:
        if not billing:
            raise HTTPException(404, "PayPal billing is not connected")
        return paypal_refusal_detail("/api/admin/receipt-settings/preview", admin, settings_body(body))

    @app.post("/admin/api/paypal/receipt-settings/test")
    def test_receipt_settings(body: ReceiptSettingsIn, admin: dict = Depends(admin_)) -> dict:
        if not billing:
            raise HTTPException(404, "PayPal billing is not connected")
        data = settings_body(body)
        data.pop("locale", None)
        result = paypal_refusal_detail("/api/admin/receipt-settings/test", admin, data)
        db.audit(admin["email"], "receipt_test", admin["email"], admin["ip"])
        return result

    # ---- Stripe receipts (receipts.py, this server): the same shapes as the PayPal ones above -------------

    def stripe_receipts():
        if receipts is None:
            raise HTTPException(404, "receipts are not set up")
        return receipts

    def receipt_out(r: dict, full: bool, last: dict | None = None) -> dict:
        def recipients(to: str) -> str:
            return ", ".join(a if full else mask(a) for a in to.split(","))
        out = {
            "id": str(r["id"]), "kind": "receipt", "number": str(r["number"]),
            "email": r["customer_email"] if full else mask(r["customer_email"]),
            "amountMinor": r["amount_minor"], "currency": r["currency"], "status": r["status"],
            "issuedAt": r["issued_at"], "sentAt": r["sent_at"], "attempts": r["attempts"],
            "lastError": r["last_error"] if full else None,
        }
        if last is not None:
            out["lastEmail"] = {"status": last["status"], "trigger": last["trigger"], "to": recipients(last["to_addresses"]),
                                "at": last["updated_at"]} if last else None
        return out

    def email_out(e: dict, full: bool) -> dict:
        return {"status": e["status"], "trigger": e["trigger"], "to": ", ".join(a if full else mask(a) for a in e["to_addresses"].split(",")),
                "attempts": e["attempts"], "error": e["error"] if full else None, "createdAt": e["created_at"], "updatedAt": e["updated_at"]}

    def receipt_id_int(rid: str) -> int:
        if not rid.isdigit():
            raise HTTPException(400, "invalid receipt id")
        return int(rid)

    @app.get("/admin/api/stripe/receipts")
    def stripe_receipt_list(q: str = "", status: str = "", page: int = 1, admin: dict = Depends(viewer)) -> dict:
        rc, full, page = stripe_receipts(), admin["role"] != "viewer", max(1, page)
        rows, total = rc.list(q[:100] if full else "", status, page)
        out = []
        for r in rows:
            emails = rc.emails(r["id"])
            out.append(receipt_out(r, full, emails[0] if emails else {}))
        return {"receipts": out, "total": total, "page": page, "pages": max(1, -(-total // 25))}

    @app.get("/admin/api/stripe/receipts/{rid}")
    def stripe_receipt(rid: str, admin: dict = Depends(viewer)) -> dict:
        rc, full = stripe_receipts(), admin["role"] != "viewer"
        r = rc.get(receipt_id_int(rid))
        if not r:
            raise HTTPException(404, "not found")
        return {**receipt_out(r, full), "emails": [email_out(e, full) for e in rc.emails(r["id"])]}

    @app.get("/admin/api/stripe/receipts/{rid}/pdf")
    def stripe_receipt_pdf(rid: str, admin: dict = Depends(admin_)) -> Response:
        rc = stripe_receipts()
        r = rc.get(receipt_id_int(rid))
        if not r or not r["pdf"]:
            raise HTTPException(404, "the PDF isn't ready")
        db.audit(admin["email"], "receipt_downloaded", f"stripe {r['number']}", admin["ip"])
        return Response(rc.pdf_bytes(r), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="kabala-receipt-{r["number"]}.pdf"', "Cache-Control": "no-store"})

    @app.post("/admin/api/stripe/receipts/{rid}/send")
    def stripe_receipt_send(rid: str, body: SendIn, admin: dict = Depends(admin_)) -> dict:
        to = (body.to or "").strip() or None
        result = stripe_receipts().admin_send(receipt_id_int(rid), to)
        if result != "queued":
            raise HTTPException(404 if result == "not_found" else 409, result)
        db.audit(admin["email"], "receipt_sent", f"stripe {rid} → {to or 'customer'}", admin["ip"])
        return {"ok": True}

    @app.post("/admin/api/stripe/receipts/{rid}/retry")
    def stripe_receipt_retry(rid: str, admin: dict = Depends(admin_)) -> dict:
        result = stripe_receipts().retry(receipt_id_int(rid))
        if result != "queued":
            raise HTTPException(404 if result == "not_found" else 409, result)
        db.audit(admin["email"], "receipt_retry", f"stripe {rid}", admin["ip"])
        return {"ok": True}

    def stripe_settings_out(admin: dict) -> dict:
        rc = stripe_receipts()
        s = rc.settings()
        if s["copyTo"] and admin["role"] == "viewer":
            s["copyTo"] = mask(s["copyTo"])
        return {**s, **rc.meta()}

    @app.get("/admin/api/stripe/receipt-settings")
    def stripe_settings(admin: dict = Depends(viewer)) -> dict:
        return stripe_settings_out(admin)

    @app.post("/admin/api/stripe/receipt-settings")
    def stripe_save_settings(body: ReceiptSettingsIn, admin: dict = Depends(admin_)) -> dict:
        data = settings_body(body)
        data.pop("locale", None)
        try:
            stripe_receipts().update_settings(data, admin["email"])
        except TemplateError as e:
            raise HTTPException(400, str(e)) from e
        db.audit(admin["email"], "receipt_settings", "stripe: " + ", ".join(sorted(data)), admin["ip"])
        return stripe_settings_out(admin)

    @app.post("/admin/api/stripe/receipt-settings/preview")
    def stripe_preview(body: ReceiptSettingsIn, admin: dict = Depends(viewer)) -> dict:
        data = settings_body(body)
        locale = data.pop("locale", None) or "he"
        try:
            return stripe_receipts().preview(data, locale)
        except TemplateError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/admin/api/stripe/receipt-settings/test")
    def stripe_test(body: ReceiptSettingsIn, admin: dict = Depends(admin_)) -> dict:
        data = settings_body(body)
        data.pop("locale", None)
        try:
            result = stripe_receipts().send_test(admin["email"], data)
        except TemplateError as e:
            raise HTTPException(400, str(e)) from e
        if result != "queued":
            raise HTTPException(409, result)
        db.audit(admin["email"], "receipt_test", f"stripe → {admin['email']}", admin["ip"])
        return {"ok": True, "to": admin["email"]}
