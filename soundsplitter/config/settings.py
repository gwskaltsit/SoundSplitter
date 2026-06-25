"""Application settings: a typed schema with atomic, corruption-tolerant I/O.

Saves are atomic (write to a temp file, then ``os.replace``) so a crash mid-write
can never leave a half-written settings file. Loads never raise: a missing or
corrupt file falls back to defaults, and unknown keys are ignored so old files
keep working after a schema change.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass
class TargetSettings:
    index: int
    name: str
    delay_ms: float = 0.0
    volume_db: float = 0.0


@dataclass
class Settings:
    source_id: str | None = None
    samplerate: int = 48000
    blocksize: int = 256
    theme: str = "dark"
    language: str = "en"
    autoalign: bool = True
    targets: list[TargetSettings] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known and k != "targets"}
        targets_raw = data.get("targets", [])
        target_keys = {f.name for f in fields(TargetSettings)}
        kwargs["targets"] = [
            TargetSettings(**{k: v for k, v in t.items() if k in target_keys})
            for t in targets_raw
            if isinstance(t, dict) and "index" in t and "name" in t
        ]
        return cls(**kwargs)


def default_settings_path() -> Path:
    """A per-user config path that follows OS conventions, no extra deps."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "soundsplitter" / "settings.json"


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_settings_path()

    def load(self) -> Settings:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
            return Settings()
        if not isinstance(data, dict):
            return Settings()
        return Settings.from_dict(data)

    def save(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(settings), indent=2, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
