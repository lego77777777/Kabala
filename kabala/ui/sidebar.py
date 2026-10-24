from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QProgressBar, QToolButton, QVBoxLayout

from kabala.i18n import t
from kabala.licensing import UNMANAGED, Plan
from kabala.models import Account
from kabala.ui import icons
from kabala.ui.widgets import Card, Clickable, c, fit_pill, hbox, hline, icon_label, label, ltr, vbox, wrap

PRIMARY = [("dashboard", "dashboard"), ("scan", "scan"), ("review", "inbox")]
SECONDARY = [("settings", "settings"), ("privacy", "shield"), ("billing", "card")]
ROUTES = [r for r, _ in PRIMARY + SECONDARY]
FREE_LIMIT = 50


class Sidebar(QFrame):
    navigate = Signal(str)
    #: "disconnect": the sign-out button was clicked
    account_action = Signal(str)

    def __init__(self, route: str, account: Account, review_count: int, usage: int, plan: Plan = UNMANAGED) -> None:
        super().__init__()
        self.setObjectName("Sidebar")
        self.setFixedWidth(232)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 16, 12, 14)
        lay.setSpacing(2)

        logo = QLabel()
        logo.setPixmap(icons.logo(28, c("accent")))
        name = label(t("app.name"))
        name.setStyleSheet("font-size: 11pt; font-weight: 600;")
        lay.addLayout(hbox(logo, name, None, spacing=10, margins=(8, 0, 8, 18)))

        self.items: dict[str, Clickable] = {}
        # without a subscription server (the free full version) there is no plan page to show
        items = [(r, ic) for r, ic in PRIMARY + SECONDARY if plan.managed or r != "billing"]
        for i, (r, ic) in enumerate(items):
            if i == len(PRIMARY):
                lay.addSpacing(10)
                lay.addWidget(hline())
                lay.addSpacing(10)
            item = Clickable("NavItem")
            item.setFixedHeight(36)
            item.setToolTip(f"Ctrl+{i + 1}")
            item.setAccessibleName(t(f"nav.{r}"))
            active = r == route
            row = hbox(icon_label(ic, c("fg") if active else c("muted")), label(t(f"nav.{r}")), None, spacing=12, margins=(12, 0, 10, 0))
            if r == "review" and review_count:
                count = QLabel(str(review_count))
                count.setObjectName("Count")
                row.addWidget(fit_pill(count, 8, 12, 20))
            item.setLayout(row)
            item.set_active(active)
            item.clicked.connect(lambda r=r: self.navigate.emit(r))
            self.items[r] = item
            lay.addWidget(item)

        lay.addStretch(1)

        # account + usage
        card = Card(padding=12, spacing=8)
        avatar = label(account.email[:1].upper())
        avatar.setObjectName("Avatar")
        avatar.setFixedSize(32, 32)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        email = label("")
        email.setStyleSheet("font-weight: 500;")
        # long addresses are shortened in the middle (the start and the domain stay readable)
        shown = email.fontMetrics().elidedText(account.email, Qt.TextElideMode.ElideMiddle, 100)
        email.setText(ltr(shown))
        email.setToolTip(account.email)
        plan_name = t("nav.pro_plan") if plan.is_pro and plan.managed else t("nav.free_plan") if plan.managed else t("nav.dev_plan")
        # one click signs out (deleting local data stays on the Privacy page, behind a confirmation)
        out_btn = QToolButton()
        out_btn.setObjectName("SignOut")
        out_btn.setIcon(icons.icon("logout", c("muted"), 16))
        out_btn.setToolTip(t("account.disconnect"))
        out_btn.setAccessibleName(t("account.disconnect"))
        out_btn.setAutoRaise(True)
        out_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        out_btn.clicked.connect(lambda: self.account_action.emit("disconnect"))
        card.lay.addLayout(hbox(avatar, wrap(vbox(email, label(plan_name, "small"), spacing=0)), None, out_btn, spacing=10))
        limit = plan.monthly_receipts
        if limit is not None:
            bar = QProgressBar()
            bar.setRange(0, limit)
            bar.setValue(min(usage, limit))
            bar.setTextVisible(False)
            bar.setAccessibleName(t("nav.usage", used=usage, limit=limit))
            card.add(bar, label(t("nav.usage", used=min(usage, limit), limit=limit), "small"))
        lay.addWidget(card)
