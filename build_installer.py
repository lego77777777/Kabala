"""
Builds the distributable installer.

  Windows:  .venv\\Scripts\\python build_installer.py   → dist/Kabala-Setup-<version>.exe  (needs Inno Setup 6)
  macOS:    .venv/bin/python build_installer.py         → dist/Kabala-<version>.dmg        (uses hdiutil)

Both run build_exe.py (PyInstaller) first. Code signing is optional: set SIGN_CMD on Windows
(e.g. a signtool command with {file}) or MAC_SIGN_IDENTITY on macOS.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from kabala import __version__  # noqa: E402


def find_iscc() -> str | None:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ]
    found = shutil.which("ISCC")
    return found or next((str(p) for p in candidates if p.exists()), None)


def sign(path: Path) -> None:
    cmd = os.environ.get("SIGN_CMD")
    if cmd:
        subprocess.run(cmd.format(file=str(path)), shell=True, check=True)
        print(f"Signed {path.name}")


def build_windows() -> Path:
    iscc = find_iscc()
    if not iscc:
        raise SystemExit("Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php (per-user install works).")
    sign(ROOT / "dist" / "Kabala" / "Kabala.exe")
    subprocess.run([iscc, f"/DAppVersion={__version__}", str(ROOT / "installer" / "kabala.iss")], check=True)
    out = ROOT / "dist" / f"Kabala-Setup-{__version__}.exe"
    sign(out)
    return out


def build_macos() -> Path:
    app = ROOT / "dist" / "Kabala.app"
    if identity := os.environ.get("MAC_SIGN_IDENTITY"):
        subprocess.run(["codesign", "--deep", "--force", "--options", "runtime", "--sign", identity, str(app)], check=True)
    staging = ROOT / "build" / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    shutil.copytree(app, staging / "Kabala.app", symlinks=True)
    (staging / "Applications").symlink_to("/Applications")
    out = ROOT / "dist" / f"Kabala-{__version__}.dmg"
    out.unlink(missing_ok=True)
    subprocess.run(["hdiutil", "create", "-volname", "Kabala", "-srcfolder", str(staging), "-ov", "-format", "UDZO", str(out)], check=True)
    return out


def main() -> None:
    subprocess.run([sys.executable, str(ROOT / "build_exe.py")], check=True)
    out = build_windows() if sys.platform == "win32" else build_macos() if sys.platform == "darwin" else None
    if out is None:
        raise SystemExit("Installers are built on Windows (.exe) or macOS (.dmg).")
    print(f"\nInstaller ready: {out}  ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
