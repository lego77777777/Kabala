"""Design tokens (light/dark), the app-wide stylesheet, and system theme tracking."""

from __future__ import annotations

from string import Template

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

LIGHT = {
    "bg": "#fafaf9",
    "sidebar": "#f3f3f4",
    "surface": "#ffffff",
    "surface2": "#f4f4f5",
    "border": "#e4e4e7",
    "fg": "#18181b",
    "muted": "#64646e",
    "accent": "#4f46e5",
    "accent_fg": "#ffffff",
    "accent_soft": "#eeedfe",
    "success": "#15803d",
    "warning": "#b45309",
    "danger": "#c81e1e",
    "danger_fg": "#ffffff",
}

DARK = {
    "bg": "#0d0d10",
    "sidebar": "#121216",
    "surface": "#16161b",
    "surface2": "#1f1f26",
    "border": "#2e2e38",
    "fg": "#ececf1",
    "muted": "#9c9caa",
    "accent": "#635bff",
    "accent_fg": "#ffffff",
    "accent_soft": "#252348",
    "success": "#4ade80",
    "warning": "#fbbf24",
    "danger": "#f87171",
    "danger_fg": "#0d0d10",
}


def mix(a: str, b: str, t: float) -> str:
    ca, cb = QColor(a), QColor(b)
    return QColor(
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t),
    ).name()


def _derived(c: dict[str, str]) -> dict[str, str]:
    d = dict(c)
    d["accent_hover"] = mix(c["accent"], "#ffffff", 0.12)
    d["accent_pressed"] = mix(c["accent"], "#000000", 0.12)
    d["success_soft"] = mix(c["surface"], c["success"], 0.12)
    d["warning_soft"] = mix(c["surface"], c["warning"], 0.12)
    d["danger_soft"] = mix(c["surface"], c["danger"], 0.12)
    d["border_soft"] = mix(c["surface"], c["border"], 0.6)
    d["focus_ring"] = mix(c["accent"], c["fg"], 0.5)
    d["border_strong"] = mix(c["border"], c["fg"], 0.1)
    d["sidebar_hover"] = mix(c["sidebar"], c["surface"], 0.6)
    return d


QSS = Template(
    """
* { outline: none; }
QWidget { color: $fg; }
QMainWindow, #Root, #Content { background: $bg; }
#Sidebar { background: $sidebar; }
#VLine { background: $border; }
#HLine { background: $border; }
QLabel { background: transparent; }
QLabel[role="h1"] { font-size: 20pt; font-weight: 600; }
QLabel[role="display"] { font-size: 23pt; font-weight: 600; }
QLabel[role="h2"] { font-size: 12pt; font-weight: 600; }
QLabel[role="muted"] { color: $muted; }
QLabel[role="small"] { color: $muted; font-size: 8.5pt; }
QLabel[role="section"] { color: $muted; font-size: 8.5pt; font-weight: 600; }
QLabel[role="stat"] { font-size: 18pt; font-weight: 600; }
QLabel[role="big"] { font-size: 30pt; font-weight: 600; }
QLabel[role="mono"] { color: $muted; font-family: "Cascadia Mono", "Consolas", "Menlo", monospace; font-size: 9pt; }
QLabel[role="success"] { color: $success; font-weight: 600; }
QLabel[role="link"] { color: $accent; font-weight: 500; }

QFrame#Card { background: $surface; border: 1px solid $border; border-radius: 16px; }
QFrame#Card[elevated="true"] { border: 1px solid $border_strong; }
QFrame#Inset { background: $surface2; border: none; border-radius: 12px; }
QFrame#SuccessBox { background: $success_soft; border: none; border-radius: 12px; }
QFrame#DangerCard { background: $surface; border: 1px solid $danger_soft; border-radius: 16px; }
QFrame#ProCard { background: $surface; border: 2px solid $accent; border-radius: 16px; }
QFrame#Banner { background: $warning_soft; border: none; }
QLabel#BannerText { color: $warning; font-size: 8.5pt; }
QFrame#Warn { background: $warning_soft; border: none; border-radius: 12px; }
QLabel#WarnText { color: $warning; }
QFrame#Alert { background: $danger_soft; border: none; border-radius: 10px; }
QLabel#AlertText { color: $danger; }
QLabel#IconBadge { background: $accent_soft; border-radius: 14px; }
QLabel#Avatar { background: $accent_soft; color: $accent; border-radius: 16px; font-weight: 600; }

QPushButton { border: none; border-radius: 10px; padding: 0 16px; min-height: 38px; font-weight: 500; }
QPushButton:focus { border: 2px solid $accent; }
QPushButton[variant="primary"] { background: $accent; color: $accent_fg; }
QPushButton[variant="primary"]:hover { background: $accent_hover; }
QPushButton[variant="primary"]:pressed { background: $accent_pressed; }
QPushButton[variant="primary"]:focus { border: 2px solid $focus_ring; }
QPushButton[variant="secondary"] { background: $surface; color: $fg; border: 1px solid $border; }
QPushButton[variant="secondary"]:hover { background: $surface2; }
QPushButton[variant="secondary"]:focus { border: 2px solid $accent; }
QPushButton[variant="ghost"] { background: transparent; color: $fg; }
QPushButton[variant="ghost"]:hover { background: $surface2; }
QPushButton[variant="link"] { background: transparent; color: $accent; padding: 0 4px; min-height: 22px; }
QPushButton[variant="link"]:hover { text-decoration: underline; }
QPushButton[variant="danger"] { background: $danger; color: $danger_fg; }
QPushButton[variant="danger"]:hover { background: $danger; }
QPushButton:disabled { background: $surface2; color: $muted; border: 1px solid $border; }
QPushButton[size="lg"] { min-height: 52px; font-size: 11pt; font-weight: 600; border-radius: 12px; padding: 0 22px; }
QPushButton[size="sm"] { min-height: 30px; padding: 0 12px; border-radius: 8px; font-size: 9pt; }

QFrame#NavItem { background: transparent; border-radius: 8px; }
QFrame#NavItem:hover { background: $sidebar_hover; }
QFrame#NavItem[active="true"] { background: $surface; border: 1px solid $border; }
QFrame#NavItem:focus { border: 2px solid $accent; }
QFrame#NavItem QLabel { color: $muted; font-weight: 500; }
QFrame#NavItem[active="true"] QLabel { color: $fg; }
QFrame#NavItem QLabel#Count, QFrame#NavItem[active="true"] QLabel#Count { background: $accent; color: $accent_fg; border-radius: 9px; padding: 0; font-size: 8pt; font-weight: 600; }

QFrame#Row { background: transparent; border-radius: 8px; }
QFrame#Row:hover { background: $surface2; }
QFrame#Row[active="true"] { background: $accent_soft; }
QFrame#Row[active="true"] QLabel { color: $accent; }
QFrame#Row:focus { border: 2px solid $accent; }

QFrame#Segmented { background: $surface2; border: 1px solid $border; border-radius: 10px; }
QFrame#Segmented QPushButton { background: transparent; color: $muted; min-height: 28px; border-radius: 7px; padding: 0 12px; }
QFrame#Segmented QPushButton:hover { color: $fg; }
QFrame#Segmented QPushButton:checked { background: $surface; color: $fg; }
QFrame#Segmented QPushButton:focus { border: 2px solid $accent; }

QLabel[badge="neutral"] { background: $surface2; color: $muted; border-radius: 9px; padding: 0; font-size: 8.5pt; }
QLabel[badge="accent"] { background: $accent_soft; color: $accent; border-radius: 9px; padding: 0; font-size: 8.5pt; font-weight: 600; }
QLabel[badge="success"] { background: $success_soft; color: $success; border-radius: 9px; padding: 0; font-size: 8.5pt; }
QLabel[badge="warning"] { background: $warning_soft; color: $warning; border-radius: 9px; padding: 0; font-size: 8.5pt; }

QLineEdit, QComboBox {
  background: $surface2; border: 1px solid $border; border-radius: 9px; padding: 0 10px; min-height: 34px;
  selection-background-color: $accent; selection-color: $accent_fg;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid $accent; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView { background: $surface; border: 1px solid $border; selection-background-color: $accent_soft; selection-color: $fg; padding: 4px; }

QProgressBar { background: $surface2; border: none; border-radius: 4px; min-height: 8px; max-height: 8px; }
QProgressBar::chunk { background: $accent; border-radius: 4px; }

QSlider::groove:horizontal { height: 4px; background: $border; border-radius: 2px; }
QSlider::sub-page:horizontal { background: $accent; border-radius: 2px; }
QSlider::add-page:horizontal { background: $border; border-radius: 2px; }
QSlider::handle:horizontal { background: $surface; border: 2px solid $accent; width: 14px; height: 14px; margin: -7px 0; border-radius: 9px; }

QTableWidget { background: transparent; border: none; gridline-color: transparent; selection-background-color: $accent_soft; selection-color: $fg; }
QTableWidget::item { padding: 0 12px; border-bottom: 1px solid $border_soft; }
QHeaderView { background: transparent; }
QHeaderView::section { background: $surface; color: $muted; border: none; border-bottom: 1px solid $border; padding: 8px 12px; font-size: 8.5pt; font-weight: 600; }

QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $border; border-radius: 3px; min-height: 30px; }
QScrollBar:horizontal { height: 0; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

QToolTip { background: $surface; color: $fg; border: 1px solid $border; padding: 6px 8px; border-radius: 6px; }
QDialog { background: $surface; }
"""
)


class Theme(QObject):
    """Holds the active palette and re-applies it when the preference or the OS theme changes."""

    changed = Signal()

    def __init__(self, app: QApplication, pref: str = "system") -> None:
        super().__init__()
        self._app = app
        self._pref = pref
        self.c: dict[str, str] = _derived(LIGHT)
        self.dark = False
        app.styleHints().colorSchemeChanged.connect(self._on_system_change)
        self._apply(emit=False)

    @property
    def pref(self) -> str:
        return self._pref

    def set_pref(self, pref: str) -> None:
        self._pref = pref
        self._apply()

    def _on_system_change(self, *_: object) -> None:
        if self._pref == "system":
            self._apply()

    def _system_dark(self) -> bool:
        return self._app.styleHints().colorScheme() == Qt.ColorScheme.Dark

    def _apply(self, emit: bool = True) -> None:
        dark = self._pref == "dark" or (self._pref == "system" and self._system_dark())
        if emit and dark == self.dark and self._app.styleSheet():
            return
        self.dark = dark
        self.c = _derived(DARK if dark else LIGHT)

        pal = QPalette()
        c = self.c
        for role, key in [
            (QPalette.ColorRole.Window, "bg"),
            (QPalette.ColorRole.WindowText, "fg"),
            (QPalette.ColorRole.Base, "surface"),
            (QPalette.ColorRole.AlternateBase, "surface2"),
            (QPalette.ColorRole.Text, "fg"),
            (QPalette.ColorRole.Button, "surface"),
            (QPalette.ColorRole.ButtonText, "fg"),
            (QPalette.ColorRole.Highlight, "accent"),
            (QPalette.ColorRole.HighlightedText, "accent_fg"),
            (QPalette.ColorRole.ToolTipBase, "surface"),
            (QPalette.ColorRole.ToolTipText, "fg"),
            (QPalette.ColorRole.PlaceholderText, "muted"),
        ]:
            pal.setColor(role, QColor(c[key]))
        self._app.setPalette(pal)
        self._app.setStyleSheet(QSS.substitute(c))
        if emit:
            self.changed.emit()


def pick_font() -> QFont:
    """Inter/Heebo if installed, otherwise the best system UI font with Hebrew support."""
    families = set(QFontDatabase.families())
    for name in ("Inter", "Heebo", "Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Helvetica Neue"):
        if name in families:
            f = QFont(name, 10)
            f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
            return f
    return QFont()
