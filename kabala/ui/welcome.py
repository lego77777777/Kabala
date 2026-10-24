from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QLabel,
    QLineEdit,
    QProgressBar,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from kabala.i18n import language, t
from kabala.imap_gmail import APP_PASSWORDS_URL
from kabala.settings import config_dir
from kabala.ui import icons
from kabala.ui.components import Ctx, FolderPreview
from kabala.ui.widgets import (
    Button,
    Card,
    Segmented,
    alert,
    c,
    hbox,
    icon_badge,
    icon_label,
    label,
    ltr,
    vbox,
    wrap,
)

class _FitStack(QStackedWidget):
    """
    A QStackedWidget exactly as tall as the page on show. The stock one is as tall as its tallest
    page, which left the welcome card half empty. Call fit() whenever the visible page's content
    changes (an error shown, a message added).
    """

    def __init__(self, content_width: int) -> None:
        super().__init__()
        self._width = content_width
        self.currentChanged.connect(lambda _i: self.fit())

    def fit(self) -> None:
        w = self.currentWidget()
        if w is None:
            return
        lay = w.layout()
        if lay is not None:
            lay.activate()
        h = w.heightForWidth(self._width) if w.hasHeightForWidth() else -1
        self.setFixedHeight(h if h > 0 else w.sizeHint().height())


# The welcome screen connects with one click through Google's sign-in (until Google verifies the app,
# Google shows its "unverified app" screen there). True adds "Connect with an app password" (IMAP),
# which has no such screen but needs the user's address and an app password.
OFFER_APP_PASSWORD = False


class Welcome(QWidget):
    def __init__(self, ctx: Ctx, on_ready: Callable[[], None], on_privacy: Callable[[], None]) -> None:
        super().__init__()
        self.ctx = ctx
        self.on_ready = on_ready
        self.setObjectName("Root")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 24)

        lang = Segmented([("he", "עב"), ("en", "EN")], language(), t("settings.language"))
        lang.changed.connect(self._change_lang)
        outer.addLayout(hbox(None, lang))
        outer.addStretch(1)

        col = QWidget()
        col.setFixedWidth(460)
        cl = vbox(spacing=0)
        col.setLayout(cl)
        outer.addWidget(col, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)

        logo = QLabel()
        logo.setPixmap(icons.logo(52, c("accent")))
        cl.addWidget(logo, alignment=Qt.AlignmentFlag.AlignHCenter)
        cl.addSpacing(18)
        cl.addWidget(label(t("welcome.title"), "display", align=Qt.AlignmentFlag.AlignHCenter))
        cl.addSpacing(6)
        sub = label(t("welcome.subtitle"), "muted", wrap=True, align=Qt.AlignmentFlag.AlignHCenter)
        sub.setStyleSheet("font-size: 10.5pt;")
        cl.addWidget(sub)
        cl.addSpacing(26)

        self.steps_row = QWidget()
        cl.addWidget(self.steps_row, alignment=Qt.AlignmentFlag.AlignHCenter)
        cl.addSpacing(14)

        self.card = Card(shadow=True, padding=24, spacing=0)
        # never taller than its content: spare window height goes to the space around the column
        self.card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.stack = _FitStack(460 - 2 * 24)  # column width minus the card's padding
        self.card.add(self.stack)
        cl.addWidget(self.card)

        self.stack.addWidget(self._connect_panel())
        self.stack.addWidget(self._waiting_panel())
        self.connected_holder = QWidget()
        self.connected_holder.setLayout(vbox())
        self.stack.addWidget(self.connected_holder)

        cl.addSpacing(18)
        cl.addLayout(hbox(None, icon_label("lock", c("muted"), 13), label(t("welcome.read_only"), "small"), None, spacing=6))
        cl.addSpacing(2)
        cl.addWidget(Button(t("welcome.privacy_link"), "link", on_click=on_privacy), alignment=Qt.AlignmentFlag.AlignHCenter)
        self.stack.addWidget(self._config_panel())
        self.stack.addWidget(self._imap_panel())

        ctx.backend.connected.connect(self._on_connected)
        ctx.backend.connect_failed.connect(self._on_failed)
        self._set_step("connected" if ctx.backend.account else "connect")
        if not ctx.backend.account:
            QTimer.singleShot(0, self.gmail_btn.setFocus)  # Enter connects right away

    # ---- panels -------------------------------------------------------------

    def _connect_panel(self) -> QWidget:
        w = QWidget()
        lay = vbox(spacing=10)
        w.setLayout(lay)
        if email := self.ctx.backend.signed_out_email:
            # confirmation right after signing out (and how to finish the job for an app password)
            imap = self.ctx.backend.signed_out_provider == "imap"
            done = QFrame()
            done.setObjectName("SuccessBox")
            text = label(t("account.signed_out_imap" if imap else "account.signed_out", email=ltr(email)), wrap=True)
            box = vbox(text, spacing=6, margins=(12, 10, 12, 10))
            if imap:
                box.addWidget(Button(t("account.open_app_passwords"), "link", on_click=lambda: QDesktopServices.openUrl(QUrl(APP_PASSWORDS_URL))), alignment=Qt.AlignmentFlag.AlignLeading)
            done.setLayout(box)
            lay.addWidget(done)
            lay.addSpacing(4)
        gmail = Button(t("welcome.connect_gmail"), "primary", "lg", on_click=self._connect)
        buttons = [gmail]
        if OFFER_APP_PASSWORD:
            imap = Button(t("welcome.connect_imap"), "secondary", "lg", icon="lock", on_click=lambda: self._set_step("imap"))
            imap.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            buttons.append(imap)
        gmail.setIcon(icons.icon("google", "#fff", 20))
        gmail.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        outlook = Button(t("welcome.connect_outlook"), "secondary", "lg", icon="mail")
        outlook.setEnabled(False)
        for b in buttons + [outlook]:
            lay.addWidget(b)
        self.error_box = alert(t("welcome.error"))
        self.error_box.hide()
        lay.addWidget(self.error_box)
        lay.addSpacing(8)
        for ic, text in zip(("sparkles", "tree", "drive"), t("welcome.features")):
            lay.addLayout(hbox(icon_label(ic, c("accent")), label(text, "muted"), None, spacing=10))
        lay.addStretch(1)  # any extra height goes below the list, never between its rows
        self.gmail_btn = gmail
        return w

    def _config_panel(self) -> QWidget:
        """Shown when the Google OAuth client file is missing (a developer setup step)."""
        w = QWidget()
        lay = vbox(spacing=8, margins=(0, 4, 0, 0))
        w.setLayout(lay)
        lay.addWidget(icon_badge("settings"), alignment=Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(6)
        lay.addWidget(label(t("welcome.config_title"), "h2", align=Qt.AlignmentFlag.AlignHCenter))
        body = label(t("welcome.config_body"), "muted", wrap=True, align=Qt.AlignmentFlag.AlignHCenter)
        # QStackedWidget doesn't pass height-for-width through, so reserve room for the wrapped lines.
        body.setMinimumHeight(body.fontMetrics().lineSpacing() * 3)
        lay.addWidget(body)
        lay.addSpacing(6)
        for i, step in enumerate(t("welcome.config_steps"), start=1):
            num = label(str(i), align=Qt.AlignmentFlag.AlignCenter)
            num.setFixedSize(20, 20)
            num.setStyleSheet(f"background:{c('accent_soft')}; color:{c('accent')}; border-radius:10px; font-size:8pt; font-weight:600;")
            text = label(step, wrap=True)
            text.setMinimumHeight(text.fontMetrics().lineSpacing() * 2)
            row = hbox(num, text, spacing=10)
            row.setStretch(1, 1)
            lay.addLayout(row)
        lay.addSpacing(6)
        lay.addWidget(Button(t("welcome.config_console"), "secondary", icon="arrow_left" if self.isRightToLeft() else "arrow_right",
                             on_click=lambda: QDesktopServices.openUrl(QUrl("https://console.cloud.google.com/auth/clients"))))
        pick = Button(t("welcome.config_pick"), "primary", icon="file", on_click=self._pick_client_file)
        lay.addWidget(pick)
        self.config_error = alert("")
        self.config_error.hide()
        lay.addWidget(self.config_error)
        lay.addSpacing(2)
        lay.addLayout(hbox(None, Button(t("welcome.config_retry"), "link", on_click=self._retry_config),
                           label("·", "small"),
                           Button(t("welcome.config_demo"), "link", on_click=self.ctx.backend.connect_demo), None, spacing=4))
        return w

    def _imap_panel(self) -> QWidget:
        """Gmail over IMAP with an app password: the way in without Google's "unverified app" screen."""
        w = QWidget()
        lay = vbox(spacing=8, margins=(0, 4, 0, 0))
        w.setLayout(lay)
        lay.addWidget(icon_badge("lock"), alignment=Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(4)
        lay.addWidget(label(t("welcome.imap_title"), "h2", align=Qt.AlignmentFlag.AlignHCenter))
        body = label(t("welcome.imap_body"), "muted", wrap=True, align=Qt.AlignmentFlag.AlignHCenter)
        body.setMinimumHeight(body.fontMetrics().lineSpacing() * 2)
        lay.addWidget(body)
        lay.addSpacing(4)
        for i, step in enumerate(t("welcome.imap_steps"), start=1):
            num = label(str(i), align=Qt.AlignmentFlag.AlignCenter)
            num.setFixedSize(20, 20)
            num.setStyleSheet(f"background:{c('accent_soft')}; color:{c('accent')}; border-radius:10px; font-size:8pt; font-weight:600;")
            text = label(step, wrap=True)
            row = hbox(num, text, spacing=10)
            row.setStretch(1, 1)
            lay.addLayout(row)
        lay.addWidget(Button(t("welcome.imap_open"), "secondary", icon="arrow_left" if self.isRightToLeft() else "arrow_right",
                             on_click=lambda: QDesktopServices.openUrl(QUrl(APP_PASSWORDS_URL))))
        lay.addSpacing(4)

        def field(placeholder: str, secret: bool) -> QLineEdit:
            f = QLineEdit()
            f.setPlaceholderText(placeholder)
            f.setAccessibleName(placeholder)
            f.setLayoutDirection(Qt.LayoutDirection.LeftToRight)  # addresses and passwords are LTR
            if secret:
                f.setEchoMode(QLineEdit.EchoMode.Password)
            f.returnPressed.connect(self._connect_imap)
            return f

        self.imap_email = field(t("welcome.imap_email"), False)
        self.imap_password = field(t("welcome.imap_password"), True)
        lay.addWidget(self.imap_email)
        lay.addWidget(self.imap_password)
        self.imap_go = Button(t("welcome.imap_connect"), "primary", "lg", on_click=self._connect_imap)
        lay.addWidget(self.imap_go)
        self.imap_error = alert("")
        self.imap_error.hide()
        lay.addWidget(self.imap_error)
        lay.addWidget(Button(t("welcome.imap_back"), "link", on_click=lambda: self._set_step("connect")),
                      alignment=Qt.AlignmentFlag.AlignHCenter)
        return w

    def _connect_imap(self) -> None:
        address, password = self.imap_email.text().strip(), self.imap_password.text()
        if "@" not in address or not password.strip():
            self._imap_failed("imap_missing")
            return
        self.imap_error.hide()
        self.imap_go.setEnabled(False)
        self.imap_go.setText(t("welcome.imap_connecting"))
        self.ctx.backend.connect_imap(address, password)

    def _imap_failed(self, code: str) -> None:
        self.imap_go.setEnabled(True)
        self.imap_go.setText(t("welcome.imap_connect"))
        self.imap_error.findChild(QLabel, "AlertText").setText(t("welcome.errors").get(code, t("welcome.error")))
        self.imap_error.show()
        self.stack.fit()

    def _pick_client_file(self) -> None:
        start = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(self, t("welcome.config_pick"), start, "JSON (*.json)")
        if not path:
            return
        problem = self.ctx.backend.import_google_client(path)
        if problem:
            self.config_error.findChild(QLabel, "AlertText").setText(t(f"welcome.errors.{problem}"))
            self.config_error.show()
            self.stack.fit()
            return
        self.config_error.hide()
        self._connect()  # straight to Google's consent page in Chrome

    def _waiting_panel(self) -> QWidget:
        w = QWidget()
        lay = vbox(spacing=8, margins=(0, 8, 0, 0))
        w.setLayout(lay)
        lay.addWidget(icon_badge("lock"), alignment=Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(8)
        lay.addWidget(label(t("welcome.waiting_title"), "h2", align=Qt.AlignmentFlag.AlignHCenter))
        lay.addWidget(label(t("welcome.waiting_body"), "muted", wrap=True, align=Qt.AlignmentFlag.AlignHCenter))
        lay.addSpacing(8)
        busy = QProgressBar()
        busy.setRange(0, 0)
        busy.setTextVisible(False)
        busy.setFixedWidth(160)
        lay.addWidget(busy, alignment=Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(6)
        lay.addWidget(Button(t("welcome.cancel"), "ghost", on_click=self._cancel), alignment=Qt.AlignmentFlag.AlignHCenter)
        return w

    def _build_connected(self) -> None:
        acc = self.ctx.backend.account
        if not acc:
            return
        old = self.connected_holder
        self.connected_holder = QWidget()
        lay = vbox(spacing=0)
        self.connected_holder.setLayout(lay)
        self.stack.insertWidget(2, self.connected_holder)
        self.stack.removeWidget(old)
        old.deleteLater()
        folder = self.ctx.settings.destination

        ok = QFrame()
        ok.setObjectName("SuccessBox")
        check = QLabel()
        check.setPixmap(icons.pixmap("check_circle", c("success"), 28))
        ok.setLayout(
            hbox(
                check,
                wrap(vbox(label(t("welcome.connected_title"), "success"), label(t("welcome.connected_as", email=ltr(acc.email)), "muted"), spacing=0)),
                None,
                spacing=12,
                margins=(12, 10, 12, 10),
            )
        )
        lay.addWidget(ok)
        lay.addSpacing(18)
        lay.addWidget(label(t("welcome.folder_label")))
        lay.addSpacing(6)

        path_box = QFrame()
        path_box.setObjectName("Inset")
        path = label(ltr(folder))
        path.setToolTip(folder)
        path_box.setLayout(hbox(icon_label("folder", c("muted")), path, None, spacing=8, margins=(10, 8, 10, 8)))
        change = Button(t("welcome.change"), "secondary", on_click=self._pick_folder)
        row = hbox(path_box, change, spacing=8)
        row.setStretch(0, 1)
        lay.addLayout(row)
        lay.addSpacing(10)
        lay.addWidget(FolderPreview(folder, self.ctx.settings.layout))
        lay.addSpacing(18)
        go = Button(t("welcome.scan_now"), "primary", "lg", icon="scan", on_click=self.on_ready)
        go.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        lay.addWidget(go)

    # ---- actions ------------------------------------------------------------

    def _set_step(self, step: str) -> None:
        idx = {"connect": 0, "waiting": 1, "connected": 2, "config": 3, "imap": 4}[step]
        if step == "connected":
            self._build_connected()
        self.stack.setCurrentIndex(idx)
        self.stack.fit()
        self._render_steps(1 if step == "connected" else 0)

    def _render_steps(self, current: int) -> None:
        old = self.steps_row.layout()
        if old is not None:
            QWidget().setLayout(old)  # reparent the old layout so it gets deleted
        row = hbox(spacing=8)
        for i, name in enumerate(t("welcome.steps")):
            dot = label(str(i + 1), align=Qt.AlignmentFlag.AlignCenter)
            dot.setFixedSize(20, 20)
            if i < current:
                dot.setPixmap(icons.pixmap("check", "#fff", 12))
                dot.setStyleSheet(f"background:{c('success')}; border-radius:10px;")
            elif i == current:
                dot.setStyleSheet(f"background:{c('accent')}; color:{c('accent_fg')}; border-radius:10px; font-size:8pt; font-weight:600;")
            else:
                dot.setStyleSheet(f"background:{c('surface2')}; color:{c('muted')}; border-radius:10px; font-size:8pt;")
            text = label(name, None if i == current else "small")
            if i == current:
                text.setStyleSheet("font-size: 8.5pt; font-weight: 600;")
            row.addWidget(dot)
            row.addWidget(text)
            if i < 2:
                line = QFrame()
                line.setObjectName("HLine")
                line.setFixedSize(24, 1)
                row.addWidget(line)
        self.steps_row.setLayout(row)

    def _connect(self) -> None:
        self.error_box.hide()
        if not self.ctx.backend.has_google_client():
            self._set_step("config")
            return
        self._set_step("waiting")
        self.ctx.backend.connect_account("gmail")

    def _retry_config(self) -> None:
        if self.ctx.backend.has_google_client():
            self.config_error.hide()
            self._set_step("connect")
            self._connect()
        else:
            self.config_error.findChild(QLabel, "AlertText").setText(t("welcome.errors.client_not_found"))
            self.config_error.show()
            self.stack.fit()

    def _on_failed(self, code: str) -> None:
        if code == "config_missing":
            self._set_step("config")
            return
        if self.stack.currentIndex() == 4:  # failure of the app-password connection
            self._imap_failed(code)
            return
        errors = t("welcome.errors")
        self.error_box.findChild(QLabel, "AlertText").setText(errors.get(code, t("welcome.error")))
        self.error_box.show()
        self.stack.fit()
        self._set_step("connect")
        self._bring_to_front()

    def _bring_to_front(self) -> None:
        win = self.window()
        win.showNormal()
        win.raise_()
        win.activateWindow()

    def _cancel(self) -> None:
        self.ctx.backend.cancel_connect()
        self._set_step("connect")
        self.gmail_btn.setFocus()

    def _on_connected(self, _account: object) -> None:
        self.ctx.backend.signed_out_email = None
        self._set_step("connected")
        self._bring_to_front()  # the user is coming back from the browser

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, t("welcome.folder_label"), self.ctx.settings.destination)
        if folder:
            self.ctx.settings.folder = folder
            self.ctx.settings.save()
            self._build_connected()
            self.stack.setCurrentIndex(2)

    def _change_lang(self, lang: str) -> None:
        self.ctx.settings.lang = lang
        self.ctx.settings.save()
        self.ctx.rebuild()
