"""Persisting settings. The mouse cannot be read back, so this is the only record."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .protocol import Settings

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "x3ctl"
LAST_APPLIED = CONFIG_DIR / "last-applied.json"
PROFILES_DIR = CONFIG_DIR / "profiles"
MACROS_FILE = CONFIG_DIR / "macros.json"


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def load_last_applied() -> Settings | None:
    data = _read_json(LAST_APPLIED)
    return Settings.from_dict(data) if isinstance(data, dict) else None


def save_last_applied(settings: Settings) -> None:
    _write_json(LAST_APPLIED, settings.to_dict())


def profile_names() -> list[str]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def _profile_path(name: str) -> Path:
    safe = re.sub(r"[^\w\- ]", "_", name).strip() or "profile"
    return PROFILES_DIR / f"{safe}.json"


def save_profile(name: str, settings: Settings) -> None:
    _write_json(_profile_path(name), settings.to_dict())


def load_profile(name: str) -> Settings | None:
    data = _read_json(_profile_path(name))
    return Settings.from_dict(data) if isinstance(data, dict) else None


def delete_profile(name: str) -> None:
    try:
        _profile_path(name).unlink()
    except FileNotFoundError:
        pass


def load_macros() -> dict:
    data = _read_json(MACROS_FILE)
    return data if isinstance(data, dict) else {}


def save_macros(macros: dict) -> None:
    _write_json(MACROS_FILE, macros)
