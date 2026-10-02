"""JSON-backed settings with validation, safe defaults and atomic saves.

Only the keys in DEFAULTS are ever read or written. Nothing about typed
keys is stored here (or anywhere else in the application).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "armed": False,               # persisted, but applied on launch only if restore_armed
    "volume": 55,                 # 0-100 %
    "intensity": 70,              # 0-100 %
    "pitch": 100,                 # 50-200 % (100 = original)
    "duration_ms": 180,           # 100-300 ms
    "max_voices": 16,             # 1-32 simultaneous voices
    "launch_minimized": False,
    "start_with_windows": False,
    "repeat_on_hold": False,      # retrigger while a key is held down
    "restore_armed": False,       # re-arm automatically on launch (off = safe default)
}

LIMITS: dict[str, tuple[int, int]] = {
    "volume": (0, 100),
    "intensity": (0, 100),
    "pitch": (50, 200),
    "duration_ms": (100, 300),
    "max_voices": (1, 32),
}

BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}


def coerce(key: str, value: Any) -> Any:
    """Validate/clamp one value. Raises ValueError for unusable input."""
    if key not in DEFAULTS:
        raise KeyError(key)
    if key in BOOL_KEYS:
        if isinstance(value, bool):
            return value
        raise ValueError(f"{key} must be true or false")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    try:
        number = round(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{key} is not a finite number") from exc
    lo, hi = LIMITS[key]
    return int(min(hi, max(lo, number)))


class SettingsManager:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._values: dict[str, Any] = dict(DEFAULTS)
        self.last_save_error: str | None = None

    # ------------------------------------------------------------------ load
    def load(self) -> list[str]:
        """Load from disk. Returns human-readable warnings (empty when clean)."""
        warnings: list[str] = []
        with self._lock:
            self._values = dict(DEFAULTS)
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return warnings
            except OSError as exc:
                warnings.append(f"Could not read settings ({exc}). Using defaults.")
                return warnings

            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("top level is not an object")
            except ValueError as exc:  # JSONDecodeError is a ValueError
                warnings.append(f"Settings file was corrupted ({exc}). Defaults restored.")
                try:
                    self.path.replace(self.path.with_suffix(".json.bad"))
                except OSError:
                    pass
                return warnings

            for key in DEFAULTS:
                if key in data:
                    try:
                        self._values[key] = coerce(key, data[key])
                    except ValueError:
                        warnings.append(f"Ignored invalid value for '{key}'; using default.")

            if not self._values["restore_armed"]:
                self._values["armed"] = False  # safety: always start disarmed
        return warnings

    # ------------------------------------------------------------------ access
    def get(self, key: str) -> Any:
        with self._lock:
            return self._values[key]

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._values)

    def update(self, save: bool = True, **changes: Any) -> set[str]:
        """Validate and apply changes. Returns the set of keys whose value changed."""
        changed: set[str] = set()
        with self._lock:
            for key, value in changes.items():
                new = coerce(key, value)
                if self._values[key] != new:
                    self._values[key] = new
                    changed.add(key)
        if changed and save:
            self.save()
        return changed

    def reset_sound(self) -> set[str]:
        return self.update(
            save=False,
            **{k: DEFAULTS[k] for k in ("volume", "intensity", "pitch", "duration_ms", "max_voices")},
        )

    # ------------------------------------------------------------------ save
    def save(self) -> bool:
        with self._lock:
            payload = json.dumps(self._values, indent=2, sort_keys=True)
            tmp = self.path.with_suffix(".json.tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, self.path)
                self.last_save_error = None
                return True
            except OSError as exc:
                self.last_save_error = str(exc)
                log.warning("Could not save settings: %s", exc)
                return False
