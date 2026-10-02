#!/usr/bin/env python3
"""ShotgunKeyboard - plays a synthesized shotgun sound on every key press (Windows).

Entry point + controller. Component wiring:

    keyboard thread --(on_key)--> AudioEngine.trigger()          (no Qt, no locks held long)
    tray thread / UI thread --> AppController methods --> state_changed signal --> UI refresh
    AppController owns: SettingsManager, AudioEngine, KeyboardListener, TrayManager

Qt objects are only touched on the main thread: calls arriving from the tray or
keyboard threads are marshalled with queued signals.

Command line:  --minimized (start hidden in tray)   --elevated (internal, after UAC restart)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication, QMessageBox

from generate_sound import SoundFileError, ensure_sound_file
from src import __version__, paths, system_utils
from src.audio_engine import AudioEngine, AudioError, build_sound_buffer
from src.keyboard_listener import HOTKEY_LABEL, KeyboardHookError, KeyboardListener
from src.settings_manager import SettingsManager
from src.tray_manager import TrayError, TrayManager
from src.ui import MainWindow

log = logging.getLogger("shotgunkeyboard")

HOOK_HELP = (
    "Global keyboard detection is unavailable, so ShotgunKeyboard is in limited mode "
    "(Test Sound still works). Try: close other keyboard-hook tools, then click "
    "'Retry keyboard hook'. If it keeps failing, click 'Restart as Administrator'."
)
AUDIO_HELP = (
    "Connect or enable an audio output device (check Windows Sound settings), "
    "then click 'Retry audio'."
)


def setup_logging() -> None:
    """Errors/lifecycle only. Keystrokes are never logged."""
    handlers: list[logging.Handler] = []
    try:
        handlers.append(RotatingFileHandler(paths.log_path(), maxBytes=256_000, backupCount=1, encoding="utf-8"))
    except OSError:
        pass
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class AppController(QObject):
    state_changed = Signal()
    show_window_requested = Signal()
    quit_requested = Signal()
    _setting_requested = Signal(str, object)

    def __init__(self, instance: system_utils.SingleInstance):
        super().__init__()
        self.version = __version__
        self.instance = instance
        self.settings = SettingsManager(paths.config_path())
        self.engine: AudioEngine | None = None
        self.listener: KeyboardListener | None = None
        self.tray: TrayManager | None = None
        self.window: MainWindow | None = None
        self.wav_path = None
        self.hook_error: str | None = None
        self.audio_error: str | None = None
        self.tray_error: str | None = None
        self.notices: list[str] = []
        self._armed = False
        self._shutdown_done = False

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_settings)
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(120)
        self._rebuild_timer.timeout.connect(self._rebuild_sound)
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(1000)
        self._health_timer.timeout.connect(self._check_health)

        # Queued connections: emitted from tray/keyboard threads, run on the main thread.
        self.show_window_requested.connect(self._on_show_window)
        self.quit_requested.connect(self.quit_app)
        self._setting_requested.connect(self._apply_setting)

    def _save_settings(self) -> None:
        self.settings.save()

    # ------------------------------------------------------------------ startup
    def initialize(self) -> None:
        s = self.settings
        self.notices.extend(s.load())
        if system_utils.is_windows():  # reflect the real registry state
            s.update(save=False, start_with_windows=system_utils.is_startup_enabled())

        self.wav_path = self._prepare_sound_file()
        self.engine = AudioEngine(max_voices=s.get("max_voices"), volume=s.get("volume"))
        self._rebuild_sound(initial=True)
        self._start_audio()

        self.listener = KeyboardListener(
            on_key=self.engine.trigger,
            is_armed=lambda: self._armed,
            allow_repeat=lambda: bool(self.settings.get("repeat_on_hold")),
            on_toggle_hotkey=self.toggle_armed,
        )
        self._start_hook()

        self._armed = bool(s.get("armed")) and self.hook_error is None and self.audio_error is None
        s.update(save=False, armed=self._armed)
        self._health_timer.start()

    def start_tray(self) -> None:
        s = self.settings
        self.tray = TrayManager(
            is_armed=lambda: self._armed,
            get_volume=lambda: s.get("volume"),
            startup_enabled=lambda: bool(s.get("start_with_windows")),
            on_toggle_armed=self.toggle_armed,
            on_set_volume=lambda v: self.set_setting("volume", v),
            on_open_settings=self.show_window_requested.emit,
            on_test_sound=self.test_sound,
            on_toggle_startup=lambda: self.set_startup(not bool(s.get("start_with_windows"))),
            on_quit=self.quit_requested.emit,
        )
        try:
            self.tray.start()
            self.tray_error = None
        except TrayError as exc:
            self.tray = None
            self.tray_error = f"{exc} The window will stay open; closing it will quit the app."
            log.error("%s", exc)

    @property
    def tray_ok(self) -> bool:
        return self.tray is not None and self.tray_error is None

    def _prepare_sound_file(self):
        """Make sure sounds/shotgun.wav exists (generate it on first launch if needed)."""
        for candidate in (paths.resource_path("sounds", "shotgun.wav"),
                          paths.data_dir() / "sounds" / "shotgun.wav"):
            try:
                return ensure_sound_file(candidate)
            except (OSError, SoundFileError, ValueError) as exc:
                log.warning("Sound file unavailable at %s: %s", candidate, exc)
        self.notices.append("Could not create sounds/shotgun.wav; the sound is generated live instead.")
        return None

    def _start_audio(self) -> None:
        try:
            self.engine.start()
            self.audio_error = None
        except AudioError as exc:
            self.audio_error = f"{exc}\n{AUDIO_HELP}"
            log.error("%s", exc)

    def _start_hook(self) -> None:
        try:
            self.listener.start()
            self.hook_error = None
        except KeyboardHookError as exc:
            self.hook_error = f"{exc}\n{HOOK_HELP}"
            log.error("%s", exc)

    # ------------------------------------------------------------------ state for the UI
    def snapshot(self) -> dict:
        s = self.settings.as_dict()
        s.update(
            armed=self._armed,
            hook_active=bool(self.listener and self.listener.is_active),
            hook_error=self.hook_error,
            events_seen=self.listener.events_seen if self.listener else 0,
            audio_ok=bool(self.engine and self.engine.is_running),
            audio_error=self.audio_error,
            active_voices=self.engine.active_voices if self.engine else 0,
            device=self.engine.device_name if self.engine else "",
            tray_ok=self.tray_ok,
            tray_error=self.tray_error,
            notices=list(self.notices),
            admin=system_utils.is_admin(),
            version=self.version,
            hotkey=HOTKEY_LABEL,
        )
        return s

    def _changed(self) -> None:
        if self.tray:
            self.tray.update()
        self.state_changed.emit()

    # ------------------------------------------------------------------ actions (thread-safe)
    def set_armed(self, value: bool) -> bool:
        value = bool(value)
        if value:
            if self.listener and not self.listener.is_active:
                self._start_hook()
            if self.engine and not self.engine.is_running:
                self._start_audio()
            if self.hook_error or self.audio_error:
                self._armed = False  # never claim to be armed while detection or audio is broken
                self.settings.update(armed=False)
                self._changed()
                return False
        self._armed = value
        self.settings.update(armed=value)
        self._changed()
        return True

    def toggle_armed(self) -> None:
        self.set_armed(not self._armed)

    def test_sound(self) -> bool:
        if self.engine is None:
            return False
        if not self.engine.is_running:
            self._start_audio()
        if self.audio_error or not self.engine.trigger():
            self._changed()
            return False
        return True

    def set_setting(self, key: str, value) -> None:
        self._setting_requested.emit(key, value)  # marshalled to the main thread

    def retry_hook(self) -> None:
        self._start_hook()
        self._changed()

    def retry_audio(self) -> None:
        try:
            self.engine.recover()
            self.audio_error = None
        except AudioError as exc:
            self.audio_error = f"{exc}\n{AUDIO_HELP}"
        self._changed()

    def dismiss_notices(self) -> None:
        self.notices.clear()
        self.state_changed.emit()

    def reset_sound_defaults(self) -> None:
        if self.settings.reset_sound():
            self._apply_all_sound_settings()
        self._save_timer.start()
        self.state_changed.emit()

    def set_startup(self, enabled: bool) -> None:
        ok, message = system_utils.set_startup(bool(enabled))
        if ok:
            self.settings.update(start_with_windows=bool(enabled))
        elif message:
            self.notices.append(message)
        self._changed()

    def restart_as_admin(self) -> None:
        """User-initiated, once. Releases the single-instance lock so the elevated copy can start."""
        self.instance.release()
        ok, message = system_utils.restart_as_admin()
        if ok:
            self.quit_requested.emit()
            return
        self.instance.acquire()
        self.notices.append(message)
        self._changed()

    # ------------------------------------------------------------------ main-thread slots
    @Slot(str, object)
    def _apply_setting(self, key: str, value) -> None:
        try:
            changed = self.settings.update(save=False, **{key: value})
        except (KeyError, ValueError) as exc:
            log.warning("Rejected setting %s: %s", key, exc)
            return
        if not changed:
            return
        if "volume" in changed:
            self.engine.set_volume(self.settings.get("volume"))
        if "max_voices" in changed:
            self.engine.set_max_voices(self.settings.get("max_voices"))
        if changed & {"pitch", "intensity", "duration_ms"}:
            self._rebuild_timer.start()
        self._save_timer.start()
        self._changed()

    def _apply_all_sound_settings(self) -> None:
        self.engine.set_volume(self.settings.get("volume"))
        self.engine.set_max_voices(self.settings.get("max_voices"))
        self._rebuild_sound()
        self._changed()

    @Slot()
    def _rebuild_sound(self, initial: bool = False) -> None:
        s = self.settings
        try:
            buf, warning = build_sound_buffer(
                s.get("duration_ms"), s.get("pitch") / 100.0, s.get("intensity") / 100.0, self.wav_path)
            self.engine.set_buffer(buf)
        except Exception as exc:
            log.exception("Sound generation failed")
            self.notices.append(f"Sound generation failed: {exc}")
            warning = None
        if warning and warning not in self.notices:
            self.notices.append(warning)
        if not initial:
            self.state_changed.emit()

    @Slot()
    def _on_show_window(self) -> None:
        if self.window is not None:
            self.window.show_window()

    @Slot()
    def _check_health(self) -> None:
        """Runs every second on the main thread; keeps the armed state honest."""
        if self._armed and self.engine and not self.engine.is_running:
            self.audio_error = f"Audio output stopped (device removed or changed?). Disarmed for safety.\n{AUDIO_HELP}"
            self._armed = False
            self.settings.update(armed=False)
            self._changed()
        elif self.engine and self.engine.last_error and not self.audio_error:
            self.audio_error = f"{self.engine.last_error}\n{AUDIO_HELP}"
            self._changed()

    # ------------------------------------------------------------------ shutdown
    @Slot()
    def quit_app(self) -> None:
        self.shutdown()
        QApplication.quit()

    def shutdown(self) -> None:
        """Idempotent: unhook keyboard, stop audio, remove tray, save settings."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        watchdog = threading.Timer(4.0, lambda: os._exit(0))  # never leave an orphaned process
        watchdog.daemon = True
        watchdog.start()
        self._armed = False
        for step in (
            lambda: self.listener and self.listener.stop(),
            lambda: self.engine and self.engine.close(),
            lambda: self.tray and self.tray.stop(),
            lambda: self.settings.save(),
            lambda: self.instance.release(),
        ):
            try:
                step()
            except Exception:
                log.exception("Shutdown step failed")
        log.info("ShotgunKeyboard stopped")


def main() -> int:
    setup_logging()
    args = set(sys.argv[1:])
    if system_utils.is_windows():
        try:  # proper taskbar identity / icon grouping
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ShotgunKeyboard.App")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("ShotgunKeyboard")
    app.setQuitOnLastWindowClosed(False)  # keep running in the tray

    instance = system_utils.SingleInstance()
    if not instance.acquire(wait_seconds=15.0 if "--elevated" in args else 0.0):
        QMessageBox.information(
            None, "ShotgunKeyboard",
            "ShotgunKeyboard is already running.\nLook for its icon in the system tray "
            "(click the ^ arrow next to the clock) and choose Open Settings.")
        return 0

    controller = AppController(instance)
    controller.initialize()
    window = MainWindow(controller)
    controller.window = window
    controller.state_changed.connect(window.refresh)
    controller.start_tray()
    controller._changed()

    start_hidden = ("--minimized" in args or controller.settings.get("launch_minimized")) and controller.tray_ok
    if not start_hidden:
        window.show_window()

    app.aboutToQuit.connect(controller.shutdown)
    signal.signal(signal.SIGINT, lambda *_: controller.quit_requested.emit())  # Ctrl+C when run from a console
    keepalive = QTimer()  # lets Python deliver signals while Qt runs
    keepalive.start(500)
    keepalive.timeout.connect(lambda: None)

    code = app.exec()
    controller.shutdown()
    return code


if __name__ == "__main__":
    sys.exit(main())
