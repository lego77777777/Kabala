"""
Builds dist/Kabala/Kabala.exe (Windows) or dist/Kabala.app (macOS) with PyInstaller.

    .venv\\Scripts\\python build_exe.py

If google_client.json exists next to this file it is bundled, so the built app can connect to Gmail.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"


def make_icon() -> Path:
    """Renders the app logo into a multi-size .ico (Windows) / .png (macOS) using Qt."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 - needed for rendering
    from kabala.theme import LIGHT
    from kabala.ui.icons import logo

    BUILD.mkdir(exist_ok=True)
    path = BUILD / ("kabala.ico" if sys.platform == "win32" else "kabala.png")
    pixmap = logo(256, LIGHT["accent"]).scaled(256, 256)
    if not pixmap.toImage().save(str(path)):
        raise SystemExit(f"Could not write icon {path}")
    return path


def main() -> None:
    import PyInstaller.__main__

    icon = make_icon()
    sep = ";" if sys.platform == "win32" else ":"
    args = [
        str(ROOT / "kabala" / "__main__.py"),
        "--name=Kabala",
        "--windowed",  # no console window
        "--noconfirm",
        "--clean",
        f"--icon={icon}",
        f"--distpath={ROOT / 'dist'}",
        f"--workpath={BUILD / 'pyinstaller'}",
        f"--specpath={BUILD}",
        # keyring finds its OS backend through entry points; make sure they ship.
        "--collect-submodules=keyring.backends",
        "--copy-metadata=keyring",
        # The in-app PayPal window (kabala/ui/checkout_window.py) uses QtWebEngine, which needs QtQml and
        # QtQuick at runtime, so those are no longer excluded.
        "--hidden-import=kabala.ui.checkout_window",
        "--exclude-module=tkinter",
        "--exclude-module=pytest",
    ]
    client = ROOT / "google_client.json"
    if client.exists():
        args.append(f"--add-data={client}{sep}.")
        print("Bundling google_client.json")
    else:
        print("google_client.json not found: the built app will ask for Google setup (or run in demo mode).")
    pub = ROOT / "license_public_key.pem"
    if pub.exists():
        args.append(f"--add-data={pub}{sep}.")
        print("Bundling license_public_key.pem")
    server = ROOT / "kabala_server.json"
    if server.exists():
        args.append(f"--add-data={server}{sep}.")
        print("Bundling kabala_server.json (plans enforced by the license server)")
    else:
        print("kabala_server.json not found: the built app runs unmanaged (all features open).")
    PyInstaller.__main__.run(args)
    out = ROOT / "dist" / "Kabala"
    print(f"\nDone: {out / 'Kabala.exe' if sys.platform == 'win32' else ROOT / 'dist' / 'Kabala.app'}")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
