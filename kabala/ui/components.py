"""Composite widgets: folder preview tree, monthly bar chart."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import PurePath
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QToolTip, QVBoxLayout, QWidget

from kabala.backend import Backend
from kabala.paths import receipt_path_segments
from kabala.settings import Settings
from kabala.theme import Theme
from kabala.ui import icons
from kabala.ui.widgets import c, hbox, icon_label, label


@dataclass
class Ctx:
    """What every screen needs: state, preferences, and how to move around."""

    backend: Backend
    settings: Settings
    theme: Theme
    navigate: Callable[[str], None]
    rebuild: Callable[[], None]
    #: asks for confirmation, then signs out ("disconnect") or signs out and deletes local data ("wipe")
    sign_out: Callable[[str], None] = lambda _kind: None


class FolderPreview(QFrame):
    def __init__(self, folder: str, layout: str) -> None:
        super().__init__()
        self.setObjectName("Inset")
        # Paths are always shown left-to-right, even in the Hebrew UI.
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(2)
        root = PurePath(folder).name or "Receipts"
        sample = date.today().replace(day=14).isoformat()
        segments = [root, *receipt_path_segments("Netflix", sample, 54.9, "ILS", layout)]  # type: ignore[arg-type]
        for i, seg in enumerate(segments):
            is_file = i == len(segments) - 1
            text = label(seg, "mono")
            if is_file:
                text.setStyleSheet(f"color: {c('fg')};")
            row = hbox(i * 16, icon_label("file" if is_file else "folder", c("accent") if is_file else c("muted"), 14), text, None, spacing=6)
            lay.addLayout(row)


class BarChart(QWidget):
    """Monthly totals. Mirrors itself in RTL (latest month on the left, axis on the right)."""

    def __init__(self, data: list[tuple[str, float]], fmt: Callable[[float], str], rtl: bool) -> None:
        super().__init__()
        self.data = list(reversed(data)) if rtl else data
        self.fmt = fmt
        self.rtl = rtl
        self.hover = -1
        self.setMinimumHeight(220)
        self.setMouseTracking(True)
        self._bars: list[QRectF] = []

    def _nice_max(self) -> float:
        m = max((v for _, v in self.data), default=0) or 1
        step = 10 ** (len(str(int(m))) - 1)
        return ((m // step) + 1) * step

    def paintEvent(self, _: object) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        axis_w, bottom = 56, 26
        plot = QRectF(0 if self.rtl else axis_w, 6, w - axis_w, h - bottom - 6)
        top = self._nice_max()
        small = QFont(self.font())
        small.setPointSizeF(8.5)
        p.setFont(small)

        # grid + y labels
        for i in range(5):
            y = plot.bottom() - plot.height() * i / 4
            pen = QPen(QColor(c("border")))
            pen.setStyle(Qt.PenStyle.DashLine if i else Qt.PenStyle.SolidLine)
            p.setPen(pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            v = top * i / 4
            text = f"₪{v / 1000:.1f}k" if v >= 1000 else f"₪{v:.0f}"
            p.setPen(QColor(c("muted")))
            ax = QRectF(plot.right() + 6, y - 8, axis_w - 6, 16) if self.rtl else QRectF(0, y - 8, axis_w - 8, 16)
            p.drawText(ax, (Qt.AlignmentFlag.AlignLeft if self.rtl else Qt.AlignmentFlag.AlignRight) | Qt.AlignmentFlag.AlignVCenter, text)

        # bars
        self._bars = []
        n = max(len(self.data), 1)
        slot = plot.width() / n
        bw = min(40.0, slot * 0.56)
        for i, (lbl, v) in enumerate(self.data):
            x = plot.left() + slot * i + (slot - bw) / 2
            bh = plot.height() * (v / top)
            rect = QRectF(x, plot.bottom() - bh, bw, bh)
            self._bars.append(QRectF(plot.left() + slot * i, plot.top(), slot, plot.height()))
            if i == self.hover:
                p.fillRect(self._bars[-1], QColor(c("surface2")))
            path = QPainterPath()
            r = min(6.0, bw / 2, bh)
            path.addRoundedRect(rect.adjusted(0, 0, 0, r), r, r)
            p.save()
            p.setClipRect(QRectF(rect.left(), rect.top(), rect.width(), rect.height()))
            p.fillPath(path, QColor(c("accent")))
            p.restore()
            p.setPen(QColor(c("muted")))
            p.drawText(QRectF(plot.left() + slot * i, plot.bottom() + 4, slot, bottom - 4), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, lbl)
        p.end()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        pos = e.position()
        idx = next((i for i, r in enumerate(self._bars) if r.contains(pos)), -1)
        if idx != self.hover:
            self.hover = idx
            self.update()
        if idx >= 0:
            lbl, v = self.data[idx]
            QToolTip.showText(e.globalPosition().toPoint(), f"{lbl}\n{self.fmt(v)}", self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, _: object) -> None:
        self.hover = -1
        self.update()


def month_bars(rows: list[tuple[str, float, str]]) -> QWidget:
    """Compact horizontal bars for the scan summary: (label, ILS value, formatted text)."""
    from PySide6.QtWidgets import QGridLayout, QProgressBar

    w = QWidget()
    grid = QGridLayout(w)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(12)
    grid.setVerticalSpacing(8)
    top = max((v for _, v, _ in rows), default=1) or 1
    for i, (lbl, v, text) in enumerate(rows):
        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setValue(round(v / top * 1000))
        bar.setTextVisible(False)
        grid.addWidget(label(lbl, "muted"), i, 0)
        grid.addWidget(bar, i, 1)
        val = label(text)
        val.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignTrailing)
        grid.addWidget(val, i, 2)
    grid.setColumnStretch(1, 1)
    return w


__all__ = ["Ctx", "FolderPreview", "BarChart", "month_bars", "icons"]
