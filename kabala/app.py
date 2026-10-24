from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from kabala import i18n
from kabala.backend import Backend
from kabala.settings import Settings
from kabala.theme import Theme, pick_font
from kabala.ui import icons, widgets


def setup_logging() -> None:
    """Log to %APPDATA%\\Kabala\\Kabala\\kabala.log (rotating, 3 × 1 MB). Never logs email contents or tokens."""
    import logging
    from logging.handlers import RotatingFileHandler

    from kabala.settings import config_dir

    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    handler = RotatingFileHandler(config_dir() / "kabala.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("urllib3", "google", "httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("kabala").info("Kabala starting")


def create_app(argv: list[str] | None = None) -> tuple[QApplication, "MainWindow"]:  # noqa: F821
    from kabala.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Kabala")
    app.setOrganizationName("Kabala")
    setup_logging()  # after the names are set: they decide the app data folder
    app.setStyle("Fusion")  # consistent rendering of the stylesheet on every OS
    app.setFont(pick_font())

    settings = Settings.load()
    i18n.set_language(settings.lang)
    theme = Theme(app, settings.theme)
    widgets.theme = theme
    app.setWindowIcon(QIcon(icons.logo(64, theme.c["accent"])))
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft if i18n.is_rtl() else Qt.LayoutDirection.LeftToRight)

    window = MainWindow(app, settings, theme, Backend(settings))
    return app, window


def main() -> None:
    from kabala.autoscan import BACKGROUND_FLAG
    from kabala.deeplink import SingleInstance, link_from_argv, register_protocol

    QApplication.instance() or QApplication(sys.argv)
    link = link_from_argv(sys.argv)
    instance = SingleInstance()
    if instance.secondary:
        # Kabala is already open (e.g. the browser returned kabala://payment-success): hand over and exit.
        instance.send(link or "activate")
        return

    app, window = create_app()
    app.setQuitOnLastWindowClosed(False)  # the window decides: quit, or keep running in the tray
    register_protocol()
    instance.message.connect(window.on_external_message)
    # Started by Windows at login: stay in the tray if background scanning is on.
    start_hidden = BACKGROUND_FLAG in sys.argv and window.tray and window.ctx.settings.auto_scan and not window._in_welcome()
    if not start_hidden:
        window.show()
    if link:
        window.on_external_message(link)
    sys.exit(app.exec())
