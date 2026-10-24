"""The design system: buttons, cards, segmented controls, empty states and friends."""

from __future__ import annotations

import re
from typing import Callable, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from kabala.i18n import is_rtl
from kabala.ui import icons

# Set once at startup by the app; widgets read colors from it when they are built.
theme = None  # type: ignore[assignment]


def c(key: str) -> str:
    return theme.c[key]  # type: ignore[union-attr]


def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)
    for child in w.findChildren(QWidget):
        child.style().unpolish(child)
        child.style().polish(child)


_HEBREW = re.compile(r"[֐-׿]")


def bidi(text: str) -> str:
    """
    A Hebrew sentence that starts with a Latin word ("Kabala ימשיך…") would be laid out left-to-right
    by the Unicode bidi algorithm. A leading RLM makes the paragraph right-to-left in the Hebrew UI.
    """
    return f"‏{text}" if is_rtl() and _HEBREW.search(text) and not text.startswith("‏") else text


def label(text: str = "", role: str | None = None, wrap: bool = False, align: Qt.AlignmentFlag | None = None) -> QLabel:
    lbl = QLabel(bidi(text))
    if role:
        lbl.setProperty("role", role)
    lbl.setWordWrap(wrap)
    if align is None:
        # Qt aligns each label by its own text's direction, so an English subject in the Hebrew UI
        # would hug the left edge. Pin labels to the UI's reading side instead.
        side = Qt.AlignmentFlag.AlignRight if is_rtl() else Qt.AlignmentFlag.AlignLeft
        align = side | Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignVCenter
    lbl.setAlignment(align)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    return lbl


def ltr(text: str) -> str:
    """Isolates an LTR fragment (email, path) inside RTL text."""
    return f"⁦{text}⁩"


def hline() -> QFrame:
    f = QFrame()
    f.setObjectName("HLine")
    f.setFixedHeight(1)
    return f


def vline() -> QFrame:
    f = QFrame()
    f.setObjectName("VLine")
    f.setFixedWidth(1)
    return f


def vbox(*items: QWidget | int | None, spacing: int = 8, margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> QVBoxLayout:
    lay = QVBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        else:
            lay.addWidget(it)
    return lay


def hbox(*items: QWidget | int | None, spacing: int = 8, margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        else:
            lay.addWidget(it)
    return lay


def wrap(layout: QHBoxLayout | QVBoxLayout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w


class Button(QPushButton):
    def __init__(
        self,
        text: str = "",
        variant: str = "primary",
        size: str = "md",
        icon: str | None = None,
        on_click: Callable[[], object] | None = None,
    ) -> None:
        # A leading space sits between the icon and the label in both LTR and RTL.
        text = bidi(text)
        super().__init__(f" {text}" if icon and text else text)
        self.setProperty("variant", variant)
        self.setProperty("size", size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Only keyboard focus shows a ring, like :focus-visible on the web.
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        if icon:
            fg = {"primary": c("accent_fg"), "danger": c("danger_fg"), "link": c("accent")}.get(variant, c("fg"))
            px = 18 if size == "lg" else 16
            self.setIcon(icons.icon(icon, fg, px))
            self.setIconSize(QSize(px, px))
        if on_click:
            self.clicked.connect(lambda: on_click())


class Card(QFrame):
    def __init__(self, name: str = "Card", shadow: bool = False, padding: int = 20, spacing: int = 12) -> None:
        super().__init__()
        self.setObjectName(name)
        # "Elevated" cards get a slightly stronger border instead of a drop shadow:
        # QGraphicsDropShadowEffect renders poorly with rounded stylesheet frames and is slow.
        self.setProperty("elevated", shadow)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(padding, padding, padding, padding)
        self.lay.setSpacing(spacing)

    def add(self, *widgets: QWidget) -> "Card":
        for w in widgets:
            self.lay.addWidget(w)
        return self


class Clickable(QFrame):
    """A focusable, clickable container (nav items, list rows). Enter/Space activate it."""

    clicked = Signal()

    def __init__(self, name: str) -> None:
        super().__init__()
        self.setObjectName(name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setProperty("active", False)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", active)
        self.setAccessibleDescription("selected" if active else "")
        repolish(self)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(e)


class Segmented(QFrame):
    changed = Signal(str)

    def __init__(self, options: Sequence[tuple[str, str]], value: str, accessible_name: str = "") -> None:
        super().__init__()
        self.setObjectName("Segmented")
        self.setAccessibleName(accessible_name)
        lay = hbox(spacing=2, margins=(3, 3, 3, 3))
        self.setLayout(lay)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for key, text in options:
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(key == value)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            b.setProperty("key", key)
            self.group.addButton(b)
            lay.addWidget(b)
        self.group.buttonClicked.connect(lambda b: self.changed.emit(b.property("key")))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


def fit_pill(lbl: QLabel, point_size: float = 8.5, h_pad: int = 18, min_width: int = 0) -> QLabel:
    """Stylesheet padding isn't reliably part of a QLabel's size hint, so pills are sized explicitly."""
    f = QFont(lbl.font())
    f.setPointSizeF(point_size)
    fm = QFontMetrics(f)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setFixedSize(max(min_width, fm.horizontalAdvance(lbl.text()) + h_pad), fm.height() + 6)
    return lbl


def badge(text: str, tone: str = "neutral") -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("badge", tone)
    return fit_pill(lbl)


def icon_badge(name: str, size: int = 56, color: str | None = None) -> QLabel:
    lbl = QLabel()
    lbl.setObjectName("IconBadge")
    lbl.setFixedSize(size, size)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setPixmap(icons.pixmap(name, color or c("accent"), size // 2 - 4))
    return lbl


def icon_label(name: str, color: str, size: int = 16) -> QLabel:
    lbl = QLabel()
    lbl.setPixmap(icons.pixmap(name, color, size))
    lbl.setFixedSize(size, size)
    return lbl


def empty_state(icon_name: str, title: str, body: str, action: QWidget | None = None) -> QWidget:
    w = QWidget()
    lay = vbox(spacing=6, margins=(24, 56, 24, 56))
    w.setLayout(lay)
    badge_ = icon_badge(icon_name)
    lay.addWidget(badge_, alignment=Qt.AlignmentFlag.AlignHCenter)
    lay.addSpacing(10)
    lay.addWidget(label(title, "h2", align=Qt.AlignmentFlag.AlignHCenter))
    if body:
        lay.addWidget(label(body, "muted", wrap=True, align=Qt.AlignmentFlag.AlignHCenter))
    if action:
        lay.addSpacing(16)
        lay.addWidget(action, alignment=Qt.AlignmentFlag.AlignHCenter)
    return w


def stat(title: str, value: str, hint: str = "") -> Card:
    card = Card(padding=18, spacing=4)
    card.add(label(title, "muted"))
    v = label(value, "stat")
    card.add(v)
    if hint:
        card.add(label(hint, "small"))
    card.lay.addStretch(1)
    return card


def page_header(title: str, subtitle: str = "", actions: Sequence[QWidget] = ()) -> QWidget:
    left = wrap(vbox(label(title, "h1"), label(subtitle, "muted") if subtitle else None, spacing=4))
    row = hbox(left, None, *actions, spacing=8, margins=(0, 0, 0, 20))
    row.setAlignment(Qt.AlignmentFlag.AlignBottom)
    return wrap(row)


def alert(text: str) -> QFrame:
    f = QFrame()
    f.setObjectName("Alert")
    lbl = label(text, wrap=True)
    lbl.setObjectName("AlertText")
    f.setLayout(hbox(icon_label("alert", c("danger")), lbl, spacing=10, margins=(14, 10, 14, 10)))
    f.setAccessibleName(text)
    return f


class ConfirmDialog(QDialog):
    """Modal confirmation. Esc cancels; Cancel has initial focus so Enter is never destructive."""

    def __init__(self, parent: QWidget, title: str, body: str, confirm: str, cancel: str, danger: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)
        lay = vbox(spacing=8, margins=(24, 22, 24, 20))
        self.setLayout(lay)
        lay.addWidget(label(title, "h2"))
        lay.addWidget(label(body, "muted", wrap=True))
        lay.addSpacing(16)
        cancel_btn = Button(cancel, "secondary", on_click=self.reject)
        ok_btn = Button(confirm, "danger" if danger else "primary", icon="trash" if danger else None, on_click=self.accept)
        lay.addLayout(hbox(None, cancel_btn, ok_btn))
        cancel_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        cancel_btn.setDefault(True)
        cancel_btn.setFocus()
