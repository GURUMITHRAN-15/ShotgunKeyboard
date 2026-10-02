#!/usr/bin/env python3
"""Procedural, original "shotgun-style" impact sound for ShotgunKeyboard.

Pure NumPy synthesis - no recordings, samples or downloaded audio.

Layers
------
1. Crack   : very short high-passed noise transient (the sharp attack).
2. Punch   : pitch-swept sine thump, 175 Hz -> 45 Hz, lightly saturated.
3. Burst   : low-passed noise body with a short exponential decay.
4. Tail    : small synthetic reverb (decaying-noise impulse response).

Output: mono, 44.1 kHz, 16-bit PCM, ~180 ms, peak-normalised to 0.8 (about -2 dBFS)
with a micro fade-in and a cosine fade-out so there are no clicks.

Usage
-----
    python generate_sound.py                       # writes sounds/shotgun.wav
    python generate_sound.py --pitch 0.8 --intensity 0.9 --duration 220 --out my.wav
"""

from __future__ import annotations

import argparse
import os
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44_100
DEFAULT_DURATION_MS = 180
DEFAULT_PITCH = 1.0        # multiplier
DEFAULT_INTENSITY = 0.7    # 0..1
DEFAULT_DECAY = 1.0        # multiplier on envelope time constants
DEFAULT_SEED = 7
PEAK = 0.8                 # output peak (full scale = 1.0)


class SoundFileError(Exception):
    """Raised when a WAV file is missing, unreadable or in an unsupported format."""


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _lowpass(x: np.ndarray, cutoff_hz: float, sr: int, taps: int = 129) -> np.ndarray:
    """Zero-phase windowed-sinc low-pass (no SciPy needed)."""
    cutoff = _clamp(cutoff_hz, 50.0, sr * 0.45)
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2.0 * cutoff / sr * n) * np.hamming(taps)
    h /= h.sum()
    half = taps // 2
    return np.convolve(x, h, mode="full")[half:half + len(x)]


def _reverb_tail(x: np.ndarray, sr: int, seed: int, tail_ms: float, wet: float) -> np.ndarray:
    """Convolve with a short decaying-noise impulse response (FFT convolution)."""
    rng = np.random.default_rng(seed + 1)
    n = max(64, int(sr * tail_ms / 1000))
    t = np.arange(n) / sr
    ir = rng.standard_normal(n) * np.exp(-t / (tail_ms / 1000 / 4))
    ir = _lowpass(ir, 3500, sr)
    ir[0] = 0.0
    ir /= np.sqrt(np.sum(ir ** 2)) + 1e-12
    size = len(x) + n - 1
    nfft = 1 << (size - 1).bit_length()
    y = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(ir, nfft), nfft)[:len(x)]
    return x + wet * y


def generate_shotgun(
    sample_rate: int = SAMPLE_RATE,
    duration_ms: float = DEFAULT_DURATION_MS,
    pitch: float = DEFAULT_PITCH,
    intensity: float = DEFAULT_INTENSITY,
    decay: float = DEFAULT_DECAY,
    seed: int = DEFAULT_SEED,
) -> np.ndarray:
    """Return the sound as a float32 mono array in [-PEAK, PEAK].

    duration_ms : total length (clamped 50-1000 ms)
    pitch       : frequency multiplier for punch + noise colour (0.25-4)
    intensity   : 0..1, balance of crack/noise vs. punch and amount of saturation
    decay       : multiplier on envelope decay times (0.25-4)
    """
    sr = int(sample_rate)
    duration_ms = _clamp(duration_ms, 50, 1000)
    pitch = _clamp(pitch, 0.25, 4.0)
    intensity = _clamp(intensity, 0.0, 1.0)
    decay = _clamp(decay, 0.25, 4.0)

    n = int(sr * duration_ms / 1000)
    t = np.arange(n) / sr
    rng = np.random.default_rng(seed)
    k = _clamp((duration_ms / DEFAULT_DURATION_MS) * decay, 0.25, 6.0)  # time-constant scale

    # 1) crack: high-passed noise, ~2.5 ms decay
    crack = rng.standard_normal(n)
    crack = crack - _lowpass(crack, 1800 * pitch, sr)
    crack *= np.exp(-t / (0.0025 * k))

    # 2) punch: exponential pitch sweep, soft-saturated
    freq = pitch * (45.0 + 130.0 * np.exp(-t / 0.028))
    phase = 2.0 * np.pi * np.cumsum(freq) / sr
    punch = np.tanh(1.8 * np.sin(phase)) * np.exp(-t / (0.050 * k))

    # 3) noise burst: low-passed noise body
    burst = _lowpass(rng.standard_normal(n), 2600 * pitch, sr) * np.exp(-t / (0.032 * k)) * 1.6

    mix = (0.45 + 0.55 * intensity) * crack + 1.0 * punch + (0.25 + 0.75 * intensity) * burst

    # saturation ("drive") scales with intensity
    drive = 1.0 + 2.5 * intensity
    mix = np.tanh(mix * drive)

    # 4) reverb tail
    mix = _reverb_tail(mix, sr, seed, tail_ms=min(120.0, duration_ms * 0.6), wet=0.15)

    # click-free envelope
    mix -= np.mean(mix)
    fade_in = max(4, int(0.0002 * sr))
    mix[:fade_in] *= np.linspace(0.0, 1.0, fade_in)
    fade_out = max(8, min(int(0.020 * sr), n // 3))
    mix[-fade_out:] *= 0.5 * (1.0 + np.cos(np.linspace(0.0, np.pi, fade_out)))

    peak = float(np.max(np.abs(mix)))
    if not np.isfinite(peak) or peak <= 0:
        raise ValueError("Synthesis produced silence or invalid samples")
    return (mix * (PEAK / peak)).astype(np.float32)


# ---------------------------------------------------------------------- WAV I/O
def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> Path:
    """Write mono 16-bit PCM WAV (atomic: temp file then rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0)
    pcm = np.round(data * 32767.0).astype("<i2")
    tmp = path.with_name(path.name + ".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())
    os.replace(tmp, path)
    return path


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Read 16-bit PCM WAV -> (float32 mono samples, sample_rate)."""
    try:
        with wave.open(str(path), "rb") as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(frames)
    except (OSError, EOFError, wave.Error) as exc:
        raise SoundFileError(str(exc)) from exc
    if width != 2:
        raise SoundFileError("only 16-bit PCM WAV files are supported")
    if channels not in (1, 2):
        raise SoundFileError("only mono or stereo WAV files are supported")
    pcm = np.frombuffer(raw, dtype="<i2")
    if pcm.size == 0:
        raise SoundFileError("WAV file contains no audio")
    if channels == 2:
        pcm = pcm.reshape(-1, 2).mean(axis=1)
    return (pcm.astype(np.float32) / 32768.0), int(rate)


def ensure_sound_file(path: str | Path, **params) -> Path:
    """Return `path`, generating it first if it is missing or invalid."""
    path = Path(path)
    try:
        samples, rate = read_wav(path)
        if rate == SAMPLE_RATE and len(samples) > 0:
            return path
    except SoundFileError:
        pass
    write_wav(path, generate_shotgun(**params))
    return path


def main() -> None:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Generate the ShotgunKeyboard impact sound.")
    p.add_argument("--out", default=str(here / "sounds" / "shotgun.wav"))
    p.add_argument("--duration", type=float, default=DEFAULT_DURATION_MS, help="milliseconds (50-1000)")
    p.add_argument("--pitch", type=float, default=DEFAULT_PITCH, help="multiplier (0.25-4)")
    p.add_argument("--intensity", type=float, default=DEFAULT_INTENSITY, help="0-1")
    p.add_argument("--decay", type=float, default=DEFAULT_DECAY, help="multiplier (0.25-4)")
    a = p.parse_args()
    samples = generate_shotgun(duration_ms=a.duration, pitch=a.pitch, intensity=a.intensity, decay=a.decay)
    out = write_wav(a.out, samples)
    print(f"Wrote {out}  ({len(samples) / SAMPLE_RATE * 1000:.0f} ms, {SAMPLE_RATE} Hz, 16-bit mono, "
          f"peak {np.max(np.abs(samples)):.2f})")


if __name__ == "__main__":
    main()
