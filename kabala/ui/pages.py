"""Review, Settings, Privacy and Billing screens."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QProgressBar,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import sys

from kabala.autoscan import INTERVALS_HOURS, runs_at_login, set_run_at_login
from kabala.detect.llm import get_anthropic_key, set_anthropic_key
from kabala.i18n import language, money, short_date, t  # money() formats amounts that come from the server
from kabala.ui.components import Ctx, FolderPreview
from kabala.ui.sidebar import FREE_LIMIT
from kabala.ui.widgets import (
    Button,
    Card,
    ConfirmDialog,
    Segmented,
    alert,
    badge,
    c,
    empty_state,
    hbox,
    hline,
    icon_badge,
    icon_label,
    label,
    ltr,
    page_header,
    vbox,
    wrap,
)


def _centered(page: QWidget) -> QHBoxLayout:
    """Page column that grows up to its max width and stays centered."""
    page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addStretch(1)
    row.addWidget(page, 100)
    row.addStretch(1)
    return row


def _page(max_width: int | None = None) -> tuple[QWidget, QVBoxLayout]:
    w = QWidget()
    if max_width:
        w.setMaximumWidth(max_width)
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(14)
    return w, lay


class ReviewPage(QWidget):
    def __init__(self, ctx: Ctx) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(page_header(t("review.title"), t("review.subtitle")))
        items = ctx.backend.review
        if not items:
            card = Card()
            card.add(empty_state("party", t("review.empty_title"), t("review.empty_body")))
            lay.addWidget(card)
        for r in items:
            card = Card(padding=16, spacing=4)
            head = hbox(label(r.company, "h2"), badge(t(f"review.reason.{r.reason}"), "warning"), badge(t("review.confidence", value=round(r.confidence * 100))), None, spacing=8)
            card.lay.addLayout(head)
            card.add(label(r.subject))
            meta = [ltr(r.sender), short_date(r.date)]
            if r.amount > 0:
                meta.append(money(r.amount, r.currency))
            card.add(label("   ·   ".join(meta), "small"))
            actions = hbox(
                None,
                Button(t("review.not_receipt"), "secondary", "sm", icon="x", on_click=lambda i=r.id: ctx.backend.resolve_review(i, False)),
                Button(t("review.is_receipt"), "primary", "sm", icon="check", on_click=lambda i=r.id: ctx.backend.resolve_review(i, True)),
            )
            card.lay.addLayout(actions)
            lay.addWidget(card)
        lay.addStretch(1)


def _section(title: str, rows: list[QWidget]) -> QWidget:
    card = Card(padding=0, spacing=0)
    for i, row in enumerate(rows):
        if i:
            card.add(hline())
        card.add(row)
    return wrap(vbox(label(title.upper() if language() == "en" else title, "section"), card, spacing=8))


def _row(title: str, control: QWidget | None, hint: str = "", hint_wrap: bool = True) -> QWidget:
    left = wrap(vbox(label(title), label(hint, "small", wrap=hint_wrap) if hint else None, spacing=2))
    row = hbox(left, spacing=24, margins=(20, 14, 20, 14))
    row.setStretch(0, 1)
    if control is not None:
        row.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
    return wrap(row)


class SettingsPage(QWidget):
    def __init__(self, ctx: Ctx) -> None:
        super().__init__()
        self.ctx = ctx
        s = ctx.settings
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        page, lay = _page(760)
        outer.addLayout(_centered(page))
        outer.addStretch(1)
        lay.setSpacing(24)
        lay.addWidget(page_header(t("settings.title")))

        lang = Segmented([("he", "עברית"), ("en", "English")], s.lang, t("settings.language"))
        lang.changed.connect(lambda v: self._set(lang=v, rebuild=True))
        theme = Segmented([(k, t(f"settings.themes.{k}")) for k in ("system", "light", "dark")], s.theme, t("settings.theme"))
        theme.changed.connect(self._set_theme)
        lay.addWidget(_section(t("settings.general"), [_row(t("settings.language"), lang), _row(t("settings.theme"), theme)]))

        layout_ctl = Segmented([(k, t(f"settings.layouts.{k}")) for k in ("company-first", "date-first")], s.layout, t("settings.layout"))
        layout_ctl.changed.connect(lambda v: self._set(layout=v, preview=True))
        self.preview_holder = QVBoxLayout()
        self.preview_holder.setContentsMargins(20, 0, 20, 16)
        layout_box = wrap(vbox(_row(t("settings.layout"), layout_ctl), spacing=0))
        layout_box.layout().addLayout(self.preview_holder)
        self._render_preview()
        dest = _row(t("settings.destination"), Button(t("settings.change"), "secondary", icon="folder", on_click=self._pick), ltr(s.destination), hint_wrap=False)
        lay.addWidget(_section(t("settings.folders"), [dest, layout_box]))

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(10, 19)  # 50%..95% in 5% steps
        slider.setValue(round(s.threshold * 20))
        slider.setFixedWidth(160)
        slider.setInvertedAppearance(self.isRightToLeft())  # fill from the reading start side
        slider.setAccessibleName(t("settings.threshold"))
        value = label(f"{round(s.threshold * 100)}%")
        value.setFixedWidth(40)
        value.setStyleSheet("font-weight: 600;")

        def on_slide(v: int) -> None:
            value.setText(f"{v * 5}%")
            self._set(threshold=v / 20)

        slider.valueChanged.connect(on_slide)
        combo = QComboBox()
        for key in ("anthropic", "ollama", "none"):
            combo.addItem(t(f"settings.providers.{key}"), key)
        combo.setCurrentIndex(max(0, combo.findData(s.provider)))
        combo.setMinimumWidth(230)
        combo.setAccessibleName(t("settings.provider"))
        combo.currentIndexChanged.connect(lambda _i: self._set(provider=combo.currentData(), rebuild=True))

        rows = [
            _row(t("settings.threshold"), wrap(hbox(slider, value, spacing=10)), t("settings.threshold_hint")),
            _row(t("settings.provider"), combo, t("settings.provider_hint")),
        ]
        if s.provider == "anthropic":
            rows.append(self._api_key_row())
        lay.addWidget(_section(t("settings.detection"), rows))
        lay.addWidget(self._auto_scan_section())

        acc = ctx.backend.account
        if acc:
            lay.addWidget(
                _section(t("settings.account"), [_row(ltr(acc.email), Button(t("settings.disconnect"), "secondary", icon="logout", on_click=lambda: ctx.sign_out("disconnect")))])
            )

    def _auto_scan_section(self) -> QWidget:
        s = self.ctx.settings
        on_off = [("on", t("settings.on")), ("off", t("settings.off"))]

        if not self.ctx.backend.plan.background_scan:
            upgrade = Button(t("billing.upgrade"), "secondary", "sm", icon="sparkles", on_click=lambda: self.ctx.navigate("billing"))
            title = wrap(hbox(label(t("settings.auto_scan")), badge("Pro", "accent"), None, spacing=8))
            row = wrap(hbox(wrap(vbox(title, label(t("settings.pro_only"), "small", wrap=True), spacing=2)), upgrade, spacing=24,
                            margins=(20, 14, 20, 14)))
            return _section(t("settings.background"), [row])

        auto = Segmented(on_off, "on" if s.auto_scan else "off", t("settings.auto_scan"))
        def toggle(v: str) -> None:
            if v == "off" and runs_at_login():
                set_run_at_login(False)  # nothing to do at login without background scans
            self._set(auto_scan=v == "on", rebuild=True)

        auto.changed.connect(toggle)
        rows = [_row(t("settings.auto_scan"), auto, t("settings.auto_scan_hint"))]

        if s.auto_scan:
            interval = QComboBox()
            for h in INTERVALS_HOURS:
                text = t("settings.every_hours", hours=h) if h < 24 else t("settings.every_day") if h == 24 else t("settings.every_days", days=h // 24)
                interval.addItem(text, h)
            interval.setCurrentIndex(max(0, interval.findData(s.auto_interval)))
            interval.setAccessibleName(t("settings.interval"))
            interval.currentIndexChanged.connect(lambda _i: self._set(auto_interval=interval.currentData()))
            last = self.ctx.backend.last_scan_at()
            last_text = t("settings.last_scan", when=last.strftime("%d/%m/%Y %H:%M")) if last else t("settings.never_scanned")
            rows.append(_row(t("settings.interval"), interval, last_text))

            if sys.platform == "win32":
                login = Segmented(on_off, "on" if runs_at_login() else "off", t("settings.start_with_windows"))
                login.changed.connect(lambda v: set_run_at_login(v == "on"))
                rows.append(_row(t("settings.start_with_windows"), login, t("settings.start_with_windows_hint")))

        section = _section(t("settings.background"), rows)
        return section

    def _api_key_row(self) -> QWidget:
        """Anthropic API key, saved to the OS credential vault, never shown again once stored."""
        has_key = bool(get_anthropic_key())
        field = QLineEdit()
        field.setEchoMode(QLineEdit.EchoMode.Password)
        field.setPlaceholderText("••••••••  " + t("settings.api_key_saved") if has_key else "sk-ant-…")
        field.setMinimumWidth(230)
        field.setAccessibleName(t("settings.api_key"))
        field.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

        def save() -> None:
            set_anthropic_key(field.text().strip() or None)
            self.ctx.rebuild()

        field.returnPressed.connect(save)
        controls = [field, Button(t("settings.save"), "secondary", on_click=save)]
        if has_key:
            controls.append(Button(t("settings.remove"), "ghost", on_click=lambda: (set_anthropic_key(None), self.ctx.rebuild())))
        hint = t("settings.api_key_hint_saved") if has_key else t("settings.api_key_hint")
        return _row(t("settings.api_key"), wrap(hbox(*controls, spacing=8)), hint)

    def _render_preview(self) -> None:
        while self.preview_holder.count():
            item = self.preview_holder.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.preview_holder.addWidget(FolderPreview(self.ctx.settings.destination, self.ctx.settings.layout))

    def _set(self, rebuild: bool = False, preview: bool = False, **changes: object) -> None:
        for k, v in changes.items():
            setattr(self.ctx.settings, k, v)
        self.ctx.settings.save()
        if preview:
            self._render_preview()
        if rebuild:
            self.ctx.rebuild()

    def _set_theme(self, pref: str) -> None:
        self.ctx.settings.theme = pref
        self.ctx.settings.save()
        self.ctx.theme.set_pref(pref)

    def _pick(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, t("settings.destination"), self.ctx.settings.destination)
        if folder:
            self._set(folder=folder, rebuild=True)


class PrivacyPage(QWidget):
    def __init__(self, ctx: Ctx, on_back: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        page, lay = _page(900)
        outer.addLayout(_centered(page))
        outer.addStretch(1)

        if on_back:
            back = Button(t("privacy.back"), "ghost", "sm", icon="arrow_right" if self.isRightToLeft() else "arrow_left", on_click=on_back)
            lay.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeading)
        lay.addWidget(page_header(t("privacy.title"), t("privacy.subtitle")))

        grid = QGridLayout()
        grid.setSpacing(14)
        for i, ((title, items), ic) in enumerate(zip(t("privacy.blocks"), ("eye", "send", "drive"))):
            card = Card(padding=20, spacing=10)
            b = icon_badge(ic, 36)
            b.setStyleSheet("border-radius: 10px;")
            card.lay.addWidget(b)
            card.add(label(title, "h2"))
            for it in items:
                row = hbox(icon_label("check", c("success"), 15), label(it, "muted", wrap=True), spacing=8)
                row.setAlignment(Qt.AlignmentFlag.AlignTop)
                card.lay.addLayout(row)
            card.lay.addStretch(1)
            grid.addWidget(card, 0, i)
            grid.setColumnStretch(i, 1)
        lay.addLayout(grid)

        if not on_back:
            danger = Card("DangerCard", padding=20, spacing=4)
            text = wrap(vbox(label(t("privacy.wipe_title"), "h2"), label(t("privacy.wipe_body"), "muted", wrap=True), spacing=4))
            row = hbox(text, Button(t("privacy.wipe"), "danger", icon="trash", on_click=self._confirm), spacing=24)
            row.setStretch(0, 1)
            danger.lay.addLayout(row)
            lay.addSpacing(8)
            lay.addWidget(danger)

    def _confirm(self) -> None:
        self.ctx.sign_out("wipe")


class BillingPage(QWidget):
    def __init__(self, ctx: Ctx) -> None:
        super().__init__()
        b = ctx.backend
        plan = b.plan
        usage = b.usage_this_month()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        page, lay = _page(820)
        outer.addLayout(_centered(page))
        outer.addStretch(1)
        actions = [Button(t("billing.refresh"), "secondary", icon="refresh", on_click=b.refresh_plan)] if plan.managed else []
        lay.addWidget(page_header(t("billing.title"), t("billing.subtitle"), actions))

        if not plan.managed:
            note = QFrame()
            note.setObjectName("Inset")
            note.setLayout(hbox(icon_label("settings", c("muted")), label(t("billing.unmanaged"), "muted", wrap=True), spacing=10, margins=(14, 10, 14, 10)))
            lay.addWidget(note)

        if plan.monthly_receipts is not None:
            limit = plan.monthly_receipts
            usage_card = Card(padding=20, spacing=10)
            usage_card.lay.addLayout(hbox(label(t("billing.usage")), None, label(t("billing.usage_of", used=min(usage, limit), limit=limit), "muted")))
            bar = QProgressBar()
            bar.setRange(0, limit)
            bar.setValue(min(usage, limit))
            bar.setTextVisible(False)
            usage_card.add(bar)
            lay.addWidget(usage_card)

        self.error = alert("")
        self.error.hide()
        lay.addWidget(self.error)
        b.billing_failed.connect(self._on_failed)

        # Prices come only from the billing server (GET /api/pricing): never written in the app.
        pricing = b.pricing
        if pricing is None and b.license is not None and hasattr(b.license, "pricing"):
            b.load_pricing_once()
        self.interval = getattr(ctx, "billing_interval", "monthly")
        if pricing:
            yearly = pricing["tiers"]["pro"]["prices"]["yearly"]
            toggle = Segmented(
                [("monthly", t("billing.monthly")), ("yearly", t("billing.yearly", percent=yearly["savingsPercent"]))],
                self.interval,
                t("billing.interval"),
            )

            def pick(v: str) -> None:
                ctx.billing_interval = v  # type: ignore[attr-defined]
                ctx.navigate("billing")

            toggle.changed.connect(pick)
            lay.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignHCenter)

        grid = QGridLayout()
        grid.setSpacing(14)
        for i, (name, features) in enumerate(t("billing.plans")):
            pro = i == 1
            current = pro == plan.is_pro
            card = Card("ProCard" if pro else "Card", padding=24, spacing=10)
            title = hbox(icon_label("sparkles", c("accent")) if pro else None, label(name, "h2"), None, spacing=8)
            if current and plan.managed:
                title.addWidget(badge(t("billing.current"), "accent"))
            card.lay.addLayout(title)
            if pricing:
                cur = pricing["currency"]
                if pro:
                    q = pricing["tiers"]["pro"]["prices"][self.interval]
                    amount, per = float(q["amount"]), t("billing.per_month") if self.interval == "monthly" else t("billing.per_year")
                else:
                    amount, per = 0.0, t("billing.per_month")
                card.lay.addLayout(hbox(label(money(amount, cur), "display"), label(per, "muted"), None, spacing=6))
                if pro and self.interval == "yearly":
                    q = pricing["tiers"]["pro"]["prices"]["yearly"]
                    card.add(label(t("billing.yearly_note", monthly=money(q["monthlyEquivalentMinor"] / 100, cur), saving=money(q["savingsMinor"] / 100, cur)), "small"))
            card.lay.addSpacing(6)
            for f in features:
                card.lay.addLayout(hbox(icon_label("check", c("accent"), 15), label(f), None, spacing=8))
            card.lay.addStretch(1)
            if pro and plan.managed:
                card.lay.addSpacing(10)
                if plan.is_pro:
                    card.add(Button(t("billing.manage"), "secondary", icon="card", on_click=b.open_billing_portal))
                    if plan.current_period_end:
                        from datetime import datetime

                        when = datetime.fromtimestamp(plan.current_period_end).strftime("%d/%m/%Y")
                        card.add(label(t("billing.renews", date=when), "small", align=Qt.AlignmentFlag.AlignHCenter))
                else:
                    btn = Button(t("billing.upgrade"), "primary", icon="sparkles", on_click=b.start_checkout)
                    btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                    card.add(btn, label(t("billing.secure_paypal" if b.billing_provider == "paypal" else "billing.secure"), "small", align=Qt.AlignmentFlag.AlignHCenter))
            grid.addWidget(card, 0, i)
            grid.setColumnStretch(i, 1)
        lay.addLayout(grid)

        if plan.managed and not plan.is_pro and b.license_keys_supported:
            key_card = Card(padding=20, spacing=8)
            key_card.add(label(t("billing.have_key"), "h2"), label(t("billing.key_hint"), "muted", wrap=True))
            field = QLineEdit()
            field.setPlaceholderText("KBL-XXXXX-XXXXX-XXXXX-XXXXX")
            field.setAccessibleName(t("billing.have_key"))
            field.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            field.setMaxLength(64)
            activate = Button(t("billing.activate"), "primary", icon="check", on_click=lambda: field.text().strip() and b.activate_license_key(field.text()))
            field.returnPressed.connect(activate.click)
            key_card.lay.addLayout(hbox(field, activate))
            lay.addWidget(key_card)

    def _on_failed(self, code: str) -> None:
        self.error.findChild(QLabel, "AlertText").setText(t(f"billing.errors.{code}"))
        self.error.show()


__all__ = ["ReviewPage", "SettingsPage", "PrivacyPage", "BillingPage", "QFrame"]
