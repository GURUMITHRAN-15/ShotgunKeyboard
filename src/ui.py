"""ShotgunKeyboard desktop UI (PySide6): Dashboard, Sound settings, System.

The window only talks to the controller (see shotgunkeyboard.AppController):
it reads `ctrl.snapshot()` and calls controller methods. It never touches the
keyboard hook or audio stream directly, so it stays responsive.
"""

from __future__ import annotations

import io

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QScrollArea, QSlider, QStackedWidget, QVBoxLayout, QWidget,
)

from .tray_manager import make_icon_image

STYLE = """
#root { background: #0E1320; }
QLabel, QCheckBox { background: transparent; color: #E6EAF2;
    font-family: "Segoe UI Variable Text", "Segoe UI", "Inter", sans-serif; font-size: 13px; }
QMessageBox, QDialog { background: #141B2D; }

QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #2C3860; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

#sidebar { background: #0A0E19; border-right: 1px solid #1B2338; }
#brand { font-size: 17px; font-weight: 700; color: #FF7A18; }
#version { color: #5C6682; font-size: 11px; }
QPushButton#nav { text-align: left; padding: 10px 14px; border: none; border-radius: 10px;
    color: #8A94AD; font-weight: 600; font-size: 13px; background: transparent; }
QPushButton#nav:hover { background: #161D2F; color: #E6EAF2; }
QPushButton#nav:checked { background: #1F2740; color: #FF7A18; }

#card { background: #141B2D; border: 1px solid #232C45; border-radius: 16px; }
#cardTitle { color: #8A94AD; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
#h1 { font-size: 24px; font-weight: 700; }
#muted { color: #8A94AD; }
#hint { color: #6F7A97; font-size: 12px; }

#stateLabel { font-size: 34px; font-weight: 800; color: #8A94AD; }
#stateLabel[armed="true"] { color: #FF7A18; }
#statusValue { font-size: 16px; font-weight: 700; }
#statusValue[state="ok"] { color: #2ECC71; }
#statusValue[state="warn"] { color: #FF7A18; }
#statusValue[state="bad"] { color: #FF5C5C; }

#banner { background: #2A1A1A; border: 1px solid #5A2B2B; border-radius: 14px; }
#bannerText { color: #FFB4B4; }

QPushButton { font-family: "Segoe UI Variable Text", "Segoe UI", sans-serif; font-size: 13px; }
QPushButton#primary, QPushButton#armToggle { background: #FF7A18; color: #10131C; border: none;
    border-radius: 12px; padding: 10px 20px; font-weight: 700; }
QPushButton#primary:hover, QPushButton#armToggle:hover { background: #FF9340; }
QPushButton#armToggle { min-width: 170px; min-height: 40px; font-size: 15px; }
QPushButton#armToggle[armed="true"] { background: #2A3350; color: #E6EAF2; border: 1px solid #FF7A18; }
QPushButton#armToggle[armed="true"]:hover { background: #343F63; }
QPushButton#secondary { background: #1C2540; color: #E6EAF2; border: 1px solid #2C3860;
    border-radius: 10px; padding: 8px 16px; font-weight: 600; }
QPushButton#secondary:hover { background: #243055; }
QPushButton:disabled { background: #1A2036; color: #566082; border: 1px solid #232C45; }

QSlider::groove:horizontal { height: 6px; background: #232C45; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #FF7A18; border-radius: 3px; }
QSlider::handle:horizontal { background: #FFB070; width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; }
QSlider::handle:horizontal:hover { background: #FFD0A0; }

QCheckBox { spacing: 10px; padding: 4px 0; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 5px; border: 1px solid #3A4670; background: #101728; }
QCheckBox::indicator:checked { background: #FF7A18; border: 1px solid #FF7A18; }
"""


def qicon_from_pil(img) -> QIcon:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    pm = QPixmap()
    pm.loadFromData(buf.getvalue(), "PNG")
    return QIcon(pm)


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def make_card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(20, 18, 20, 18)
    lay.setSpacing(12)
    if title:
        t = QLabel(title.upper())
        t.setObjectName("cardTitle")
        lay.addWidget(t)
    return card, lay


def label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    w = QLabel(text)
    if name:
        w.setObjectName(name)
    w.setWordWrap(wrap)
    return w


class LabeledSlider(QWidget):
    changed = Signal(int)

    def __init__(self, title: str, lo: int, hi: int, unit: str = "", hint: str = ""):
        super().__init__()
        self.unit = unit
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.addWidget(label(title))
        head.addStretch(1)
        self.value_label = label("", "statusValue")
        self.value_label.setProperty("state", "warn")
        head.addWidget(self.value_label)
        lay.addLayout(head)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(lo, hi)
        self.slider.valueChanged.connect(self._on_change)
        lay.addWidget(self.slider)
        if hint:
            lay.addWidget(label(hint, "hint"))

    def _on_change(self, value: int) -> None:
        self.value_label.setText(f"{value}{self.unit}")
        self.changed.emit(value)

    def set_value(self, value: int) -> None:
        if self.slider.isSliderDown() or self.slider.value() == value:
            return
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)
        self.value_label.setText(f"{value}{self.unit}")


class MainWindow(QMainWindow):
    def __init__(self, ctrl):
        super().__init__()
        self.ctrl = ctrl
        self.setWindowTitle("ShotgunKeyboard")
        self.setMinimumSize(780, 600)
        self.resize(920, 680)
        self.setWindowIcon(qicon_from_pil(make_icon_image(True, 256)))
        self.setStyleSheet(STYLE)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_sidebar())
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        for build in (self._build_dashboard, self._build_sound, self._build_system):
            self.stack.addWidget(self._scrollable(build()))

        self._anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

        self._tick = QTimer(self)
        self._tick.setInterval(400)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        self.refresh()

    # ------------------------------------------------------------------ builders
    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(196)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(14, 22, 14, 16)
        lay.setSpacing(6)
        lay.addWidget(label("SHOTGUN\nKEYBOARD", "brand"))
        lay.addSpacing(18)
        group = QButtonGroup(self)
        group.setExclusive(True)
        for i, name in enumerate(("Dashboard", "Sound", "System")):
            b = QPushButton(name)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _checked=False, idx=i: self.stack.setCurrentIndex(idx))
            group.addButton(b)
            lay.addWidget(b)
        lay.addStretch(1)
        lay.addWidget(label(f"v{self.ctrl.version}", "version"))
        return side

    @staticmethod
    def _scrollable(page: QWidget) -> QScrollArea:
        """Pages scroll instead of squeezing/clipping when content is taller than the window."""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(page)
        return area

    def _page(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 26, 28, 22)
        lay.setSpacing(16)
        return page, lay

    def _build_dashboard(self) -> QWidget:
        page, lay = self._page()
        lay.addWidget(label("ShotgunKeyboard", "h1"))
        lay.addWidget(label("Every keystroke, a shotgun blast. Runs quietly in the system tray.", "muted"))

        # error / notice banner
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        bl = QVBoxLayout(self.banner)
        bl.setContentsMargins(16, 12, 16, 12)
        bl.setSpacing(10)
        self.banner_text = label("", "bannerText", wrap=True)
        bl.addWidget(self.banner_text)
        row = QHBoxLayout()
        self.btn_retry_hook = self._button("Retry keyboard hook", "secondary", self.ctrl.retry_hook)
        self.btn_retry_audio = self._button("Retry audio", "secondary", self.ctrl.retry_audio)
        self.btn_admin = self._button("Restart as Administrator", "secondary", self.ctrl.restart_as_admin)
        self.btn_dismiss = self._button("Dismiss notices", "secondary", self.ctrl.dismiss_notices)
        for b in (self.btn_retry_hook, self.btn_retry_audio, self.btn_admin, self.btn_dismiss):
            row.addWidget(b)
        row.addStretch(1)
        bl.addLayout(row)
        lay.addWidget(self.banner)

        # armed state card
        card, cl = make_card("Status")
        row = QHBoxLayout()
        left = QVBoxLayout()
        self.state_label = label("DISARMED", "stateLabel")
        self.state_sub = label("", "muted", wrap=True)
        left.addWidget(self.state_label)
        left.addWidget(self.state_sub)
        row.addLayout(left, 1)
        self.btn_arm = self._button("ARM", "armToggle", self._toggle_armed)
        row.addWidget(self.btn_arm, 0, Qt.AlignVCenter)
        cl.addLayout(row)
        lay.addWidget(card)

        # hook + playback cards
        row = QHBoxLayout()
        row.setSpacing(16)
        c1, l1 = make_card("Keyboard hook")
        self.hook_value = label("", "statusValue")
        self.hook_detail = label("", "muted", wrap=True)
        l1.addWidget(self.hook_value)
        l1.addWidget(self.hook_detail)
        c2, l2 = make_card("Sound playback")
        self.audio_value = label("", "statusValue")
        self.audio_detail = label("", "muted", wrap=True)
        l2.addWidget(self.audio_value)
        l2.addWidget(self.audio_detail)
        row.addWidget(c1, 1)
        row.addWidget(c2, 1)
        lay.addLayout(row)

        row = QHBoxLayout()
        self.btn_test = self._button("Test Sound", "primary", self.ctrl.test_sound)
        row.addWidget(self.btn_test)
        row.addSpacing(12)
        self.hotkey_hint = label("", "hint")
        row.addWidget(self.hotkey_hint, 1)
        lay.addLayout(row)
        lay.addStretch(1)
        return page

    def _build_sound(self) -> QWidget:
        page, lay = self._page()
        lay.addWidget(label("Sound settings", "h1"))
        card, cl = make_card("Playback")
        self.s_volume = LabeledSlider("Volume", 0, 100, "%")
        self.s_intensity = LabeledSlider("Sound intensity", 0, 100, "%", "Crack and noise vs. punch, plus saturation.")
        self.s_pitch = LabeledSlider("Pitch", 50, 200, "%", "100% is the original tuning.")
        self.s_duration = LabeledSlider("Sound duration", 100, 300, " ms")
        self.s_voices = LabeledSlider("Maximum overlapping voices", 1, 32, "", "Oldest voice is recycled when the limit is reached.")
        self.s_volume.changed.connect(lambda v: self.ctrl.set_setting("volume", v))
        self.s_intensity.changed.connect(lambda v: self.ctrl.set_setting("intensity", v))
        self.s_pitch.changed.connect(lambda v: self.ctrl.set_setting("pitch", v))
        self.s_duration.changed.connect(lambda v: self.ctrl.set_setting("duration_ms", v))
        self.s_voices.changed.connect(lambda v: self.ctrl.set_setting("max_voices", v))
        for w in (self.s_volume, self.s_intensity, self.s_pitch, self.s_duration, self.s_voices):
            cl.addWidget(w)
        lay.addWidget(card)

        card2, c2 = make_card("Behaviour")
        self.chk_repeat = QCheckBox("Retrigger while a key is held down (auto-repeat)")
        self.chk_repeat.toggled.connect(lambda v: self.ctrl.set_setting("repeat_on_hold", v))
        c2.addWidget(self.chk_repeat)
        lay.addWidget(card2)

        row = QHBoxLayout()
        row.addWidget(self._button("Audio test", "primary", self.ctrl.test_sound))
        row.addWidget(self._button("Reset sound settings", "secondary", self.ctrl.reset_sound_defaults))
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return page

    def _build_system(self) -> QWidget:
        page, lay = self._page()
        lay.addWidget(label("System", "h1"))
        card, cl = make_card("Startup")
        self.chk_startup = QCheckBox("Start with Windows (run at login, minimized to tray)")
        self.chk_startup.toggled.connect(self.ctrl.set_startup)
        self.chk_minimized = QCheckBox("Launch minimized to the tray")
        self.chk_minimized.toggled.connect(lambda v: self.ctrl.set_setting("launch_minimized", v))
        self.chk_restore = QCheckBox("Re-arm automatically on launch (off = always start disarmed)")
        self.chk_restore.toggled.connect(lambda v: self.ctrl.set_setting("restore_armed", v))
        for w in (self.chk_startup, self.chk_minimized, self.chk_restore):
            cl.addWidget(w)
        lay.addWidget(card)

        card2, c2 = make_card("Permissions")
        self.admin_value = label("", "statusValue")
        self.admin_detail = label("", "muted", wrap=True)
        c2.addWidget(self.admin_value)
        c2.addWidget(self.admin_detail)
        self.btn_admin2 = self._button("Restart as Administrator", "secondary", self.ctrl.restart_as_admin)
        row = QHBoxLayout()
        row.addWidget(self.btn_admin2)
        row.addStretch(1)
        c2.addLayout(row)
        lay.addWidget(card2)

        card3, c3 = make_card("About")
        c3.addWidget(label(f"ShotgunKeyboard v{self.ctrl.version}", "statusValue"))
        c3.addWidget(label(
            "An audio-feedback utility. It plays a synthesized sound when keys are pressed and "
            "never records, stores or transmits what you type. The shotgun sound is generated "
            "mathematically; no recordings are used.", "muted", wrap=True))
        lay.addWidget(card3)
        lay.addStretch(1)
        return page

    def _button(self, text: str, kind: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName(kind)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(lambda _checked=False: slot())
        return b

    # ------------------------------------------------------------------ actions
    def _toggle_armed(self) -> None:
        self.ctrl.set_armed(not self.ctrl.snapshot()["armed"])

    # ------------------------------------------------------------------ refresh
    def _on_tick(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.refresh()

    @staticmethod
    def _set_state(widget: QLabel, state: str) -> None:
        widget.setProperty("state", state)
        repolish(widget)

    @Slot()
    def refresh(self) -> None:
        s = self.ctrl.snapshot()
        armed = s["armed"]

        self.state_label.setText("ARMED" if armed else "DISARMED")
        self.state_label.setProperty("armed", armed)
        repolish(self.state_label)
        self.btn_arm.setText("DISARM" if armed else "ARM")
        self.btn_arm.setProperty("armed", armed)
        repolish(self.btn_arm)
        self.state_sub.setText(
            "Key presses play the shotgun sound." if armed
            else "Sounds are off. Click ARM, use the tray menu, or press " + s["hotkey"] + ".")

        # keyboard hook card
        if s["hook_active"]:
            self.hook_value.setText("INSTALLED")
            self._set_state(self.hook_value, "ok")
            detail = f"Global hook is installed. Key events seen this session: {s['events_seen']}."
            if s["events_seen"] == 0:
                detail += " Press any key to confirm detection."
            self.hook_detail.setText(detail)
        else:
            self.hook_value.setText("UNAVAILABLE")
            self._set_state(self.hook_value, "bad")
            self.hook_detail.setText("Limited mode: key presses are not detected. See the notice above.")

        # playback card
        if s["audio_ok"]:
            busy = s["active_voices"] > 0
            self.audio_value.setText("PLAYING" if busy else "READY")
            self._set_state(self.audio_value, "warn" if busy else "ok")
            dev = f"{s['device']} - " if s["device"] else ""
            self.audio_detail.setText(f"{dev}{s['active_voices']} active voice(s), max {s['max_voices']}.")
        else:
            self.audio_value.setText("ERROR")
            self._set_state(self.audio_value, "bad")
            self.audio_detail.setText("No working audio output. See the notice above.")

        self.hotkey_hint.setText(f"Global shortcut: {s['hotkey']} toggles armed / disarmed.")

        # banner
        msgs = [m for m in (s["hook_error"], s["audio_error"], s["tray_error"]) if m] + list(s["notices"])
        self.banner.setVisible(bool(msgs))
        self.banner_text.setText("\n\n".join(msgs))
        self.btn_retry_hook.setVisible(bool(s["hook_error"]))
        self.btn_retry_audio.setVisible(bool(s["audio_error"]))
        self.btn_admin.setVisible(bool(s["hook_error"]) and not s["admin"])
        self.btn_dismiss.setVisible(bool(s["notices"]))

        # sound page
        self.s_volume.set_value(s["volume"])
        self.s_intensity.set_value(s["intensity"])
        self.s_pitch.set_value(s["pitch"])
        self.s_duration.set_value(s["duration_ms"])
        self.s_voices.set_value(s["max_voices"])
        self._set_check(self.chk_repeat, s["repeat_on_hold"])

        # system page
        self._set_check(self.chk_startup, s["start_with_windows"])
        self._set_check(self.chk_minimized, s["launch_minimized"])
        self._set_check(self.chk_restore, s["restore_armed"])
        if s["admin"]:
            self.admin_value.setText("ADMINISTRATOR: YES")
            self._set_state(self.admin_value, "ok")
            self.admin_detail.setText("Keys pressed in elevated programs can be detected.")
        else:
            self.admin_value.setText("ADMINISTRATOR: NO")
            self._set_state(self.admin_value, "warn")
            self.admin_detail.setText(
                "Windows does not send key events from elevated programs (Task Manager, admin "
                "terminals, installers) to normal apps. Restart as Administrator to hear those too.")
        self.btn_admin2.setEnabled(not s["admin"])

    @staticmethod
    def _set_check(box: QCheckBox, value: bool) -> None:
        if box.isChecked() != bool(value):
            box.blockSignals(True)
            box.setChecked(bool(value))
            box.blockSignals(False)

    # ------------------------------------------------------------------ window behaviour
    def show_window(self) -> None:
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self.refresh()
        self._anim.stop()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self.ctrl.tray_ok:
            event.ignore()
            self.hide()  # keep running in the tray
        else:
            event.accept()  # no tray to hide in: closing the window quits the app
            self.ctrl.quit_requested.emit()

    def changeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.type() == QEvent.WindowStateChange and self.isMinimized() and self.ctrl.tray_ok:
            QTimer.singleShot(0, self.hide)  # minimize-to-tray
        super().changeEvent(event)
