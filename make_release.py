"""
Builds a release: the installer plus a zip of the source code, with secrets left out.

  .venv\\Scripts\\python make_release.py            → dist/release/Kabala-Setup-<v>.exe + Kabala-source-<v>.zip
  .venv\\Scripts\\python make_release.py --no-build → only re-zips the source (reuses the existing installer)
  add --with-secrets → also Kabala-source-<v>-PRIVATE.zip, which includes the secret files (local use only!)

The regular zip never contains google_client.json, .env files, private keys, the venv, node_modules or build output,
and the script refuses to finish if anything that looks like a secret slipped into it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from kabala import __version__  # noqa: E402

SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache", "build", "dist", "node_modules", ".data", "pgdata", ".git"}
SKIP_FILES = ["google_client.json", ".env", ".env.*", "*.pyc", "*private*.pem", "*.key", "token*.json"]
KEEP_FILES = {".env.example"}
SECRET_PATTERNS = [re.compile(p) for p in (
    rb"GOCSPX-[A-Za-z0-9_-]{10,}",          # Google OAuth client secret
    rb"-----BEGIN (?:EC |RSA )?PRIVATE KEY-----",
    rb"sk-ant-[A-Za-z0-9_-]{20,}",           # Anthropic API key
    rb"sk_live_[A-Za-z0-9]{10,}",            # Stripe live key
    rb"whsec_[A-Za-z0-9]{10,}",              # Stripe webhook secret
)]


def skipped(rel: Path, with_secrets: bool = False) -> bool:
    if any(part in SKIP_DIRS for part in rel.parts[:-1]):
        return True
    name = rel.name
    if with_secrets and not fnmatch.fnmatch(name, "*.pyc"):
        return False
    return name not in KEEP_FILES and any(fnmatch.fnmatch(name, pat) for pat in SKIP_FILES)


def zip_source(out: Path, with_secrets: bool = False) -> int:
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ROOT.rglob("*")):
            rel = path.relative_to(ROOT)
            if path.is_dir() or skipped(rel, with_secrets):
                continue
            data = path.read_bytes()
            for pattern in [] if with_secrets else SECRET_PATTERNS:
                if pattern.search(data):
                    out.unlink(missing_ok=True)
                    raise SystemExit(f"Refusing to release: {rel} looks like it contains a secret ({pattern.pattern[:20]!r}…)")
            zf.writestr(f"kabala-{__version__}/{rel.as_posix()}", data)
            count += 1
    return count


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    if "--no-build" not in sys.argv:
        subprocess.run([sys.executable, str(ROOT / "build_installer.py")], check=True)
    installer = ROOT / "dist" / f"Kabala-Setup-{__version__}.exe"
    if not installer.exists():
        raise SystemExit(f"{installer.name} not found; run without --no-build first.")

    release = ROOT / "dist" / "release"
    shutil.rmtree(release, ignore_errors=True)
    release.mkdir(parents=True)
    shutil.copy2(installer, release / installer.name)
    source = release / f"Kabala-source-{__version__}.zip"
    files = zip_source(source)

    outputs = [release / installer.name, source]
    if "--with-secrets" in sys.argv:
        private = release / f"Kabala-source-{__version__}-PRIVATE.zip"
        private_files = zip_source(private, with_secrets=True)
        outputs.append(private)

    sums = "\n".join(f"{sha256(p)}  {p.name}" for p in outputs)
    (release / "SHA256SUMS.txt").write_text(sums + "\n", encoding="utf-8")
    print(f"\nRelease {__version__} ready in {release}")
    print(f"  {installer.name}  ({installer.stat().st_size / 1e6:.1f} MB)")
    print(f"  {source.name}  ({files} files, {source.stat().st_size / 1e6:.1f} MB, no secrets)")
    if "--with-secrets" in sys.argv:
        print(f"  {private.name}  ({private_files} files, WITH secrets: keep it private, never upload or share it)")


if __name__ == "__main__":
    main()
