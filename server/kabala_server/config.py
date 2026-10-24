from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    """All settings come from environment variables (see server/README.md)."""

    secret: str = field(default_factory=lambda: os.environ.get("KABALA_SECRET", ""))  # signs session tokens
    google_client_id: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLIENT_ID", ""))
    stripe_secret_key: str = field(default_factory=lambda: os.environ.get("STRIPE_SECRET_KEY", ""))
    stripe_price_pro: str = field(default_factory=lambda: os.environ.get("STRIPE_PRICE_PRO", ""))
    stripe_webhook_secret: str = field(default_factory=lambda: os.environ.get("STRIPE_WEBHOOK_SECRET", ""))
    public_url: str = field(default_factory=lambda: os.environ.get("PUBLIC_URL", "https://serewf191-tech.github.io/kabala/"))
    db_path: str = field(default_factory=lambda: os.environ.get("KABALA_DB", "kabala_server.db"))
    latest_version: str = field(default_factory=lambda: os.environ.get("LATEST_VERSION", "0.1.0"))
    download_url: str = field(default_factory=lambda: os.environ.get("DOWNLOAD_URL", "https://serewf191-tech.github.io/kabala/"))
    session_days: int = 30

    def check(self) -> None:
        missing = [n for n, v in [("KABALA_SECRET", self.secret), ("GOOGLE_CLIENT_ID", self.google_client_id)] if not v]
        if missing:
            raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
        if len(self.secret) < 32:
            raise RuntimeError("KABALA_SECRET must be at least 32 characters")

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_price_pro)
