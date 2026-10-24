"""Background scanning schedule and "start with Windows"."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

INTERVALS_HOURS = [6, 12, 24, 72]
RUN_KEY = r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "Kabala"
BACKGROUND_FLAG = "--background"


def is_due(last_scan: datetime | None, now: datetime, interval_hours: int) -> bool:
    return last_scan is None or now - last_scan >= timedelta(hours=interval_hours)


def launch_command() -> str:
    """The command Windows runs at login: the installed exe, or python -m kabala in development."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {BACKGROUND_FLAG}'
    exe = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{exe}" -m kabala {BACKGROUND_FLAG}'


def set_run_at_login(enabled: bool, settings=None) -> bool:
    """
    Adds/removes Kabala from the current user's startup programs (HKCU Run key; no admin rights).
    `settings` lets tests pass their own QSettings. Returns False where this isn't supported yet.
    """
    if settings is None:
        if sys.platform != "win32":
            return False  # macOS (LaunchAgent) comes with the installer stage
        from PySide6.QtCore import QSettings

        settings = QSettings(RUN_KEY, QSettings.Format.NativeFormat)
    if enabled:
        settings.setValue(APP_NAME, launch_command())
    else:
        settings.remove(APP_NAME)
    settings.sync()
    return True


def runs_at_login(settings=None) -> bool:
    if settings is None:
        if sys.platform != "win32":
            return False
        from PySide6.QtCore import QSettings

        settings = QSettings(RUN_KEY, QSettings.Format.NativeFormat)
    return settings.contains(APP_NAME)
