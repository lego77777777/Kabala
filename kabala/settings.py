"""User preferences, stored as JSON in the per-user app data folder. Nothing sensitive goes here."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from PySide6.QtCore import QStandardPaths


def config_dir() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    path = Path(base or Path.home() / ".kabala")
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_folder() -> str:
    docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation) or str(Path.home())
    return str(Path(docs) / "Receipts")


@dataclass
class Settings:
    lang: str = "he"
    theme: str = "system"  # system | light | dark
    layout: str = "company-first"  # company-first | date-first
    threshold: float = 0.75
    provider: str = "anthropic"  # anthropic | openai | ollama
    folder: str = ""
    auto_scan: bool = False  # scan in the background and keep running in the tray
    auto_interval: int = 24  # hours between background scans
    tray_hint_shown: bool = False

    @property
    def destination(self) -> str:
        return self.folder or default_folder()

    @classmethod
    def load(cls) -> "Settings":
        try:
            raw = json.loads((config_dir() / "settings.json").read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in raw.items() if k in known})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        try:
            (config_dir() / "settings.json").write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # preferences just won't persist
