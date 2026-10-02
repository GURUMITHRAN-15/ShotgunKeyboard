"""Low-latency overlapping playback.

Design
------
One long-lived `sounddevice.OutputStream` (PortAudio, WASAPI/MME on Windows) pulls
audio from a small software mixer. A key press just appends a lightweight voice
(a read cursor into the shared sound buffer) - no thread or audio object is
created per key press, so fast typing cannot exhaust threads or handles.

* Up to `max_voices` voices play at once; they are summed, never cut off.
* When the cap is reached the OLDEST voice gets a ~3 ms fade-out (no click) and
  the new voice starts immediately.
* A soft-knee limiter keeps the summed output below full scale (no hard clipping).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from generate_sound import (
    DEFAULT_DURATION_MS,
    DEFAULT_INTENSITY,
    DEFAULT_PITCH,
    SAMPLE_RATE,
    SoundFileError,
    generate_shotgun,
    read_wav,
)

log = logging.getLogger(__name__)

STEAL_FADE_SAMPLES = 128   # ~2.9 ms fade when the oldest voice is recycled
LIMITER_KNEE = 0.7         # below this the signal passes untouched


class AudioError(RuntimeError):
    """Audio device / stream could not be opened or has failed."""


class _Voice:
    __slots__ = ("buf", "pos", "fade")

    def __init__(self, buf: np.ndarray):
        self.buf = buf
        self.pos = 0
        self.fade = -1  # -1 = playing normally, >=0 = fading out (samples left)


def build_sound_buffer(duration_ms: float, pitch: float, intensity: float,
                       wav_path: Path | None = None) -> tuple[np.ndarray, str | None]:
    """Return (float32 samples, optional warning).

    With the default sound parameters the generated shotgun.wav is loaded; any
    other combination (or an unusable file) is synthesised live with the same code
    that produced the file, so both paths are format-compatible.
    """
    warning = None
    is_default = (
        int(duration_ms) == DEFAULT_DURATION_MS
        and abs(pitch - DEFAULT_PITCH) < 1e-9
        and abs(intensity - DEFAULT_INTENSITY) < 1e-9
    )
    if is_default and wav_path is not None:
        try:
            samples, rate = read_wav(wav_path)
            if rate == SAMPLE_RATE:
                return samples, None
            warning = "shotgun.wav has an unexpected sample rate; using live synthesis instead."
        except SoundFileError as exc:
            warning = f"Could not load shotgun.wav ({exc}); using live synthesis instead."
    return generate_shotgun(SAMPLE_RATE, duration_ms, pitch, intensity), warning


class AudioEngine:
    def __init__(self, sample_rate: int = SAMPLE_RATE, max_voices: int = 16, volume: int = 50):
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._voices: list[_Voice] = []
        self._buffer: np.ndarray | None = None
        self._max_voices = max(1, int(max_voices))
        self._gain = self._volume_to_gain(volume)
        self._stream = None
        self.device_name = ""
        self.last_error: str | None = None

    # ------------------------------------------------------------------ config
    @staticmethod
    def _volume_to_gain(volume: float) -> float:
        v = min(100.0, max(0.0, float(volume))) / 100.0
        return v * v  # perceptual-ish curve; max output peak = 0.8 * 1.0

    def set_volume(self, volume: float) -> None:
        self._gain = self._volume_to_gain(volume)

    def set_max_voices(self, count: int) -> None:
        with self._lock:
            self._max_voices = max(1, int(count))
            live = [v for v in self._voices if v.fade < 0]
            while len(live) > self._max_voices:
                live.pop(0).fade = STEAL_FADE_SAMPLES

    def set_buffer(self, samples: np.ndarray) -> None:
        arr = np.ascontiguousarray(samples, dtype=np.float32)
        if arr.ndim != 1 or arr.size == 0 or not np.isfinite(arr).all():
            raise ValueError("sound buffer must be a non-empty finite 1-D array")
        self._buffer = arr  # atomic swap; playing voices keep their old array

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice as sd  # lazy: lets the mixer be tested without PortAudio
        except Exception as exc:  # ImportError, or OSError when PortAudio is missing
            self.last_error = f"Audio library failed to load: {exc}"
            raise AudioError(self.last_error) from exc
        try:
            device = sd.query_devices(kind="output")  # raises when no output device exists
            stream = sd.OutputStream(
                samplerate=self.sample_rate, channels=1, dtype="float32",
                latency="low", callback=self._callback,
            )
            stream.start()
        except Exception as exc:
            self.last_error = f"Could not open an audio output device: {exc}"
            raise AudioError(self.last_error) from exc
        self._stream = stream
        self.device_name = str(device.get("name", "")) if isinstance(device, dict) else ""
        self.last_error = None
        log.info("Audio stream started on '%s'", self.device_name)

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        with self._lock:
            self._voices.clear()

    def recover(self) -> None:
        """Close and reopen the stream (used by 'Retry audio')."""
        self.close()
        self.start()

    @property
    def is_running(self) -> bool:
        stream = self._stream
        if stream is None:
            return False
        try:
            return bool(stream.active)
        except Exception:
            return False

    @property
    def active_voices(self) -> int:
        return sum(1 for v in list(self._voices) if v.fade < 0)

    # ------------------------------------------------------------------ playback
    def trigger(self) -> bool:
        """Start one overlapping voice. Cheap and safe to call from any thread."""
        buf = self._buffer
        if buf is None or not self.is_running:
            return False
        with self._lock:
            live = [v for v in self._voices if v.fade < 0]
            while len(live) >= self._max_voices:
                live.pop(0).fade = STEAL_FADE_SAMPLES  # recycle oldest, with a tiny fade
            if len(self._voices) >= 2 * self._max_voices:  # hard bound on fading voices
                self._voices = [v for v in self._voices if v.fade < 0]
            self._voices.append(_Voice(buf))
        return True

    def render(self, frames: int) -> np.ndarray:
        """Mix `frames` samples. Runs on the audio thread (also used by tests)."""
        out = np.zeros(frames, dtype=np.float32)
        with self._lock:
            alive: list[_Voice] = []
            for v in self._voices:
                n = min(frames, len(v.buf) - v.pos)
                if n <= 0:
                    continue
                seg = v.buf[v.pos:v.pos + n]
                if v.fade >= 0:
                    m = min(n, v.fade)
                    if m > 0:
                        ramp = (v.fade - np.arange(m, dtype=np.float32)) / STEAL_FADE_SAMPLES
                        out[:m] += seg[:m] * ramp
                    v.fade -= m
                    v.pos += n
                    if v.fade > 0 and v.pos < len(v.buf):
                        alive.append(v)
                else:
                    out[:n] += seg
                    v.pos += n
                    if v.pos < len(v.buf):
                        alive.append(v)
            self._voices = alive

        out *= self._gain
        a = np.abs(out)
        if frames and float(a.max()) > LIMITER_KNEE:  # soft-knee limiter, output never exceeds 1.0
            over = a > LIMITER_KNEE
            span = 1.0 - LIMITER_KNEE
            out[over] = np.sign(out[over]) * (LIMITER_KNEE + span * np.tanh((a[over] - LIMITER_KNEE) / span))
        return out

    def _callback(self, outdata, frames, time_info, status) -> None:  # audio thread
        try:
            outdata[:, 0] = self.render(frames)
        except Exception as exc:  # never let an exception kill the stream
            outdata.fill(0)
            self.last_error = f"Playback error: {exc}"
