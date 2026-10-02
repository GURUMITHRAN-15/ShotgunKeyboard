"""Windows integration helpers. Every function degrades gracefully off-Windows."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ShotgunKeyboard"
_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5


def is_windows() -> bool:
    return sys.platform == "win32"


def is_admin() -> bool:
    if not is_windows():
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _launch_command(extra_args: list[str]) -> tuple[str, str]:
    """(executable, parameter string) that relaunches this app."""
    if paths.is_frozen():
        return sys.executable, subprocess.list2cmdline(extra_args)
    script = str(paths.bundle_dir() / "shotgunkeyboard.py")
    return sys.executable, subprocess.list2cmdline([script, *extra_args])


def restart_as_admin() -> tuple[bool, str]:
    """Ask Windows (UAC) to relaunch elevated. Called only from an explicit button
    press, once - the app never auto-elevates or loops."""
    if not is_windows():
        return False, "Administrator restart is only available on Windows."
    exe, params = _launch_command(["--elevated"])
    try:
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    except Exception as exc:
        return False, f"Could not request elevation: {exc}"
    if result <= 32:
        return False, "Administrator restart was cancelled or denied."
    return True, "Restarting as Administrator..."


# ---------------------------------------------------------------------- startup
def _startup_command() -> str:
    if paths.is_frozen():
        return subprocess.list2cmdline([sys.executable, "--minimized"])
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")  # no console window at login
    exe = pyw if pyw.exists() else py
    return subprocess.list2cmdline([str(exe), str(paths.bundle_dir() / "shotgunkeyboard.py"), "--minimized"])


def is_startup_enabled() -> bool:
    if not is_windows():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def set_startup(enabled: bool) -> tuple[bool, str]:
    """Add/remove the per-user Run entry (HKCU - no admin rights needed)."""
    if not is_windows():
        return False, "Start with Windows is only available on Windows."
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _startup_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True, ""
    except OSError as exc:
        return False, f"Could not update the Windows startup entry: {exc}"


# ---------------------------------------------------------------------- single instance
class SingleInstance:
    """Named-mutex guard so only one tray icon / keyboard hook exists."""

    NAME = "Local\\ShotgunKeyboard.SingleInstance"

    def __init__(self) -> None:
        self._handle = None

    def acquire(self, wait_seconds: float = 0.0) -> bool:
        if not is_windows():
            return True
        deadline = time.monotonic() + wait_seconds
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        while True:
            handle = kernel32.CreateMutexW(None, False, self.NAME)
            err = ctypes.get_last_error()
            if handle and err != _ERROR_ALREADY_EXISTS:
                self._handle = handle
                return True
            if handle:
                kernel32.CloseHandle(handle)
            # NULL handle + access denied means an elevated instance owns it: still "running"
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def release(self) -> None:
        if self._handle and is_windows():
            try:
                ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(self._handle))
            except Exception:
                pass
        self._handle = None
