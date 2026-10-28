from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    """All settings come from environment variables (see server/README.md)."""

    # repr=False: secrets never show up in a printed config, a traceback or a debugger
    secret: str = field(default_factory=lambda: os.environ.get("KABALA_SECRET", ""), repr=False)  # encryption key (crypto.py)
    # previous KABALA_SECRET values after a rotation (comma-separated): still decrypt, never encrypt
    old_secrets: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip() for s in os.environ.get("KABALA_OLD_SECRETS", "").split(",") if s.strip()), repr=False)
    google_client_id: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLIENT_ID", ""))
    stripe_secret_key: str = field(default_factory=lambda: os.environ.get("STRIPE_SECRET_KEY", ""), repr=False)
    stripe_price_pro: str = field(default_factory=lambda: os.environ.get("STRIPE_PRICE_PRO", ""))
    stripe_webhook_secret: str = field(default_factory=lambda: os.environ.get("STRIPE_WEBHOOK_SECRET", ""), repr=False)
    public_url: str = field(default_factory=lambda: os.environ.get("PUBLIC_URL", "https://serewf191-tech.github.io/kabala/"))
    db_path: str = field(default_factory=lambda: os.environ.get("KABALA_DB", "kabala_server.db"))
    latest_version: str = field(default_factory=lambda: os.environ.get("LATEST_VERSION", "0.1.0"))
    download_url: str = field(default_factory=lambda: os.environ.get("DOWNLOAD_URL", "https://serewf191-tech.github.io/kabala/"))
    session_days: int = 30
    # sign-in by email code (kabala_server/email_login.py); off without SMTP_URL
    smtp_url: str = field(default_factory=lambda: os.environ.get("SMTP_URL", ""), repr=False)
    # admin panel (/admin): addresses that are always owners, comma-separated; more admins are added in the panel
    owner_emails: tuple[str, ...] = field(default_factory=lambda: tuple(
        e.strip().lower() for e in os.environ.get("KABALA_OWNER_EMAILS", "").split(",") if e.strip()))
    # admin panel ↔ PayPal billing server (admin_billing.py); both empty = the panel shows Stripe only
    billing_url: str = field(default_factory=lambda: os.environ.get("KABALA_BILLING_URL", ""))
    billing_admin_key: str = field(default_factory=lambda: os.environ.get("KABALA_BILLING_ADMIN_KEY", ""), repr=False)
    # sending through the Gmail API instead of SMTP (gmail_api.py); all three set = used for every email
    gmail_client_id: str = field(default_factory=lambda: os.environ.get("GMAIL_CLIENT_ID", ""))
    gmail_client_secret: str = field(default_factory=lambda: os.environ.get("GMAIL_CLIENT_SECRET", ""), repr=False)
    gmail_refresh_token: str = field(default_factory=lambda: os.environ.get("GMAIL_REFRESH_TOKEN", ""), repr=False)
    mail_from: str = field(default_factory=lambda: os.environ.get("MAIL_FROM", "Kabala <no-reply@kabala.app>"))
    # anti-DDoS (kabala_server/shield.py)
    # automatic receipts after Stripe payments (receipts.py); the seller is shown on every receipt
    seller_name: str = field(default_factory=lambda: os.environ.get("SELLER_NAME", "Kabala"))
    seller_tax_id: str = field(default_factory=lambda: os.environ.get("SELLER_TAX_ID", ""))  # ע.מ. / ח.פ.
    seller_address: str = field(default_factory=lambda: os.environ.get("SELLER_ADDRESS", ""))
    seller_email: str = field(default_factory=lambda: os.environ.get("SELLER_EMAIL", ""))
    business_type: str = field(default_factory=lambda: os.environ.get("BUSINESS_TYPE", "exempt"))  # exempt | licensed
    vat_rate_percent: float = field(default_factory=lambda: float(os.environ.get("VAT_RATE_PERCENT", "18")))
    # Its own number range, so Stripe and PayPal receipts never share a number (confirm with your accountant)
    receipt_number_start: int = field(default_factory=lambda: int(os.environ.get("RECEIPT_NUMBER_START", "500001")))
    receipt_copy_to: str = field(default_factory=lambda: os.environ.get("RECEIPT_COPY_TO", ""))
    admin_email: str = field(default_factory=lambda: os.environ.get("ADMIN_EMAIL", ""))  # alerted when a receipt fails
    shield_enabled: bool = field(default_factory=lambda: os.environ.get("KABALA_SHIELD", "1") != "0")
    proxy_hops: int = field(default_factory=lambda: int(os.environ.get("KABALA_PROXY_HOPS", "0")))
    allow_ips: frozenset[str] = field(default_factory=lambda: frozenset(
        ip.strip() for ip in os.environ.get("KABALA_ALLOW_IPS", "").split(",") if ip.strip()))
    # behind a proxy (KABALA_PROXY_HOPS > 0) only https requests are served, and browsers are told to stay on https
    require_https: bool = field(default_factory=lambda: os.environ.get("KABALA_REQUIRE_HTTPS", "1") != "0")
    rate_limit_per_minute: int = field(default_factory=lambda: int(os.environ.get("KABALA_RATE_LIMIT", "120")))

    def check(self) -> None:
        missing = [n for n, v in [("KABALA_SECRET", self.secret), ("GOOGLE_CLIENT_ID", self.google_client_id)] if not v]
        if missing:
            raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
        if len(self.secret) < 32:
            raise RuntimeError("KABALA_SECRET must be at least 32 characters")
        gmail = {"GMAIL_CLIENT_ID": self.gmail_client_id, "GMAIL_CLIENT_SECRET": self.gmail_client_secret,
                 "GMAIL_REFRESH_TOKEN": self.gmail_refresh_token}
        if any(gmail.values()) and not all(gmail.values()):
            raise RuntimeError(f"Gmail API needs all of: {', '.join(n for n, v in gmail.items() if not v)}")

    @property
    def gmail_enabled(self) -> bool:
        return bool(self.gmail_client_id and self.gmail_client_secret and self.gmail_refresh_token)

    @property
    def secrets(self) -> tuple[str, ...]:
        """The current key first (encrypts and decrypts), then old ones (decrypt only)."""
        return (self.secret, *self.old_secrets)

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_price_pro)
