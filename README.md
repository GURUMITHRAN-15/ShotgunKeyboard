# ShotgunKeyboard

A Windows system-tray utility that plays a short, punchy, **synthesized** shotgun-style impact sound whenever you press a key, in any application. Fast typing produces overlapping sounds; nothing is cut off.

It is strictly an audio-feedback tool. It **never records, stores, logs or transmits** what you type, and it does not suppress or alter keyboard input.

Targets Windows 10/11 (64-bit), Python 3.11-3.13.

---

## 1. Architecture

```
 keyboard thread ──on_key──▶ AudioEngine.trigger() ──▶ voice list ──▶ audio callback (sounddevice stream)
                                                          ▲
 tray thread (pystray) ─┐                                 │ set_buffer / set_volume / set_max_voices
 UI thread (PySide6) ───┴─▶ AppController ────────────────┘
                              │  owns SettingsManager, AudioEngine, KeyboardListener, TrayManager
                              └─ state_changed (Qt signal) ─▶ MainWindow.refresh()
```

| File | Role |
|---|---|
| `shotgunkeyboard.py` | Entry point + `AppController` (wires everything, owns lifecycle and shutdown) |
| `generate_sound.py` | NumPy synthesis, WAV read/write, CLI (`sounds/shotgun.wav`) |
| `src/audio_engine.py` | One long-lived output stream + software mixer (overlap, voice cap, soft limiter) |
| `src/keyboard_listener.py` | Global hook (`keyboard`), auto-repeat filtering, own-hotkey handling |
| `src/tray_manager.py` | Pillow-drawn icon (armed/disarmed) + right-click menu (`pystray`) |
| `src/settings_manager.py` | Validated JSON settings, atomic saves, safe defaults |
| `src/ui.py` | Dark navy/orange PySide6 window: Dashboard, Sound, System |
| `src/paths.py`, `src/system_utils.py` | Helpers (PyInstaller paths; admin check, startup entry, single instance) |
| `tests/test_core.py` | Hardware-free unit tests |

Thread safety: the keyboard and tray threads never touch Qt objects directly. They call controller methods, which update state and emit Qt signals that run on the main thread.

### Deliberate deviations from the PRD's stack

* **`sounddevice` instead of `simpleaudio`.** `simpleaudio` is unmaintained and has no wheels for current Python versions. `sounddevice` ships PortAudio inside its Windows wheel, keeps one stream open (low latency) and the app mixes voices itself, so there is no thread or audio object per key press.
* **SciPy omitted** (it was optional). The filters and reverb use NumPy only.
* **Extra helper modules** `src/paths.py` and `src/system_utils.py` keep the listed modules focused.

### Library limitations on Windows

* The `keyboard` hook cannot see keys typed into **elevated** (Administrator) windows unless ShotgunKeyboard is also elevated. Also unavailable: UAC prompts and the secure desktop (lock screen, Ctrl+Alt+Del).
* Some games with anti-cheat block or flag global hooks. Security software may flag any program that installs a keyboard hook (see Troubleshooting).
* `keyboard` cannot tell left and right Ctrl/Alt/Shift apart for auto-repeat tracking (harmless).
* Audio device changes (unplugging headphones) require **Retry audio**; the app disarms itself if the stream stops.

---

## 2. Install (Windows, step by step)

1. Install Python 3.11, 3.12 or 3.13 (64-bit) from python.org. Tick **Add python.exe to PATH**.
2. Open PowerShell in the project folder (`ShotgunKeyboard\`).
3. Create and activate a virtual environment:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
   If activation is blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then retry.
4. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
5. Generate the sound (the app also does this automatically if the file is missing):
   ```powershell
   python generate_sound.py
   ```
6. Run:
   ```powershell
   python shotgunkeyboard.py
   ```

The app starts **disarmed** (safe default). Click **ARM** in the window, choose **Armed** in the tray menu, or press **Ctrl+Alt+Shift+K**.

Tray menu: Armed ON/OFF, Volume (Loud/Medium/Quiet), Open Settings, Test Sound, Start with Windows, Quit. Closing or minimizing the window hides it to the tray; **Quit** removes the hook, stops audio, removes the tray icon, saves settings and exits.

### Sound generator options

```powershell
python generate_sound.py --pitch 0.8 --intensity 0.9 --duration 220 --decay 1.2 --out sounds\shotgun.wav
```

In code: `generate_shotgun(sample_rate, duration_ms, pitch, intensity, decay, seed)`.

### Configuration

Stored as JSON: `config.json` next to the code when run from source, `%APPDATA%\ShotgunKeyboard\config.json` for the EXE. Missing or corrupted files fall back to safe defaults (a corrupt file is kept as `config.json.bad`). Armed state starts OFF unless "Re-arm automatically on launch" is enabled. Only the settings listed in `config.json` are stored; never keystrokes.

A small log (`shotgunkeyboard.log`, errors and lifecycle events only, no keys) sits beside the config.

---

## 3. Package as a standalone EXE

Double-click or run `build_exe.bat`, or run this exact command from the activated virtual environment:

```powershell
pyinstaller --noconfirm --clean --onefile --windowed --name ShotgunKeyboard --icon=NONE --add-data "sounds;sounds" --hidden-import pystray._win32 --hidden-import keyboard._winkeyboard --hidden-import keyboard._winmouse --collect-all sounddevice --collect-all _sounddevice_data shotgunkeyboard.py
```

Output: `dist\ShotgunKeyboard.exe`. On Windows `--add-data` uses `;` between source and destination. The WAV is bundled, and regenerated on first launch if it is somehow missing. Paths use `sys._MEIPASS` when frozen.

Building must be done **on Windows**; PyInstaller cannot cross-compile.

---

## 4. Troubleshooting

**Keyboard hook unavailable / no sound for some windows.** Open **System → Restart as Administrator** (one UAC prompt, never repeated automatically). Elevated programs only deliver key events to elevated hooks. Close other tools that install exclusive keyboard hooks, then click **Retry keyboard hook**. The Dashboard shows a counter of key events seen; if it stays at 0 while you type in a normal window, the hook is not receiving events.

*Start with Windows* uses a normal (non-elevated) login entry. To launch elevated at login, create a Task Scheduler task with "Run with highest privileges".

**No sound / audio error.** Check that a playback device is enabled (Settings → System → Sound), close exclusive-mode apps, then click **Retry audio**. Test Sound plays even when disarmed. If volume feels low, raise the Volume slider (the curve is quadratic; Loud = 85%).

**Windows SmartScreen ("Windows protected your PC").** The EXE is unsigned. Click **More info → Run anyway**. Code signing needs a paid certificate. Antivirus tools may also flag any unsigned program that installs a global keyboard hook, including PyInstaller one-file builds; the source is here for review, and running from source avoids this. You can add an exclusion for `dist\ShotgunKeyboard.exe`.

**"Already running" message.** Only one instance is allowed. Find the tray icon (click `^` near the clock).

**Tray icon missing.** Windows may hide it in the overflow area; drag it to the taskbar. If the tray cannot be created, the window stays open and closing it quits the app.

**`pip install` fails on PySide6/numpy.** Use 64-bit Python 3.11-3.13 and upgrade pip (`python -m pip install --upgrade pip`).

---

## 5. Test checklist

Honest status. "Automated" = run in a Linux sandbox without audio hardware, a real keyboard or Windows. "**Needs Windows check**" = not yet verified; please confirm on your machine.

| Check | Status |
|---|---|
| Sound synthesis: duration 100-300 ms, no NaN/clipping at parameter extremes, click-free edges, WAV round-trip | Automated: pass |
| Overlap: new key press does not cut off the previous voice | Automated (mixer): pass |
| Voice cap (default 16) recycles oldest, bounded memory, no unbounded threads | Automated: pass |
| Summed output never exceeds full scale (soft limiter) | Automated: pass |
| Held keys do not retrigger unless enabled; disarmed is silent; own hotkey ignored | Automated (simulated events): pass |
| No duplicate hook; stop() removes all hooks; hook failure reported, never claims active | Automated (fake hook): pass |
| Settings: defaults, corrupt file, invalid values clamped, persistence, only known keys saved, starts disarmed | Automated: pass |
| UI builds, all three pages render, settings sliders update state and save (debounced) | Automated (offscreen Qt): pass |
| Missing audio device / hook failure / tray failure show visible messages; arming refused | Automated (this sandbox has none): pass |
| App launches on Windows | **Needs Windows check** |
| Tray icon appears; armed/disarmed icon changes; menu items work | **Needs Windows check** |
| Global detection in Notepad, Explorer, browsers | **Needs Windows check** |
| Audible sound, latency, overlap during fast typing on real hardware | **Needs Windows check** |
| Volume (tray + slider), Test Sound | **Needs Windows check** |
| Settings persist after restart; Start with Windows registry entry | **Needs Windows check** |
| Quit leaves no process; hook removed (typing after Quit is silent) | **Needs Windows check** |
| Hook behaviour with an elevated window; Restart as Administrator | **Needs Windows check** |
| PyInstaller EXE builds and runs on a PC without Python | **Needs Windows check** |

Run the automated tests anywhere:

```powershell
python -m unittest discover -s tests -v
```

---

## 6. Privacy and security

* Key presses only trigger a sound. No key names, characters or history are stored, logged, or sent anywhere. The app has no networking code.
* While a key is held, only an integer scan code lives in memory (to ignore auto-repeat) and it is discarded on key release.
* No stealth, remote-control or surveillance features. Armed/disarmed state is always visible in the window and tray icon.
* Elevation is requested only when you click **Restart as Administrator**, once per click.
