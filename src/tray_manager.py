"""System-tray integration (pystray) with a programmatically drawn Pillow icon."""

from __future__ import annotations

import logging
from typing import Callable

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

VOLUME_PRESETS = (("Loud", 85), ("Medium", 55), ("Quiet", 30))

_NAVY = (14, 19, 32, 255)
_ORANGE = (255, 122, 24, 255)
_BRASS = (232, 178, 76, 255)
_GREY = (112, 120, 140, 255)
_GREY_DARK = (70, 76, 92, 255)
_GREEN = (46, 204, 113, 255)
_RED = (231, 76, 60, 255)


class TrayError(RuntimeError):
    """The tray icon could not be created."""


def make_icon_image(armed: bool, size: int = 64) -> Image.Image:
    """Draw a shotgun-shell icon. Armed = orange shell + green dot; disarmed = grey + red dot."""
    s = size / 64.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def box(x0, y0, x1, y1):
        return [x0 * s, y0 * s, x1 * s, y1 * s]

    d.rounded_rectangle(box(2, 2, 62, 62), radius=14 * s, fill=_NAVY,
                        outline=_ORANGE if armed else _GREY_DARK, width=max(1, round(3 * s)))
    body = _ORANGE if armed else _GREY
    base = _BRASS if armed else _GREY_DARK
    d.rounded_rectangle(box(21, 11, 43, 40), radius=5 * s, fill=body)       # hull
    d.rounded_rectangle(box(21, 38, 43, 53), radius=3 * s, fill=base)       # brass head
    d.rectangle(box(21, 38, 43, 41), fill=(*base[:3], 255))
    d.line(box(21, 41, 43, 41), fill=_NAVY, width=max(1, round(1.5 * s)))   # rim line
    d.ellipse(box(27, 45, 37, 50), fill=_NAVY)                              # primer
    d.ellipse(box(44, 44, 58, 58), fill=_NAVY)
    d.ellipse(box(46, 46, 56, 56), fill=_GREEN if armed else _RED)          # status dot
    return img


class TrayManager:
    def __init__(
        self,
        is_armed: Callable[[], bool],
        get_volume: Callable[[], int],
        startup_enabled: Callable[[], bool],
        on_toggle_armed: Callable[[], object],
        on_set_volume: Callable[[int], object],
        on_open_settings: Callable[[], object],
        on_test_sound: Callable[[], object],
        on_toggle_startup: Callable[[], object],
        on_quit: Callable[[], object],
    ):
        self._is_armed = is_armed
        self._get_volume = get_volume
        self._startup_enabled = startup_enabled
        self._cb = dict(toggle=on_toggle_armed, volume=on_set_volume, open=on_open_settings,
                        test=on_test_sound, startup=on_toggle_startup, quit=on_quit)
        self._icon = None
        self._images = {True: make_icon_image(True), False: make_icon_image(False)}

    # Menu callbacks run on pystray's thread; they only forward to thread-safe handlers.
    def _act(self, name: str, *args):
        def run(icon=None, item=None):
            try:
                self._cb[name](*args)
            except Exception:
                log.exception("Tray action '%s' failed", name)
        return run

    def _volume_item(self, pystray, label: str, value: int):
        return pystray.MenuItem(
            label, self._act("volume", value), radio=True,
            checked=lambda item: self._get_volume() == value,
        )

    def start(self) -> None:
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem(lambda item: f"Armed: {'ON' if self._is_armed() else 'OFF'}",
                                 self._act("toggle")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Volume", pystray.Menu(
                    *[self._volume_item(pystray, label, value) for label, value in VOLUME_PRESETS])),
                pystray.MenuItem("Open Settings", self._act("open"), default=True),
                pystray.MenuItem("Test Sound", self._act("test")),
                pystray.MenuItem("Start with Windows", self._act("startup"),
                                 checked=lambda item: self._startup_enabled()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._act("quit")),
            )
            self._icon = pystray.Icon("ShotgunKeyboard", self._images[self._is_armed()],
                                      self._title(), menu)
            self._icon.run_detached()
        except Exception as exc:
            self._icon = None
            raise TrayError(f"System tray icon could not be created: {str(exc).strip() or type(exc).__name__}.") from exc

    def _title(self) -> str:
        return f"ShotgunKeyboard - {'ARMED' if self._is_armed() else 'disarmed'}"

    def update(self) -> None:
        """Refresh icon, tooltip and menu state immediately."""
        icon = self._icon
        if icon is None:
            return
        try:
            icon.icon = self._images[bool(self._is_armed())]
            icon.title = self._title()
            icon.update_menu()
        except Exception:
            log.exception("Tray update failed")

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
