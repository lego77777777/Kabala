"""
HTTP API used by the desktop app.

POST /api/session        Google ID token → Kabala session token (identity proven by Google, no passwords)
GET  /api/me             plan and subscription status
POST /api/checkout       Stripe Checkout URL for the Pro subscription
POST /api/portal         Stripe Customer Portal URL (change card, cancel)
POST /api/stripe/webhook subscription lifecycle from Stripe
GET  /api/version        latest app version + download link
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from kabala_server import tokens
from kabala_server.config import Config
from kabala_server.db import Database, User

log = logging.getLogger("kabala_server")

FREE_MONTHLY_RECEIPTS = 50


def google_verifier(client_id: str) -> Callable[[str], dict]:
    """Verifies a Google ID token's signature, expiry and audience (our OAuth client)."""
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token

    def verify(token: str) -> dict:
        return id_token.verify_oauth2_token(token, g_requests.Request(), audience=client_id)

    return verify


class SessionIn(BaseModel):
    id_token: str


def _plan_payload(user: User) -> dict[str, Any]:
    return {
        "email": user.email,
        "plan": user.plan,
        "status": user.subscription_status,
        "current_period_end": user.current_period_end,
        "limits": {"monthly_receipts": None if user.plan == "pro" else FREE_MONTHLY_RECEIPTS,
                   "export": user.plan == "pro", "background_scan": user.plan == "pro", "accounts": None if user.plan == "pro" else 1},
    }


def _period_end(sub: dict) -> int | None:
    # Newer Stripe API versions moved current_period_end onto subscription items.
    if sub.get("current_period_end"):
        return int(sub["current_period_end"])
    items = (sub.get("items") or {}).get("data") or []
    return int(items[0]["current_period_end"]) if items and items[0].get("current_period_end") else None


def create_app(config: Config | None = None, verify_google: Callable[[str], dict] | None = None, stripe_client: Any = None) -> FastAPI:
    config = config or Config()
    config.check()
    db = Database(config.db_path)
    verify_google = verify_google or google_verifier(config.google_client_id)
    if stripe_client is None and config.stripe_enabled:
        import stripe

        stripe_client = stripe.StripeClient(config.stripe_secret_key)

    app = FastAPI(title="Kabala API", docs_url=None, redoc_url=None)

    def current_user(authorization: str = Header(default="")) -> User:
        token = authorization.removeprefix("Bearer ").strip()
        payload = tokens.verify(config.secret, token) if token else None
        user = db.by_id(payload["uid"]) if payload else None
        if not user:
            raise HTTPException(401, "invalid or expired session")
        return user

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/version")
    def version() -> dict:
        return {"latest": config.latest_version, "url": config.download_url}

    @app.post("/api/session")
    def session(body: SessionIn) -> dict:
        try:
            info = verify_google(body.id_token)
        except Exception as e:  # noqa: BLE001 - any verification failure is a 401
            log.info("rejected id token: %s", e)
            raise HTTPException(401, "invalid Google ID token") from e
        if not info.get("email") or not info.get("email_verified", False):
            raise HTTPException(401, "email not verified by Google")
        user = db.get_or_create(info["email"])
        return {"token": tokens.issue(config.secret, user.id, user.email, config.session_days), **_plan_payload(user)}

    @app.get("/api/me")
    def me(user: User = Depends(current_user)) -> dict:
        return _plan_payload(user)

    @app.post("/api/checkout")
    def checkout(user: User = Depends(current_user)) -> dict:
        if not stripe_client:
            raise HTTPException(503, "payments are not configured")
        if user.plan == "pro":
            raise HTTPException(409, "already on Pro")
        params: dict[str, Any] = {
            "mode": "subscription",
            "line_items": [{"price": config.stripe_price_pro, "quantity": 1}],
            "client_reference_id": str(user.id),
            "success_url": config.public_url.rstrip("/") + "/thanks.html",
            "cancel_url": config.public_url,
            "allow_promotion_codes": True,
        }
        if user.stripe_customer_id:
            params["customer"] = user.stripe_customer_id
        else:
            params["customer_email"] = user.email
        session_obj = stripe_client.v1.checkout.sessions.create(params=params)
        return {"url": session_obj["url"]}

    @app.post("/api/portal")
    def portal(user: User = Depends(current_user)) -> dict:
        if not stripe_client:
            raise HTTPException(503, "payments are not configured")
        if not user.stripe_customer_id:
            raise HTTPException(409, "no subscription yet")
        s = stripe_client.v1.billing_portal.sessions.create(params={"customer": user.stripe_customer_id, "return_url": config.public_url})
        return {"url": s["url"]}

    @app.post("/api/stripe/webhook")
    async def webhook(request: Request, stripe_signature: str = Header(default="")) -> dict:
        if not stripe_client:
            raise HTTPException(503, "payments are not configured")
        payload = await request.body()
        try:
            event = stripe_client.construct_event(payload, stripe_signature, config.stripe_webhook_secret)
        except Exception as e:  # noqa: BLE001 - bad signature or payload
            raise HTTPException(400, "invalid signature") from e
        if not db.first_time_event(event["id"]):
            return {"ok": True, "duplicate": True}

        kind = event["type"]
        obj = event["data"]["object"]
        if kind == "checkout.session.completed":
            user = db.by_id(int(obj.get("client_reference_id") or 0))
            if user and obj.get("customer"):
                db.link_customer(user.id, obj["customer"])
                if obj.get("subscription"):
                    sub = stripe_client.v1.subscriptions.retrieve(obj["subscription"])
                    db.set_subscription(user.id, sub["id"], sub["status"], _period_end(sub))
        elif kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
            user = db.by_customer(obj["customer"])
            if user:
                status = "canceled" if kind == "customer.subscription.deleted" else obj["status"]
                db.set_subscription(user.id, obj["id"], status, _period_end(obj))
        return {"ok": True}

    return app
