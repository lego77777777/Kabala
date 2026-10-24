from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHeaderView,
    QLineEdit,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from kabala.i18n import is_rtl, money, money_totals, month_label, short_date, t
from kabala.models import Receipt, totals_by_currency
from kabala.ui import icons
from kabala.ui.components import BarChart, Ctx
from PySide6.QtWidgets import QDialog

from kabala.ui.export_dialog import ExportDialog
from kabala.ui.widgets import ConfirmDialog
from kabala.ui.scan import open_folder
from kabala.ui.widgets import Button, Card, Clickable, badge, c, empty_state, hbox, label, page_header, stat, vbox, wrap


class Dashboard(QWidget):
    def __init__(self, ctx: Ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.company: str | None = None
        receipts = ctx.backend.receipts
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        lay.addWidget(
            page_header(
                t("dashboard.title"),
                t("dashboard.subtitle"),
                [
                    Button(t("dashboard.open_folder"), "secondary", icon="folder", on_click=lambda: open_folder(ctx.settings.destination)),
                    Button(t("export.button"), "primary", icon="file",
                           on_click=self._export),
                ] if receipts else [],
            )
        )

        if not receipts:
            card = Card()
            card.add(empty_state("receipt", t("dashboard.empty_title"), t("dashboard.empty_body"), Button(t("scan.start"), icon="scan", on_click=lambda: ctx.navigate("scan"))))
            lay.addWidget(card)
            lay.addStretch(1)
            return

        today = date.today()
        this_month = f"{today.year}-{today.month:02d}"
        month_rs = [r for r in receipts if r.date.startswith(this_month)]
        all_totals = totals_by_currency(receipts)
        foreign = {k: v for k, v in all_totals.items() if k != "ILS"}

        stats = QGridLayout()
        stats.setSpacing(12)
        stats.addWidget(stat(t("dashboard.receipts"), str(len(receipts))), 0, 0)
        stats.addWidget(stat(t("dashboard.companies"), str(len({r.company for r in receipts}))), 0, 1)
        stats.addWidget(stat(t("dashboard.this_month"), money(totals_by_currency(month_rs).get("ILS", 0.0), "ILS"), t("dashboard.count", count=len(month_rs))), 0, 2)
        stats.addWidget(stat(t("dashboard.total"), money(all_totals.get("ILS", 0.0), "ILS"), f"+ {money_totals(foreign)}" if foreign else ""), 0, 3)
        lay.addLayout(stats)

        # chart + companies
        months: dict[str, float] = {}
        for r in receipts:
            if r.currency == "ILS":
                months[r.date[:7]] = months.get(r.date[:7], 0.0) + r.amount
        chart_card = Card(padding=20, spacing=10)
        chart_card.lay.addLayout(hbox(label(t("dashboard.monthly"), "h2"), None, label(t("dashboard.monthly_note"), "small")))
        chart_card.add(BarChart([(month_label(m), v) for m, v in sorted(months.items())], lambda v: money(v, "ILS"), is_rtl()))

        comp_card = Card(padding=8, spacing=0)
        comp_card.setFixedWidth(300)
        comp_card.lay.addWidget(label(t("dashboard.by_company"), "h2"))
        comp_card.lay.itemAt(0).widget().setContentsMargins(12, 10, 12, 8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self.rows_lay = vbox(spacing=1)
        inner.setLayout(self.rows_lay)
        scroll.setWidget(inner)
        comp_card.add(scroll)
        self.rows: dict[str | None, Clickable] = {}
        self._add_company_row(None, t("dashboard.all_companies"), str(len(receipts)), None)
        groups: dict[str, list[Receipt]] = {}
        for r in receipts:
            groups.setdefault(r.company, []).append(r)
        for name, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            self._add_company_row(name, name, money_totals(totals_by_currency(rs)), len(rs))
        self.rows_lay.addStretch(1)

        mid = hbox(chart_card, comp_card, spacing=14)
        mid.setStretch(0, 1)
        mid_w = wrap(mid)
        mid_w.setFixedHeight(310)
        lay.addWidget(mid_w)

        # table
        table_card = Card(padding=0, spacing=0)
        self.table_title = label("", "h2")
        self.search = QLineEdit()
        self.search.setPlaceholderText(t("dashboard.search"))
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(340)
        self.search.addAction(icons.icon("search", c("muted")), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setAccessibleName(t("dashboard.search"))
        self.search.textChanged.connect(self._refresh_table)
        table_card.lay.addLayout(hbox(self.table_title, None, self.search, margins=(18, 14, 14, 14)))

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(t("dashboard.cols"))
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.table.verticalHeader().setDefaultSectionSize(40)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignVCenter)
        self.table.setMinimumHeight(380)
        self.table.cellDoubleClicked.connect(self._open_row)
        self.table.activated.connect(lambda idx: self._open_row(idx.row(), idx.column()))  # Enter key
        self.shown: list[Receipt] = []
        self.empty = empty_state("search", t("dashboard.no_results"), "")
        self.empty.hide()
        table_card.add(self.table, self.empty)
        lay.addWidget(table_card)

        # "/" and Ctrl+F jump to search
        for key in ("/", QKeySequence.StandardKey.Find):
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(self.search.setFocus)

        self._select(None)

    def _export(self) -> None:
        """Export is a Pro feature; Free users get a short explanation and a way to upgrade."""
        if not self.ctx.backend.plan.export:
            dlg = ConfirmDialog(self, t("export.pro_title"), t("export.pro_body"), t("billing.upgrade"), t("privacy.cancel"), danger=False)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.ctx.navigate("billing")
            return
        ExportDialog(self, self.ctx.backend.receipts, self.ctx.settings.destination).exec()

    def _open_row(self, row: int, _col: int) -> None:
        if 0 <= row < len(self.shown) and self.shown[row].file_path and Path(self.shown[row].file_path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.shown[row].file_path))

    def _add_company_row(self, key: str | None, name: str, meta: str, count: int | None) -> None:
        row = Clickable("Row")
        row.setFixedHeight(36)
        row.setAccessibleName(name)
        n = label(name)
        n.setStyleSheet("font-weight: 500;")
        items = [n, None]
        if count is not None:
            items.append(label(f"×{count}", "small"))
        items.append(label(meta, "small"))
        row.setLayout(hbox(*items, spacing=8, margins=(12, 0, 12, 0)))
        row.clicked.connect(lambda k=key: self._select(None if k == self.company else k))
        self.rows[key] = row
        self.rows_lay.addWidget(row)

    def _select(self, company: str | None) -> None:
        self.company = company
        for k, row in self.rows.items():
            row.set_active(k == company)
        self._refresh_table()

    def _refresh_table(self) -> None:
        q = self.search.text().strip().lower()
        rs = [
            r
            for r in reversed(self.ctx.backend.receipts)
            if (self.company is None or r.company == self.company)
            and (not q or q in r.company.lower() or q in r.subject.lower() or q in (r.invoice_number or "") or q in f"{r.amount:.2f}")
        ]
        self.shown = rs
        self.table_title.setText(f"{self.company or t('dashboard.recent')}  ·  {len(rs)}")
        self.table.setVisible(bool(rs))
        self.empty.setVisible(not rs)
        self.table.setRowCount(len(rs))
        for i, r in enumerate(rs):
            cells = [short_date(r.date), r.company, r.invoice_number or "—", None, money(r.amount, r.currency)]
            for col, text in enumerate(cells):
                if text is None:
                    tone = "success" if r.confidence >= 0.9 else "neutral"
                    holder = wrap(hbox(badge(f"{round(r.confidence * 100)}%", tone), None, margins=(12, 0, 12, 0)))
                    self.table.setCellWidget(i, col, holder)
                    continue
                item = QTableWidgetItem(text)
                align = Qt.AlignmentFlag.AlignTrailing if col == 4 else Qt.AlignmentFlag.AlignLeading
                item.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
                if col == 1 or col == 4:
                    f = item.font()
                    f.setWeight(f.Weight.DemiBold)
                    item.setFont(f)
                else:
                    item.setForeground(self.palette().placeholderText())
                self.table.setItem(i, col, item)
