from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QScrollArea,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from kabala import google_auth, i18n
from kabala.backend import Backend
from kabala.i18n import t
from kabala.models import ScanOptions, ScanSummary
from kabala.settings import Settings
from kabala.theme import Theme
from kabala.ui.components import Ctx
from kabala.ui.dashboard import Dashboard
from kabala.ui.pages import BillingPage, PrivacyPage, ReviewPage, SettingsPage
from kabala.ui.scan import ScanPage
from kabala.ui.sidebar import ROUTES, Sidebar
from kabala.ui.welcome import Welcome
from kabala.ui.widgets import ConfirmDialog, hbox, label, ltr, vline


class MainWindow(QMainWindow):
    def __init__(self, app: QApplication, settings: Settings, theme: Theme, backend: Backend) -> None:
        super().__init__()
        self.app = app
        self.ctx = Ctx(backend=backend, settings=settings, theme=theme, navigate=self.navigate, rebuild=self.rebuild, sign_out=self.sign_out)
        self.route = "dashboard"
        # New users stay on the welcome flow until the first "Scan now"; a restored session skips it.
        self.onboarding = backend.account is None
        self.show_privacy_first = False
        self.scan_on_open = False
        self.sidebar: Sidebar | None = None
        self.page_area: QScrollArea | None = None

        self.setWindowTitle("Kabala")
        self.resize(1200, 800)
        self.setMinimumSize(980, 660)

        backend.data_changed.connect(self._on_data_changed)
        backend.plan_changed.connect(lambda _plan: self._on_data_changed())
        # The subscription page (portal) opens in the user's real browser; checkout opens inside Kabala.
        backend.open_url.connect(lambda url: google_auth.open_in_browser(url))
        backend.open_checkout.connect(self._open_checkout)
        self._checkout = None
        theme.changed.connect(self.rebuild)

        for i, route in enumerate(ROUTES):
            sc = QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self)
            sc.activated.connect(lambda r=route: self.navigate(r) if not self._in_welcome() else None)

        self._setup_tray()
        backend.scan_finished.connect(self._notify_scan_done)
        # Background scans: check shortly after start, then every 10 minutes.
        self.scan_timer = QTimer(self)
        self.scan_timer.timeout.connect(self._maybe_auto_scan)
        self.scan_timer.start(10 * 60 * 1000)
        QTimer.singleShot(30 * 1000, self._maybe_auto_scan)

        self.rebuild()

    # ---- tray & background scanning -------------------------------------------

    def _setup_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.app.windowIcon(), self)
        self.tray.setToolTip("Kabala")
        self.tray.activated.connect(
            lambda reason: self.bring_to_front() if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None
        )
        self.tray.messageClicked.connect(self.bring_to_front)
        self._build_tray_menu()
        self.tray.show()

    def _build_tray_menu(self) -> None:
        if not self.tray:
            return
        menu = QMenu(self)
        menu.addAction(t("tray.open"), self.bring_to_front)
        menu.addAction(t("tray.scan"), self._scan_from_tray)
        menu.addSeparator()
        menu.addAction(t("tray.quit"), self._quit)
        self.tray.setContextMenu(menu)
        self._tray_menu = menu

    def sign_out(self, kind: str = "disconnect") -> None:
        """
        Sign out the way Google recommends for installed apps: stop whatever still uses the account,
        delete the stored grant, revoke it at Google (RFC 7009). "disconnect" happens at once (signing
        in again is one click); "wipe" also deletes the local index, so it asks first. Saved receipt
        PDFs are the user's files and are never touched.
        """
        acc = self.ctx.backend.account
        if acc is None:
            return
        if kind != "wipe" or acc.demo:
            self.ctx.backend.disconnect_account()
            return
        body = t(f"account.wipe_body_{'imap' if acc.provider == 'imap' else 'google'}")
        dlg = ConfirmDialog(self, t("account.wipe_title", email=ltr(acc.email)), body, t("account.wipe_confirm"), t("account.cancel"))
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.ctx.backend.wipe_all()

    def _open_checkout(self, url: str) -> None:
        """PayPal checkout as the next page of this window; the browser only if the web engine is missing."""
        try:
            import kabala.ui.checkout_window  # noqa: F401
        except ImportError:
            google_auth.open_in_browser(url)
            return
        if self._in_welcome():
            return
        self._checkout = url
        self.route = "checkout"
        self._render_sidebar()
        self._render_page()

    def on_external_message(self, message: str) -> None:
        """From a second launch: "activate", or a kabala:// deep link (e.g. returning from PayPal)."""
        self.bring_to_front()
        if message.lower().startswith("kabala://"):
            self.ctx.backend.handle_deep_link(message)
            if not self._in_welcome():
                self.navigate("billing")
            if self.tray and "payment-success" in message:
                self.tray.showMessage("Kabala", t("billing.welcome_pro"), QSystemTrayIcon.MessageIcon.Information, 6000)

    def bring_to_front(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self) -> None:
        self._really_quit = True
        self.ctx.backend.cancel_scan()
        self.app.quit()

    def _scan_options(self) -> ScanOptions:
        s = self.ctx.settings
        return ScanOptions(folder=s.destination, layout=s.layout, threshold=s.threshold)  # type: ignore[arg-type]

    def _scan_from_tray(self) -> None:
        b = self.ctx.backend
        if b.account is None or self._in_welcome():
            self.bring_to_front()
            return
        b.start_scan(self._scan_options(), background=not self.isVisible())

    def _maybe_auto_scan(self) -> None:
        if not self._in_welcome() and self.ctx.backend.auto_scan_due():
            self.ctx.backend.start_scan(self._scan_options(), background=True)

    def _notify_scan_done(self, summary: ScanSummary) -> None:
        b = self.ctx.backend
        if not self.tray or summary.canceled:
            return
        if (b.background or not self.isActiveWindow()) and (summary.receipts or summary.needs_review):
            msg = t("tray.found", count=summary.receipts)
            if summary.needs_review:
                msg += "\n" + t("tray.review", count=summary.needs_review)
            self.tray.showMessage("Kabala", msg, QSystemTrayIcon.MessageIcon.Information, 8000)

    def closeEvent(self, event: QCloseEvent) -> None:
        """With background scanning on, closing the window keeps Kabala running in the tray."""
        keep = self.tray is not None and self.ctx.settings.auto_scan and not getattr(self, "_really_quit", False)
        if not keep:
            if self.tray:
                self.tray.hide()
            event.accept()
            self.app.quit()
            return
        event.ignore()
        self.hide()
        if not self.ctx.settings.tray_hint_shown:
            self.tray.showMessage("Kabala", t("tray.still_running"), QSystemTrayIcon.MessageIcon.Information, 6000)
            self.ctx.settings.tray_hint_shown = True
            self.ctx.settings.save()

    # ---- structure ----------------------------------------------------------

    def _in_welcome(self) -> bool:
        return self.ctx.backend.account is None or self.onboarding

    def rebuild(self) -> None:
        """Rebuilds the whole UI: used for language and theme changes (icons are baked per theme)."""
        i18n.set_language(self.ctx.settings.lang)
        self.app.setLayoutDirection(Qt.LayoutDirection.RightToLeft if i18n.is_rtl() else Qt.LayoutDirection.LeftToRight)
        self._build_tray_menu()  # language may have changed
        if self.ctx.backend.account is None:
            self.onboarding = True

        if self._in_welcome():
            self.sidebar = None
            self.page_area = None
            if self.show_privacy_first:
                root = QScrollArea()
                root.setObjectName("Root")
                root.setWidgetResizable(True)
                inner = QWidget()
                inner.setObjectName("Content")
                lay = QVBoxLayout(inner)
                lay.setContentsMargins(40, 32, 40, 32)
                lay.addWidget(PrivacyPage(self.ctx, on_back=self._close_privacy))
                root.setWidget(inner)
                self.setCentralWidget(root)
            else:
                self.setCentralWidget(Welcome(self.ctx, on_ready=self._start_from_welcome, on_privacy=self._open_privacy))
            return

        root = QWidget()
        root.setObjectName("Root")
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.sidebar_slot = QVBoxLayout()
        self.sidebar_slot.setContentsMargins(0, 0, 0, 0)
        row.addLayout(self.sidebar_slot)
        row.addWidget(vline())

        content = QWidget()
        content.setObjectName("Content")
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        account = self.ctx.backend.account
        if account.demo:
            banner = QFrame()
            banner.setObjectName("Banner")
            msg = f"{t('app.demo')}: {t('app.demo_hint')}"
            text = label(msg, align=Qt.AlignmentFlag.AlignCenter)
            text.setObjectName("BannerText")
            banner.setLayout(hbox(text, margins=(12, 5, 12, 5)))
            col.addWidget(banner)
        self.page_area = QScrollArea()
        self.page_area.setWidgetResizable(True)
        self.page_area.setFrameShape(QFrame.Shape.NoFrame)
        col.addWidget(self.page_area, 1)
        row.addWidget(content, 1)

        self.setCentralWidget(root)  # deletes the previous UI, including the old sidebar
        self.sidebar = None
        self._render_sidebar()
        self._render_page()

    def _render_sidebar(self) -> None:
        if self.sidebar is not None:
            self.sidebar_slot.removeWidget(self.sidebar)
            self.sidebar.setParent(None)
            self.sidebar.deleteLater()
        b = self.ctx.backend
        assert b.account is not None
        # the checkout page belongs to the plan section
        self.sidebar = Sidebar("billing" if self.route == "checkout" else self.route, b.account, len(b.review), b.usage_this_month(), b.plan)
        self.sidebar.navigate.connect(self.navigate)
        self.sidebar.account_action.connect(self.sign_out)
        self.sidebar_slot.addWidget(self.sidebar)

    def _render_page(self) -> None:
        assert self.page_area is not None
        if self.route == "checkout":
            self._render_checkout()
            return
        pages = {
            "dashboard": lambda: Dashboard(self.ctx),
            "scan": lambda: ScanPage(self.ctx, self.scan_on_open),
            "review": lambda: ReviewPage(self.ctx),
            "settings": lambda: SettingsPage(self.ctx),
            "privacy": lambda: PrivacyPage(self.ctx),
            "billing": lambda: BillingPage(self.ctx),
        }
        page = pages[self.route]()
        self.scan_on_open = False
        holder = QWidget()
        holder.setObjectName("Content")
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(40, 32, 40, 32)
        lay.addWidget(page)
        if self.route != "scan":
            lay.addStretch(0)
        self.page_area.setWidget(holder)  # deletes the previous page

    def _render_checkout(self) -> None:
        from kabala.ui.checkout_window import CheckoutPage

        page = CheckoutPage(self._checkout or "", self)
        page.back.connect(lambda: self.navigate("billing"))
        page.completed.connect(self.on_external_message)  # same path as the browser's kabala:// link
        holder = QWidget()
        holder.setObjectName("Content")
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.addWidget(page, 1)
        # the payment page scrolls inside itself, so it fills the visible area instead of the scroll area
        holder.setMinimumHeight(max(600, self.page_area.viewport().height()))
        self.page_area.setWidget(holder)

    # ---- navigation ---------------------------------------------------------

    def navigate(self, route: str) -> None:
        if self._in_welcome() or route not in ROUTES:
            return
        self.route = route
        self._render_sidebar()
        self._render_page()

    def _start_from_welcome(self) -> None:
        self.onboarding = False
        self.route = "scan"
        self.scan_on_open = True
        self.rebuild()

    def _open_privacy(self) -> None:
        self.show_privacy_first = True
        self.rebuild()

    def _close_privacy(self) -> None:
        self.show_privacy_first = False
        self.rebuild()

    def _on_data_changed(self) -> None:
        if self.ctx.backend.account is None:
            self.route = "dashboard"
            self.rebuild()
            return
        if self._in_welcome():
            return
        self._render_sidebar()
        # The scan screen shows its own summary, and the payment page must not reload mid-payment.
        if self.route not in ("scan", "checkout"):
            self._render_page()
