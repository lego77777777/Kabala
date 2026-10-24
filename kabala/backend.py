"""
App state and long-running work, exposed to the UI through Qt signals.

A signed-in Gmail account gets the real scanner (kabala/scanner.py); demo mode keeps the simulated
mailbox so the app can be tried without Google setup.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from kabala import demo, google_auth, imap_gmail, licensing
from kabala.google_auth import AuthError
from kabala.models import Account, Receipt, ReviewItem, ScanOptions, ScanProgress, summarize
from kabala.settings import Settings, config_dir
from kabala.store import Store

log = logging.getLogger(__name__)


class Backend(QObject):
    connected = Signal(object)  # Account
    connect_failed = Signal(str)
    progress = Signal(object)  # ScanProgress
    scan_finished = Signal(object)  # ScanSummary
    scan_failed = Signal(str)  # error code, see i18n scan.errors
    data_changed = Signal()
    plan_changed = Signal(object)  # licensing.Plan
    open_url = Signal(str)  # subscription (portal) page to open in the browser
    open_checkout = Signal(str)  # checkout page, opened in Kabala's own payment window
    billing_failed = Signal(str)

    def __init__(self, settings: Settings | None = None, store: Store | None = None, license_url: str | None = "auto") -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.store = store or Store(config_dir() / "kabala.db")
        cfg = licensing.server_config() if license_url == "auto" else ({"url": license_url, "provider": "stripe"} if license_url else None)
        from kabala.paypal_billing import make_client

        self.billing_provider = cfg["provider"] if cfg else None
        self.license = make_client(cfg["url"], cfg["provider"]) if cfg else None
        # Prices shown in the app come only from the server's /api/pricing (PayPal billing).
        self.pricing: dict | None = None
        self.plan: licensing.Plan = licensing.UNMANAGED if not self.license else (self.license.cached() or licensing.FREE)
        self.account: Account | None = None
        self.receipts: list[Receipt] = []
        self.review: list[ReviewItem] = []
        self.scanning = False
        self.background = False  # the current/last scan was started by the schedule, not the user
        self.last_progress: ScanProgress | None = None
        self._cancel = threading.Event()
        self._cancel_auth = threading.Event()
        self.credentials = None  # google.oauth2.credentials.Credentials once signed in
        self.imap_password: str | None = None  # Gmail app password when connected over IMAP
        # who just signed out (for the welcome screen's confirmation and the app-password reminder)
        self.signed_out_email: str | None = None
        self.signed_out_provider: str | None = None
        self._auth_thread: threading.Thread | None = None
        self._restore_session()
        if self.license and self.account and not self.account.demo:
            self.refresh_plan()

    def usage_this_month(self) -> int:
        """
        What the Free plan's 50/month limit counts: receipts *saved* this calendar month
        (so a first scan of last year's mail is capped, and the next month starts fresh).
        """
        if self.account and not self.account.demo:
            return self.store.saved_this_month(self.account.email)
        today = date.today()
        prefix = f"{today.year}-{today.month:02d}"
        return sum(1 for r in self.receipts if r.date.startswith(prefix))

    def monthly_quota(self) -> int | None:
        """How many more receipts may be saved this month (None = unlimited)."""
        limit = self.plan.monthly_receipts
        return None if limit is None else max(0, limit - self.usage_this_month())

    # ---- plan & billing -------------------------------------------------------

    def _set_plan(self, plan: licensing.Plan) -> None:
        self.plan = plan
        self.plan_changed.emit(plan)

    def refresh_plan(self) -> None:
        if not self.license:
            return

        def run() -> None:
            # A fresh Google ID token (re)establishes the plan-server session when needed.
            if not self.license.token and self.credentials is not None:
                self._license_login()
            if hasattr(self.license, "pricing"):
                self.pricing = self.license.pricing() or self.pricing
            self._set_plan(self.license.status())

        threading.Thread(target=run, daemon=True).start()

    def load_pricing_once(self) -> None:
        """Fetch /api/pricing for the Plan screen (once per launch, so a failed fetch can't loop)."""
        if getattr(self, "_pricing_requested", False) or not self.license or not hasattr(self.license, "pricing"):
            return
        self._pricing_requested = True

        def run() -> None:
            self.pricing = self.license.pricing()
            if self.pricing:
                self.plan_changed.emit(self.plan)  # re-render with prices

        threading.Thread(target=run, daemon=True).start()

    def handle_deep_link(self, url: str) -> None:
        """
        kabala://payment-success → the browser just finished a payment: refresh the plan now.
        kabala://upgrade         → "Upgrade" on the website's pricing page: open checkout from the app.
        """
        log.info("deep link received: %s", url.split("?")[0])
        if url.lower().startswith("kabala://upgrade"):
            self.start_checkout()
            return
        self.refresh_plan()

    def _license_login(self) -> None:
        creds = self.credentials
        try:
            if not getattr(creds, "id_token", None):
                google_auth.refresh(creds)  # Google returns a new ID token with each refresh (openid scope)
            if getattr(creds, "id_token", None):
                self._set_plan(self.license.login(creds.id_token))
        except (AuthError, licensing.LicenseError, Exception) as e:  # noqa: BLE001 - stay on cached/free plan
            log.warning("plan server sign-in failed: %s", e)

    def start_checkout(self) -> None:
        self._billing_url(lambda: self.license.checkout_url(), self.open_checkout)

    def open_billing_portal(self) -> None:
        self._billing_url(lambda: self.license.portal_url(), self.open_url)

    def _billing_url(self, get, target) -> None:
        if not self.license:
            self.billing_failed.emit("not_configured")
            return
        imap = self.account is not None and self.account.provider == "imap"
        if not self.license.token and not imap and (self.credentials is None or self.account is None or self.account.demo):
            # The subscription is tied to the Google account; there is nobody to bill in demo mode.
            self.billing_failed.emit("sign_in")
            return

        def run() -> None:
            try:
                if not self.license.token and self.credentials is not None:
                    self._license_login()
                elif not self.license.token and imap:
                    # IMAP accounts have no Google token: a basic "who are you" sign-in (email only,
                    # no Gmail access, so no unverified-app screen) ties the subscription to the account.
                    self._set_plan(self.license.login(google_auth.identity_token(threading.Event())))
                target.emit(get())
            except AuthError as e:
                log.warning("identity sign-in for billing failed: %s", e.code)
                self.billing_failed.emit("sign_in" if e.code in ("canceled", "denied", "timeout") else "network")
                return
            except Exception as e:  # noqa: BLE001
                log.warning("billing request failed: %s", e)
                self.billing_failed.emit("network")

        threading.Thread(target=run, daemon=True).start()

    def _load(self) -> None:
        if self.account and not self.account.demo:
            self.receipts = self.store.receipts(self.account.email)
            self.review = self.store.review_items(self.account.email)

    # ---- account ------------------------------------------------------------

    @staticmethod
    def has_google_client() -> bool:
        try:
            return google_auth.ensure_client_config()  # also picks up a client_secret_*.json just saved to Downloads
        except AuthError as e:
            log.warning("Downloaded Google client file rejected: %s", e.code)
            return False

    @staticmethod
    def import_google_client(path: str) -> str:
        """Returns '' on success, or an error code for the UI."""
        try:
            google_auth.import_client_file(Path(path))
            return ""
        except AuthError as e:
            log.warning("Google client file %s rejected: %s", path, e.code)
            return e.code

    def _restore_session(self) -> None:
        """Signs back in silently from the OS vault. The token is verified on first use."""
        stored = google_auth.load_stored()
        if stored:
            email, self.credentials = stored
            self.account = Account(email=email, provider="gmail")
            self._load()
        elif imap := imap_gmail.load_login():
            email, self.imap_password = imap
            self.account = Account(email=email, provider="imap")
            self._load()

    def connect_account(self, provider: str = "gmail") -> None:
        """Opens Google's consent page in the system browser; result arrives via connected/connect_failed."""
        if self._auth_thread and self._auth_thread.is_alive() and not self._cancel_auth.is_set():
            return  # already waiting for the browser: a second click must not open a second consent page
        self._cancel_auth = threading.Event()  # a fresh event, so a canceled flow that is still exiting stays canceled
        self._auth_thread = threading.Thread(target=self._run_sign_in, args=(self._cancel_auth,), daemon=True)
        self._auth_thread.start()

    def connect_imap(self, address: str, app_password: str) -> None:
        """
        Gmail over IMAP with an app password: no Google consent screen at all. Checks the login first,
        then keeps the password in the OS vault. Result arrives via connected/connect_failed.
        """

        app_password = imap_gmail.normalize_app_password(app_password)

        def run() -> None:
            from kabala.gmail import GmailError

            try:
                imap_gmail.ImapGmailClient(address, app_password).close()
                imap_gmail.save_login(address, app_password)
            except GmailError as e:
                log.warning("IMAP sign-in failed: %s", e.code)
                self.connect_failed.emit({"auth": "imap_auth", "app_password": "imap_app_password"}.get(e.code, "network"))
                return
            log.info("IMAP sign-in succeeded")
            self.credentials = None
            self.imap_password = app_password
            self.account = Account(email=address, provider="imap")
            self._load()
            self.connected.emit(self.account)

        threading.Thread(target=run, daemon=True).start()

    def _run_sign_in(self, cancel: threading.Event) -> None:
        try:
            result = google_auth.sign_in(cancel)
        except AuthError as e:
            log.warning("Gmail sign-in failed: %s %s", e.code, e if str(e) != e.code else "")
            if e.code != "canceled":
                self.connect_failed.emit(e.code)
            return
        log.info("Gmail sign-in succeeded")
        if cancel.is_set():  # user gave up in the app while the browser was finishing
            google_auth.forget()
            return
        self.credentials = result.credentials
        self.account = Account(email=result.email, provider="gmail")
        self._load()
        self.connected.emit(self.account)
        if self.license:
            self._license_login()  # already on a worker thread

    def connect_demo(self) -> None:
        self.account = Account(email="demo@kabala.app", provider="gmail", demo=True)
        self.receipts, self.review = [], []
        self.connected.emit(self.account)

    def cancel_connect(self) -> None:
        self._cancel_auth.set()

    def _forget_account(self) -> None:
        # stop anything still using this account's access: a running scan, a sign-in in the browser
        self._cancel.set()
        self._cancel_auth.set()
        was_real = self.account is not None and not self.account.demo
        was_imap = was_real and self.account.provider == "imap"
        self.signed_out_email = self.account.email if was_real else None
        self.signed_out_provider = self.account.provider if was_real else None
        self.account = None
        self.credentials = None
        self.imap_password = None
        self.receipts, self.review = [], []
        if self.license:
            self.license.logout()
            self.plan = licensing.FREE
        if was_real:
            log.info("signed out (%s)", self.signed_out_provider)
        if was_imap:
            imap_gmail.forget_login()  # the app password itself is revoked by the user in their Google account
        elif was_real:
            # The vault entry goes now; only the network call to Google runs in the background, with the
            # token in hand, so it can't touch a grant from a sign-in that happens meanwhile.
            token = google_auth.take_stored_token()
            threading.Thread(target=google_auth.revoke, args=(token,), daemon=True).start()

    def disconnect_account(self) -> None:
        """Signs out. The index and saved PDFs stay, so reconnecting doesn't rescan everything."""
        self._forget_account()
        self.data_changed.emit()

    def wipe_all(self) -> None:
        """Signs out, revokes access and deletes the local index. Saved PDFs in the user's folder stay."""
        self._forget_account()
        self.store.wipe()
        self.data_changed.emit()

    # ---- scanning -----------------------------------------------------------

    def last_scan_at(self) -> datetime | None:
        if not self.account or self.account.demo:
            return None
        raw = self.store.get_meta(self.account.email, "last_scan_at")
        return datetime.fromisoformat(raw) if raw else None

    def auto_scan_due(self) -> bool:
        """True when background scanning is on and the interval since the last scan has passed."""
        from kabala.autoscan import is_due

        if not self.settings.auto_scan or not self.plan.background_scan:
            return False
        if self.scanning or not self.account or self.account.demo:
            return False
        return is_due(self.last_scan_at(), datetime.now(), self.settings.auto_interval)

    def start_scan(self, options: ScanOptions, background: bool = False) -> None:
        if self.scanning or self.account is None:
            return
        self.background = background
        try:
            Path(options.folder).mkdir(parents=True, exist_ok=True)
        except OSError:
            self.scan_failed.emit("folder")
            return
        self.scanning = True
        self.last_progress = None
        self._cancel.clear()
        target = self._run_demo_scan if self.account.demo else self._run_real_scan
        threading.Thread(target=target, args=(options,), daemon=True).start()

    def cancel_scan(self) -> None:
        self._cancel.set()

    def _on_progress(self, p: ScanProgress) -> None:
        self.last_progress = p
        self.progress.emit(p)  # queued to the UI thread by Qt

    def _fail(self, code: str) -> None:
        self.scanning = False
        self.scan_failed.emit(code)

    def _make_scanner(self, threshold: float):
        from google.auth.transport.requests import AuthorizedSession

        from kabala.detect.llm import make_classifier
        from kabala.detect.pipeline import Detector
        from kabala.gmail import GmailClient
        from kabala.scanner import Scanner

        detector = Detector(make_classifier(self.settings.provider), threshold)
        if self.account.provider == "imap":
            address, password = self.account.email, self.imap_password or ""
            make = lambda: imap_gmail.ImapGmailClient(address, password)  # noqa: E731 - one connection per worker
        else:
            creds = self.credentials
            make = lambda: GmailClient(AuthorizedSession(creds))  # noqa: E731
        return Scanner(make, self.store, detector, self.account.email, quota=self.monthly_quota())

    def _refresh_google(self) -> None:
        if self.account and self.account.provider == "gmail":
            google_auth.refresh(self.credentials)

    def _run_real_scan(self, options: ScanOptions) -> None:
        from kabala.gmail import GmailError

        try:
            self._refresh_google()
            scanner = self._make_scanner(options.threshold)
            summary = scanner.run(options, self._on_progress, self._cancel.is_set)
        except AuthError:
            return self._fail("auth")
        except GmailError as e:
            return self._fail("auth" if e.code in ("auth", "forbidden") else "network")
        except Exception:  # noqa: BLE001 - surface any failure to the UI
            log.exception("scan failed")
            return self._fail("unknown")
        if not summary.canceled:
            self.store.set_meta(self.account.email, "last_scan_at", datetime.now().isoformat(timespec="seconds"))
        self._load()
        self.scanning = False
        self.scan_finished.emit(summary)
        self.data_changed.emit()

    def _run_demo_scan(self, options: ScanOptions) -> None:
        try:
            known = {r.message_id for r in self.receipts}
            found, review = demo.run_demo_scan(options, known, self._on_progress, self._cancel.is_set)
        except Exception:  # noqa: BLE001
            log.exception("demo scan failed")
            return self._fail("unknown")
        self.receipts = sorted(self.receipts + found, key=lambda r: r.date)
        seen = {r.message_id for r in self.review}
        self.review = self.review + [r for r in review if r.message_id not in seen]
        self.scanning = False
        self.scan_finished.emit(summarize(found, len(self.review), options.folder, self._cancel.is_set()))
        self.data_changed.emit()

    # ---- review -------------------------------------------------------------

    def resolve_review(self, item_id: str, is_receipt: bool) -> None:
        item = next((r for r in self.review if r.id == item_id), None)
        if item is None or self.account is None:
            return
        self.review = [r for r in self.review if r.id != item_id]
        if self.account.demo:
            if is_receipt:
                fields = {k: v for k, v in item.__dict__.items() if k != "reason"}
                self.receipts = sorted(self.receipts + [Receipt(**fields)], key=lambda r: r.date)
            self.data_changed.emit()
            return
        self.data_changed.emit()  # remove from the list right away; filing happens in the background
        threading.Thread(target=self._resolve_real, args=(item, is_receipt), daemon=True).start()

    def _resolve_real(self, item: ReviewItem, is_receipt: bool) -> None:
        try:
            scanner = self._make_scanner(self.settings.threshold)
            if is_receipt:
                self._refresh_google()
                scanner.approve(item, self.settings.layout, Path(self.settings.destination))
            else:
                scanner.reject(item)
        except Exception:  # noqa: BLE001
            log.exception("review action failed")
        self._load()
        self.data_changed.emit()
