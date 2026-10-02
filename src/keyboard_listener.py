"""Global keyboard hook that only *triggers a callback* on eligible key presses.

Privacy: this module never stores, logs or transmits which key was pressed. The
only per-key state is a transient set of integer scan codes for keys that are
currently held down (used to ignore auto-repeat); it is never written anywhere.
The hook does not suppress or alter input, so normal typing is unaffected.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

HOTKEY = "ctrl+alt+shift+k"
HOTKEY_LABEL = "Ctrl+Alt+Shift+K"
_MAX_HELD = 256  # safety bound on the held-key set


def _reason(exc: BaseException) -> str:
    return str(exc).strip() or type(exc).__name__


class KeyboardHookError(RuntimeError):
    """The global keyboard hook could not be installed."""


class KeyboardListener:
    def __init__(
        self,
        on_key: Callable[[], object],
        is_armed: Callable[[], bool],
        allow_repeat: Callable[[], bool] = lambda: False,
        on_toggle_hotkey: Callable[[], object] | None = None,
        keyboard_module=None,
    ):
        self._on_key = on_key
        self._is_armed = is_armed
        self._allow_repeat = allow_repeat
        self._on_toggle_hotkey = on_toggle_hotkey
        self._kb = keyboard_module  # injectable for tests
        self._hook_handle = None
        self._hotkey_handle = None
        self._held: set[int] = set()
        self._lock = threading.Lock()
        self.events_seen = 0  # a plain counter, proves the hook receives events

    @property
    def is_active(self) -> bool:
        return self._hook_handle is not None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Install the hook once. Raises KeyboardHookError on failure."""
        with self._lock:
            if self._hook_handle is not None:
                return  # never register a duplicate hook
            kb = self._kb
            if kb is None:
                try:
                    import keyboard as kb  # imported lazily so failures are catchable
                except Exception as exc:
                    raise KeyboardHookError(f"The 'keyboard' library could not load: {_reason(exc)}") from exc
                self._kb = kb
            try:
                self._hook_handle = kb.hook(self._on_event, suppress=False)
                if self._on_toggle_hotkey is not None:
                    self._hotkey_handle = kb.add_hotkey(HOTKEY, self._on_hotkey, suppress=False)
            except Exception as exc:
                self._remove_handles()
                raise KeyboardHookError(f"The global keyboard hook could not be installed: {_reason(exc)}") from exc
            log.info("Global keyboard hook installed")

    def stop(self) -> None:
        """Remove every hook this listener installed. Safe to call repeatedly."""
        with self._lock:
            self._remove_handles()
            self._held.clear()

    def _remove_handles(self) -> None:
        kb = self._kb
        if kb is None:
            return
        if self._hotkey_handle is not None:
            try:
                kb.remove_hotkey(self._hotkey_handle)
            except Exception:
                pass
            self._hotkey_handle = None
        if self._hook_handle is not None:
            try:
                kb.unhook(self._hook_handle)
            except Exception:
                pass
            self._hook_handle = None
            log.info("Global keyboard hook removed")

    # ------------------------------------------------------------------ events
    def _on_hotkey(self) -> None:
        try:
            if self._on_toggle_hotkey:
                self._on_toggle_hotkey()
        except Exception:
            log.exception("Hotkey handler failed")

    def _on_event(self, event) -> None:
        """Runs on the keyboard library's thread - must stay tiny and never raise."""
        try:
            key_id = (event.scan_code << 1) | (1 if getattr(event, "is_keypad", False) else 0)
            if event.event_type == "up":
                self._held.discard(key_id)
                return
            if event.event_type != "down":
                return

            self.events_seen += 1
            is_repeat = key_id in self._held
            if len(self._held) >= _MAX_HELD:
                self._held.clear()
            self._held.add(key_id)

            if is_repeat and not self._allow_repeat():
                return
            if not self._is_armed():
                return
            if self._is_own_hotkey(event):
                return
            self._on_key()
        except Exception:
            log.exception("Keyboard event handler failed")

    def _is_own_hotkey(self, event) -> bool:
        """True for the final key of the app's own arm/disarm shortcut."""
        if self._on_toggle_hotkey is None or getattr(event, "name", None) != "k":
            return False
        kb = self._kb
        return bool(kb.is_pressed("ctrl") and kb.is_pressed("alt") and kb.is_pressed("shift"))
