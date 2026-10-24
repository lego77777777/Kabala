"""
In-app checkout: the PayPal payment page shown as a page of Kabala's main window (no separate window,
no system browser). "Upgrade to Pro" moves to this page; "Back" returns to the plan page.

* A private (off-the-record) browser profile: no cookies, cache or history are kept, and nothing is
  shared with the user's browser.
* The address line shows the real host, with a lock only on an encrypted (https) page.
* PayPal's login / card form appears as an overlay in the same page (no pop-up windows).
* When the payment page navigates to kabala://payment-success, `completed` fires with that link,
  exactly like the deep link from a browser.
* Only https pages are loaded (plus http on localhost for development); anything else is refused.
"""

from __future__ import annotations

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

from kabala.deeplink import SCHEME
from kabala.i18n import t
from kabala.ui.widgets import Button, c, hbox, icon_label, label

_LOCAL_HOSTS = {"localhost", "127.0.0.1"}


def _allowed(url: QUrl) -> bool:
    return url.scheme() == "https" or (url.scheme() == "http" and url.host() in _LOCAL_HOSTS) or url.scheme() in ("about", "data", "blob")


class _Page(QWebEnginePage):
    deep_link = Signal(str)

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:  # noqa: N802 - Qt API
        if url.scheme() == SCHEME:
            self.deep_link.emit(url.toString())
            return False
        return _allowed(url)

    def createWindow(self, _type):  # noqa: N802 - Qt API
        # No pop-ups: PayPal's SDK sees window.open() fail and shows its login / card form as an overlay
        # inside this same page (its documented iframe fallback). Verified in Sandbox: a pop-up window
        # stays blank under QtWebEngine, while the in-page overlay works.
        return None


class CheckoutPage(QWidget):
    """Emits `completed` with the kabala:// link when the payment went through, `back` to leave."""

    completed = Signal(str)
    back = Signal()

    def __init__(self, url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # a profile without a storage name is off-the-record: nothing survives the page
        self._profile = QWebEngineProfile(self)
        self.page = _Page(self._profile, self)
        self.page.deep_link.connect(self.completed.emit)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)

        back = Button(t("billing.checkout_back"), "ghost", icon="arrow_right" if self.isRightToLeft() else "arrow_left", on_click=self.back.emit)
        self.lock = icon_label("lock", c("success"), 13)
        self.address = QLabel()
        self.address.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.address.setStyleSheet(f"color:{c('muted')}; font-size:9pt;")
        head = hbox(back, label(t("billing.checkout_title"), "h2"), None, self.lock, self.address, spacing=10, margins=(0, 0, 0, 8))

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.progress.setRange(0, 100)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(head)
        lay.addWidget(self.progress)
        lay.addWidget(self.view, 1)
        self.view.urlChanged.connect(self._show_url)
        self.view.loadProgress.connect(self.progress.setValue)
        self.view.loadFinished.connect(lambda _ok: self.progress.hide())
        self.view.loadStarted.connect(self.progress.show)
        self.view.setUrl(QUrl(url))

    def _show_url(self, url: QUrl) -> None:
        # the lock only for an encrypted page; the host is always shown so the buyer sees where they are
        self.lock.setVisible(url.scheme() == "https")
        self.address.setText(f"{url.scheme()}://{url.host()}")
