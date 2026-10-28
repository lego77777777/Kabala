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
import io
import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from kabala_server import email_login
import re

from kabala_server.admin_billing import BillingClient, BillingNotFound, BillingUnavailable
from kabala_server.config import Config
from kabala_server.crypto import box
from kabala_server.db import ROLES, Database
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
            jails: OrderedDict, clock: Callable[[], float] = time.monotonic, billing: BillingClient | None = None) -> None:
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
        if not mailer:
            raise HTTPException(503, "email is not configured (SMTP_URL)")
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
        token = sessions.encrypt(json.dumps({"email": email, "exp": int(time.time()) + SESSION_S}))
        response.set_cookie(COOKIE, token, max_age=SESSION_S, httponly=True, samesite="strict", path="/admin",
                            secure=request.url.scheme == "https" or config.proxy_hops > 0)
        db.audit(email, "login", None, ip_of(request))
        return {"email": email, "role": role}

    @app.post("/admin/api/logout")
    def logout(response: Response, admin: dict = Depends(viewer)) -> dict:
        response.delete_cookie(COOKIE, path="/admin")
        db.audit(admin["email"], "logout", None, admin["ip"])
        return {"ok": True}

    @app.get("/admin/api/me")
    def me(admin: dict = Depends(viewer)) -> dict:
        return {"email": admin["email"], "role": admin["role"]}

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

    def paypal(path: str, admin: dict, params: dict | None = None, method: str = "GET") -> dict:
        if not billing:
            raise HTTPException(404, "PayPal billing is not connected (KABALA_BILLING_URL, KABALA_BILLING_ADMIN_KEY)")
        try:
            if method == "POST":
                data = billing.post(path, admin["email"], admin["role"])
            else:
                data = billing.get(path, admin["email"], admin["role"], params)
        except BillingNotFound as e:
            raise HTTPException(404, "not found") from e
        except BillingUnavailable as e:
            raise HTTPException(502, str(e)) from e
        if admin["role"] == "viewer":  # the billing server masks too; never rely on one side alone
            for row in data.get("subscribers", []) + data.get("payments", []) + data.get("keys", []):
                row["email"] = mask(row["email"]) if "***" not in row["email"] else row["email"]
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
