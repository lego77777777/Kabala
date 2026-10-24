from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QComboBox, QDialog, QFileDialog, QWidget

from kabala.export import PERIODS, export_csv, export_xlsx, filter_period
from kabala.i18n import t
from kabala.models import Receipt
from kabala.ui.widgets import Button, Segmented, alert, badge, hbox, label, vbox


class ExportDialog(QDialog):
    def __init__(self, parent: QWidget, receipts: list[Receipt], folder: str) -> None:
        super().__init__(parent)
        self.receipts = receipts
        self.folder = folder
        self.setWindowTitle(t("export.title"))
        self.setModal(True)
        self.setMinimumWidth(460)
        lay = vbox(spacing=10, margins=(24, 22, 24, 20))
        self.setLayout(lay)
        lay.addLayout(hbox(label(t("export.title"), "h2"), badge("Pro", "accent"), None, spacing=8))
        lay.addWidget(label(t("export.subtitle"), "muted", wrap=True))
        lay.addSpacing(8)

        self.period = QComboBox()
        for key in PERIODS:
            self.period.addItem(t(f"export.periods.{key}"), key)
        self.period.setAccessibleName(t("export.period"))
        self.period.currentIndexChanged.connect(self._update_count)
        lay.addLayout(hbox(label(t("export.period")), None, self.period))

        self.fmt = Segmented([("xlsx", "Excel"), ("csv", "CSV")], "xlsx", t("export.format"))
        lay.addLayout(hbox(label(t("export.format")), None, self.fmt))

        self.count = label("", "small")
        lay.addWidget(self.count)
        self.error = alert(t("export.error"))
        self.error.hide()
        lay.addWidget(self.error)
        lay.addSpacing(10)

        cancel = Button(t("privacy.cancel"), "secondary", on_click=self.reject)
        self.go = Button(t("export.button"), "primary", icon="file", on_click=self._export)
        self.go.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.go.setDefault(True)
        lay.addLayout(hbox(None, cancel, self.go))
        self._format = "xlsx"
        self.fmt.changed.connect(lambda v: setattr(self, "_format", v))
        self._update_count()

    def _rows(self) -> list[Receipt]:
        return filter_period(self.receipts, self.period.currentData())

    def _update_count(self) -> None:
        n = len(self._rows())
        self.count.setText(t("export.count_rows", count=n))
        self.go.setEnabled(n > 0)

    def _export(self) -> None:
        ext = self._format
        default = Path(self.folder) / f"Kabala_{self.period.currentData()}_{date.today():%Y-%m-%d}.{ext}"
        filters = "Excel (*.xlsx)" if ext == "xlsx" else "CSV (*.csv)"
        path, _ = QFileDialog.getSaveFileName(self, t("export.title"), str(default), filters)
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() != f".{ext}":
            target = target.with_suffix(f".{ext}")
        try:
            (export_xlsx if ext == "xlsx" else export_csv)(self._rows(), target)
        except OSError:
            # Most often: the file is open in Excel.
            self.error.show()
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent)))
        self.accept()
