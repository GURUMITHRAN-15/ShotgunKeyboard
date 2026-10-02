"""Resource and data-directory helpers (work both from source and PyInstaller)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ShotgunKeyboard"


def is_frozen() -> bool:
    """True when running from a PyInstaller executable."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Folder holding bundled read-only resources (sounds/, assets/)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    return bundle_dir().joinpath(*parts)


def data_dir() -> Path:
    """Writable per-user folder for config and logs.

    From source: the project folder (config.json lives next to the code).
    From the EXE: %APPDATA%\\ShotgunKeyboard (the EXE's own folder is a temp dir).
    """
    if is_frozen():
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    else:
        base = bundle_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


def config_path() -> Path:
    return data_dir() / "config.json"


def log_path() -> Path:
    return data_dir() / "shotgunkeyboard.log"
