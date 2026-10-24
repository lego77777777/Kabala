from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QProgressBar, QStackedWidget, QVBoxLayout, QWidget

from kabala.i18n import money, money_totals, month_label, t
from kabala.models import ScanOptions, ScanProgress, ScanSummary
from kabala.ui import icons
from kabala.ui.components import Ctx, month_bars
from kabala.ui.widgets import Button, Card, Clickable, alert, c, hbox, icon_badge, icon_label, label, ltr, stat, vbox


def open_folder(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


class ScanPage(QWidget):
    def __init__(self, ctx: Ctx, auto_start: bool) -> None:
        super().__init__()
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        self.col = QWidget()
        self.col.setMaximumWidth(600)
        self.col.setMinimumWidth(520)
        self.col_lay = QVBoxLayout(self.col)
        self.col_lay.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.col, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(2)

        self.stack = QStackedWidget()
        self.col_lay.addWidget(self.stack)
        self.stack.addWidget(self._idle())
        self.stack.addWidget(self._running())
        self.error_panel = self._error()
        self.stack.addWidget(self.error_panel)
        self.done_holder = QWidget()
        self.stack.addWidget(self.done_holder)

        b = ctx.backend
        b.progress.connect(self._on_progress)
        b.scan_finished.connect(self._on_finished)
        b.scan_failed.connect(self._on_failed)

        if b.scanning:
            self.stack.setCurrentIndex(1)
            if b.last_progress:
                self._on_progress(b.last_progress)
        elif auto_start:
            self._start()

    # ---- panels -------------------------------------------------------------

    def _idle(self) -> QWidget:
        card = Card(shadow=True, padding=32, spacing=6)
        card.lay.addWidget(icon_badge("scan"), alignment=Qt.AlignmentFlag.AlignHCenter)
        card.lay.addSpacing(10)
        card.add(label(t("scan.title"), "h1", align=Qt.AlignmentFlag.AlignHCenter))
        card.add(label(t("scan.idle_body"), "muted", wrap=True, align=Qt.AlignmentFlag.AlignHCenter))
        folder = label(ltr(self.ctx.settings.destination), align=Qt.AlignmentFlag.AlignHCenter)
        folder.setStyleSheet("font-weight: 600;")
        card.add(folder)
        card.lay.addSpacing(18)
        btn = Button(t("scan.start"), "primary", "lg", icon="scan", on_click=self._start)
        btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        card.add(btn)
        return card

    def _running(self) -> QWidget:
        card = Card(shadow=True, padding=32, spacing=8)
        self.phase = label(t("scan.waiting"), "h2")
        self.phase.setAccessibleName("scan-phase")
        self.pct = label("0%", "muted")
        card.lay.addLayout(hbox(self.phase, None, self.pct))
        card.lay.addSpacing(6)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName(t("scan.title"))
        card.add(self.bar)
        self.processed = label(" ", "small")
        card.add(self.processed)
        card.lay.addSpacing(20)

        grid = QGridLayout()
        grid.setSpacing(14)
        found_box, self.found = self._inset(t("scan.found"), "0", "big")
        last_box, self.last = self._inset(t("scan.last_company"), "—", "h2")
        grid.addWidget(found_box, 0, 0)
        grid.addWidget(last_box, 0, 1)
        card.lay.addLayout(grid)
        card.lay.addSpacing(16)
        card.lay.addLayout(hbox(label(t("scan.keep_open"), "small"), None, Button(t("scan.cancel"), "ghost", "sm", icon="stop", on_click=self.ctx.backend.cancel_scan)))
        return card

    def _inset(self, title: str, value: str, role: str) -> tuple[QFrame, QLabel]:
        box = QFrame()
        box.setObjectName("Inset")
        val = label(value, role)
        box.setLayout(vbox(label(title, "small"), val, None, spacing=6, margins=(16, 14, 16, 14)))
        box.setMinimumHeight(104)
        return box, val

    def _error(self) -> QWidget:
        card = Card(shadow=True, padding=32, spacing=12)
        big = QLabel()
        big.setPixmap(icons.pixmap("alert", c("danger"), 40))
        card.lay.addWidget(big, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.error_box = alert(t("scan.error"))
        card.add(self.error_box)
        self.relogin_btn = Button(t("scan.relogin"), "secondary", icon="logout", on_click=self.ctx.backend.disconnect_account)
        row = hbox(None, self.relogin_btn, Button(t("scan.retry"), icon="refresh", on_click=self._start), None)
        card.lay.addLayout(row)
        return card

    def _done(self, s: ScanSummary) -> QWidget:
        w = QWidget()
        lay = vbox(spacing=12)
        w.setLayout(lay)
        ok = QLabel()
        ok.setPixmap(icons.pixmap("check_circle", c("success"), 48))
        lay.addWidget(ok, alignment=Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(label(t("summary.canceled_title") if s.canceled else t("summary.title"), "h1", align=Qt.AlignmentFlag.AlignHCenter))
        lay.addWidget(label(f"{t('summary.subtitle')} {ltr(s.folder)}", "muted", wrap=True, align=Qt.AlignmentFlag.AlignHCenter))
        lay.addSpacing(6)

        stats = hbox(
            stat(t("summary.receipts"), str(s.receipts)),
            stat(t("summary.companies"), str(s.companies)),
            stat(t("summary.total"), money(s.totals_by_currency.get("ILS", 0.0), "ILS"), money_totals({k: v for k, v in s.totals_by_currency.items() if k != "ILS"}) if len(s.totals_by_currency) > 1 else ""),
            spacing=12,
        )
        lay.addLayout(stats)

        if s.by_month:
            card = Card(padding=20, spacing=12)
            card.add(label(t("summary.by_month"), "muted"))
            rows = []
            for m, totals in s.by_month:
                ils = totals.get("ILS", 0.0)
                extra = money_totals({k: v for k, v in totals.items() if k != "ILS"}) if len(totals) > (1 if "ILS" in totals else 0) else ""
                rows.append((month_label(m), ils, money(ils, "ILS") + (f"  + {extra}" if extra else "")))
            card.add(month_bars(rows))
            lay.addWidget(card)

        if s.needs_review:
            warn = Clickable("Warn")
            text = label(t("summary.review", count=s.needs_review))
            text.setObjectName("WarnText")
            cta = label(t("summary.review_cta"))
            cta.setObjectName("WarnText")
            cta.setStyleSheet("font-weight: 600;")
            warn.setLayout(hbox(icon_label("inbox", c("warning")), text, None, cta, icon_label("arrow_left", c("warning")) if self.isRightToLeft() else icon_label("arrow_right", c("warning")), spacing=10, margins=(16, 12, 16, 12)))
            warn.clicked.connect(lambda: self.ctx.navigate("review"))
            lay.addWidget(warn)

        if s.limited:
            limit = Clickable("Warn")
            text = label(t("summary.limited"), wrap=True)
            text.setObjectName("WarnText")
            cta = label(t("billing.upgrade"))
            cta.setObjectName("WarnText")
            cta.setStyleSheet("font-weight: 600;")
            row = hbox(icon_label("sparkles", c("warning")), text, cta, spacing=10, margins=(16, 12, 16, 12))
            row.setStretch(1, 1)
            limit.setLayout(row)
            limit.clicked.connect(lambda: self.ctx.navigate("billing"))
            lay.addWidget(limit)

        lay.addSpacing(6)
        lay.addLayout(
            hbox(
                Button(t("summary.open_folder"), "primary", "lg", icon="folder", on_click=lambda: open_folder(s.folder)),
                Button(t("summary.to_dashboard"), "secondary", "lg", on_click=lambda: self.ctx.navigate("dashboard")),
                spacing=12,
            )
        )
        return w

    # ---- events -------------------------------------------------------------

    def _start(self) -> None:
        st = self.ctx.settings
        self.found.setText("0")
        self.last.setText("—")
        self.bar.setValue(0)
        self.stack.setCurrentIndex(1)
        self.ctx.backend.start_scan(ScanOptions(folder=st.destination, layout=st.layout, threshold=st.threshold))  # type: ignore[arg-type]

    def _on_progress(self, p: ScanProgress) -> None:
        ratio = p.processed / p.total if p.total else 0
        self.phase.setText(t(f"scan.phase.{p.phase}"))
        self.pct.setText(f"{round(ratio * 100)}%")
        self.bar.setValue(round(ratio * 1000))
        self.processed.setText(t("scan.processed", processed=p.processed, total=p.total))
        self.found.setText(str(p.found))
        self.last.setText(p.last_company or "—")

    def _on_failed(self, code: str) -> None:
        self.error_box.findChild(QLabel, "AlertText").setText(t("scan.errors").get(code, t("scan.error")))
        self.relogin_btn.setVisible(code == "auth")
        self.stack.setCurrentIndex(2)

    def _on_finished(self, s: ScanSummary) -> None:
        old = self.done_holder
        self.done_holder = self._done(s)
        self.stack.insertWidget(3, self.done_holder)
        self.stack.removeWidget(old)
        old.deleteLater()
        self.stack.setCurrentIndex(3)
