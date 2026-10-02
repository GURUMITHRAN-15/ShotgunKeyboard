"""Hardware-free tests (no audio device, no real keyboard hook, no Windows needed).

Run:  python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generate_sound import (SAMPLE_RATE, PEAK, SoundFileError, ensure_sound_file,  # noqa: E402
                            generate_shotgun, read_wav, write_wav)
from src.audio_engine import STEAL_FADE_SAMPLES, AudioEngine, build_sound_buffer  # noqa: E402
from src.keyboard_listener import KeyboardHookError, KeyboardListener  # noqa: E402
from src.settings_manager import DEFAULTS, SettingsManager  # noqa: E402


class SynthesisTests(unittest.TestCase):
    def test_defaults_are_in_spec(self):
        x = generate_shotgun()
        self.assertEqual(x.dtype, np.float32)
        ms = len(x) / SAMPLE_RATE * 1000
        self.assertTrue(100 <= ms <= 300)
        self.assertLessEqual(float(np.max(np.abs(x))), PEAK + 1e-4)
        self.assertTrue(np.isfinite(x).all())

    def test_no_clicks_at_edges(self):
        x = generate_shotgun()
        self.assertLess(abs(float(x[0])), 0.01)
        self.assertLess(abs(float(x[-1])), 0.01)

    def test_parameters_change_the_sound(self):
        base = generate_shotgun()
        self.assertFalse(np.allclose(base, generate_shotgun(pitch=1.5)))
        self.assertFalse(np.allclose(base, generate_shotgun(intensity=0.2)))
        self.assertEqual(len(generate_shotgun(duration_ms=300)), int(SAMPLE_RATE * 0.3))

    def test_extremes_never_clip_or_nan(self):
        for kw in (dict(duration_ms=50, pitch=4), dict(duration_ms=1000, pitch=0.25, decay=4),
                   dict(intensity=0), dict(intensity=1)):
            x = generate_shotgun(**kw)
            self.assertTrue(np.isfinite(x).all())
            self.assertLessEqual(float(np.max(np.abs(x))), PEAK + 1e-4)

    def test_wav_roundtrip_and_ensure(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s" / "shotgun.wav"
            ensure_sound_file(p)
            samples, rate = read_wav(p)
            self.assertEqual(rate, SAMPLE_RATE)
            self.assertTrue(np.allclose(samples, generate_shotgun(), atol=1e-3))
            p.write_bytes(b"not a wav")  # corrupted -> regenerated
            ensure_sound_file(p)
            read_wav(p)

    def test_read_wav_rejects_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.wav"
            p.write_bytes(b"garbage")
            with self.assertRaises(SoundFileError):
                read_wav(p)
            with self.assertRaises(SoundFileError):
                read_wav(Path(d) / "missing.wav")

    def test_buffer_falls_back_when_wav_missing(self):
        buf, warning = build_sound_buffer(180, 1.0, 0.7, Path("/nonexistent/shotgun.wav"))
        self.assertGreater(len(buf), 0)
        self.assertIsNotNone(warning)


class MixerTests(unittest.TestCase):
    def make(self, voices=16, volume=100):
        e = AudioEngine(max_voices=voices, volume=volume)
        e.set_buffer(generate_shotgun())
        e._stream = SimpleNamespace(active=True)  # pretend a stream is running
        return e

    def test_overlap_does_not_cut_off_previous_sound(self):
        e = self.make()
        e.trigger()
        first = e.render(512).copy()
        e.trigger()           # second press while the first is still playing
        mixed = e.render(512)
        solo = self.make()
        solo.trigger()
        solo.render(512)
        solo_block = solo.render(512)
        self.assertEqual(e.active_voices, 2)
        self.assertFalse(np.allclose(mixed, solo_block))  # contains both voices
        self.assertGreater(float(np.max(np.abs(first))), 0)

    def test_voice_cap_recycles_oldest(self):
        e = self.make(voices=4)
        for _ in range(50):
            e.trigger()
        self.assertEqual(e.active_voices, 4)
        self.assertLessEqual(len(e._voices), 8)  # bounded: live + fading
        e.render(STEAL_FADE_SAMPLES + 10)
        self.assertLessEqual(len(e._voices), 4 + 0)

    def test_output_never_exceeds_full_scale(self):
        e = self.make(voices=32, volume=100)
        for _ in range(32):
            e.trigger()
        peak = 0.0
        for _ in range(40):
            peak = max(peak, float(np.max(np.abs(e.render(256)))))
        self.assertLessEqual(peak, 1.0)

    def test_sound_ends_and_voices_are_released(self):
        e = self.make()
        e.trigger()
        for _ in range(60):
            e.render(256)
        self.assertEqual(len(e._voices), 0)
        self.assertEqual(float(np.max(np.abs(e.render(256)))), 0.0)

    def test_volume_scales_output(self):
        loud, quiet = self.make(volume=100), self.make(volume=30)
        loud.trigger(), quiet.trigger()
        self.assertGreater(float(np.max(np.abs(loud.render(512)))), float(np.max(np.abs(quiet.render(512)))))

    def test_trigger_refuses_when_stream_not_running(self):
        e = self.make()
        e._stream = None
        self.assertFalse(e.trigger())

    def test_start_without_device_raises_audio_error(self):
        from src.audio_engine import AudioError
        e = AudioEngine()
        try:
            e.start()
        except AudioError as exc:
            self.assertIn("audio", str(exc).lower())
            self.assertFalse(e.is_running)
        else:
            e.close()  # a real device exists on this machine; nothing to assert


class FakeKeyboard:
    def __init__(self, fail=False):
        self.fail, self.hooks, self.hotkeys, self.pressed = fail, [], [], set()

    def hook(self, cb, suppress=False):
        if self.fail:
            raise OSError("access denied")
        self.hooks.append(cb)
        return cb

    def add_hotkey(self, combo, cb, suppress=False):
        self.hotkeys.append(cb)
        return cb

    def unhook(self, h):
        self.hooks.remove(h)

    def remove_hotkey(self, h):
        self.hotkeys.remove(h)

    def is_pressed(self, k):
        return k in self.pressed


def ev(kind, sc, name="a"):
    return SimpleNamespace(event_type=kind, scan_code=sc, name=name, is_keypad=False)


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.hits, self.armed, self.repeat, self.toggles = 0, True, False, 0
        self.kb = FakeKeyboard()
        self.l = KeyboardListener(on_key=self._hit, is_armed=lambda: self.armed,
                                  allow_repeat=lambda: self.repeat,
                                  on_toggle_hotkey=self._toggle, keyboard_module=self.kb)

    def _hit(self):
        self.hits += 1

    def _toggle(self):
        self.toggles += 1

    def test_press_triggers_once_and_holds_do_not_repeat(self):
        self.l.start()
        self.l._on_event(ev("down", 30))
        for _ in range(5):
            self.l._on_event(ev("down", 30))  # auto-repeat while held
        self.assertEqual(self.hits, 1)
        self.l._on_event(ev("up", 30))
        self.l._on_event(ev("down", 30))
        self.assertEqual(self.hits, 2)

    def test_repeat_when_configured(self):
        self.repeat = True
        self.l._on_event(ev("down", 30))
        self.l._on_event(ev("down", 30))
        self.assertEqual(self.hits, 2)

    def test_disarmed_is_silent_but_counts_events(self):
        self.armed = False
        self.l._on_event(ev("down", 30))
        self.assertEqual(self.hits, 0)
        self.assertEqual(self.l.events_seen, 1)

    def test_own_hotkey_final_key_is_ignored(self):
        self.kb.pressed = {"ctrl", "alt", "shift"}
        self.l._on_event(ev("down", 37, name="k"))
        self.assertEqual(self.hits, 0)
        self.kb.pressed = set()
        self.l._on_event(ev("up", 37, name="k"))
        self.l._on_event(ev("down", 37, name="k"))
        self.assertEqual(self.hits, 1)

    def test_no_duplicate_hooks_and_clean_stop(self):
        self.l.start(), self.l.start()
        self.assertEqual(len(self.kb.hooks), 1)
        self.l.stop()
        self.assertEqual((self.kb.hooks, self.kb.hotkeys), ([], []))
        self.assertFalse(self.l.is_active)
        self.l.stop()  # idempotent

    def test_hook_failure_is_reported_and_never_claims_active(self):
        bad = KeyboardListener(on_key=self._hit, is_armed=lambda: True,
                               keyboard_module=FakeKeyboard(fail=True))
        with self.assertRaises(KeyboardHookError):
            bad.start()
        self.assertFalse(bad.is_active)

    def test_handler_exceptions_do_not_propagate(self):
        def boom():
            raise RuntimeError("x")
        l = KeyboardListener(on_key=boom, is_armed=lambda: True, keyboard_module=self.kb)
        l._on_event(ev("down", 30))  # must not raise


class SettingsTests(unittest.TestCase):
    def test_missing_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            m = SettingsManager(Path(d) / "config.json")
            self.assertEqual(m.load(), [])
            self.assertEqual(m.as_dict(), DEFAULTS)

    def test_corrupt_file_restores_defaults_with_warning(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text("{not json")
            m = SettingsManager(p)
            self.assertTrue(m.load())
            self.assertEqual(m.as_dict(), DEFAULTS)

    def test_invalid_values_are_clamped_or_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"volume": 999, "pitch": "loud", "max_voices": -4,
                                     "launch_minimized": "yes", "evil": 1}))
            m = SettingsManager(p)
            warnings = m.load()
            self.assertEqual(m.get("volume"), 100)
            self.assertEqual(m.get("pitch"), DEFAULTS["pitch"])
            self.assertEqual(m.get("max_voices"), 1)
            self.assertEqual(len(warnings), 2)
            self.assertNotIn("evil", m.as_dict())

    def test_persistence_and_safe_armed_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            m = SettingsManager(p)
            m.update(volume=33, armed=True, launch_minimized=True)
            m2 = SettingsManager(p)
            m2.load()
            self.assertEqual(m2.get("volume"), 33)
            self.assertTrue(m2.get("launch_minimized"))
            self.assertFalse(m2.get("armed"))  # always starts disarmed unless restore_armed
            m.update(restore_armed=True)
            m3 = SettingsManager(p)
            m3.load()
            self.assertTrue(m3.get("armed"))

    def test_saved_file_contains_only_known_settings(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            m = SettingsManager(p)
            m.save()
            self.assertEqual(set(json.loads(p.read_text())), set(DEFAULTS))


if __name__ == "__main__":
    unittest.main()
