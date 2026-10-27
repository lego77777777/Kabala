from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QWidget

from kabala.backend import Backend
from kabala.i18n import t
from kabala.ui.widgets import Button, alert, hbox, label, ltr, vbox


class EmailCodeDialog(QDialog):
    """Asks for the 6-digit code the plan server emailed to the connected address (Backend.submit_email_code)."""

    def __init__(self, parent: QWidget, backend: Backend, email: str) -> None:
        super().__init__(parent)
        self.backend = backend
        self.setWindowTitle(t("email_code.title"))
        self.setModal(True)
        self.setMinimumWidth(440)
        lay = vbox(spacing=10, margins=(24, 22, 24, 20))
        self.setLayout(lay)
        lay.addWidget(label(t("email_code.title"), "h2"))
        lay.addWidget(label(t("email_code.body", email=ltr(email)), "muted", wrap=True))
        lay.addSpacing(6)

        self.field = QLineEdit()
        self.field.setPlaceholderText("123456")
        self.field.setMaxLength(12)
        self.field.setAccessibleName(t("email_code.field"))
        self.field.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.field.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.field.returnPressed.connect(self._verify)
        lay.addWidget(self.field)
        lay.addWidget(label(t("email_code.hint"), "small", wrap=True))

        self.notice = label("", "small", wrap=True)
        self.notice.hide()
        lay.addWidget(self.notice)
        self.error = alert("")
        self.error.hide()
        lay.addWidget(self.error)
        lay.addSpacing(10)

        resend = Button(t("email_code.resend"), "ghost", "sm", on_click=self._resend)
        later = Button(t("email_code.later"), "secondary", on_click=self.reject)
        self.go = Button(t("email_code.verify"), "primary", icon="check", on_click=self._verify)
        self.go.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.go.setDefault(True)
        lay.addLayout(hbox(resend, None, later, self.go))
        self.field.setFocus()

        backend.email_verified.connect(self.accept)
        backend.email_code_failed.connect(self._on_failed)
        self.rejected.connect(backend.cancel_email_code)
        self.finished.connect(self._disconnect)

    def _verify(self) -> None:
        if not self.field.text().strip():
            return
        self.error.hide()
        self.go.setEnabled(False)
        self.go.setText(f" {t('email_code.verifying')}")
        self.backend.submit_email_code(self.field.text())

    def _resend(self) -> None:
        self.error.hide()
        self.field.clear()
        self.notice.setText(t("email_code.resent"))
        self.notice.show()
        self.backend.request_email_code()

    def _on_failed(self, code: str) -> None:
        self.go.setEnabled(True)
        self.go.setText(f" {t('email_code.verify')}")
        self.notice.hide()
        self.error.findChild(QLabel, "AlertText").setText(t(f"email_code.errors.{code}"))
        self.error.show()
        self.field.selectAll()
        self.field.setFocus()

    def _disconnect(self) -> None:
        for signal, slot in ((self.backend.email_verified, self.accept), (self.backend.email_code_failed, self._on_failed)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
