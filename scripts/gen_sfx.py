"""Generate the presenter sound effects (WO-062P) with numpy: no samples, no licences.

Writes six mono 16-bit WAVs to ``web/public/sfx`` (or ``--out``), each under one second
and peak-normalised to -18 dBFS:

- ``hail_ping.wav``          a short sonar-style ping with one faint echo (hail sent)
- ``advisory_chime.wav``     two notes, falling (advisory raised)
- ``hold_tone.wav``          a low 0.5 s tone (hold placed)
- ``handoff_chime.wav``      a warm major arpeggio that resolves (handed to a person)
- ``twist_klaxon_lite.wav``  a brief, soft two-tone (a twist toggled)
- ``responder_dispatch.wav`` two short rising notes (the responder dispatched to look, WO-088)

Deterministic: no randomness, fixed sample rate, and the stdlib ``wave`` writer, so two
runs give identical bytes (``tests/test_sfx.py`` regenerates and compares).

    uv run python scripts/gen_sfx.py [--out DIR]
"""

from __future__ import annotations

import argparse
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

SAMPLE_RATE = 22_050
PEAK_DBFS = -18.0
FULL_SCALE = 32767
REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "web" / "public" / "sfx"

Signal = NDArray[np.float64]


def _t(seconds: float) -> Signal:
    return np.arange(round(seconds * SAMPLE_RATE), dtype=np.float64) / SAMPLE_RATE


def _env(n: int, attack_s: float, release_s: float) -> Signal:
    """Linear attack and release around a flat sustain, so nothing clicks."""
    env = np.ones(n, dtype=np.float64)
    a = min(n, max(1, round(attack_s * SAMPLE_RATE)))
    r = min(n, max(1, round(release_s * SAMPLE_RATE)))
    env[:a] *= np.linspace(0.0, 1.0, a, endpoint=False)
    env[n - r :] *= np.linspace(1.0, 0.0, r)
    return env


def _tone(freq: float, seconds: float, harmonics: tuple[float, ...] = (1.0,)) -> Signal:
    t = _t(seconds)
    out = np.zeros_like(t)
    for k, amp in enumerate(harmonics, start=1):
        out += amp * np.sin(2.0 * np.pi * freq * k * t)
    return out


def _place(total_s: float, parts: list[tuple[float, Signal]]) -> Signal:
    out = np.zeros(round(total_s * SAMPLE_RATE), dtype=np.float64)
    for start_s, sig in parts:
        i = round(start_s * SAMPLE_RATE)
        j = min(len(out), i + len(sig))
        out[i:j] += sig[: j - i]
    return out


def hail_ping() -> Signal:
    seconds = 0.8
    t = _t(seconds)
    ping = np.sin(2.0 * np.pi * 1320.0 * t) * np.exp(-t / 0.09)
    ping *= _env(len(t), 0.004, 0.05)
    echo = 0.3 * ping[: round(0.5 * SAMPLE_RATE)]
    return _place(seconds, [(0.0, ping), (0.28, echo)])


def advisory_chime() -> Signal:
    def note(freq: float) -> Signal:
        sig = _tone(freq, 0.36, (1.0, 0.35, 0.12))
        return sig * np.exp(-_t(0.36) / 0.16) * _env(len(sig), 0.006, 0.06)

    return _place(0.72, [(0.0, note(987.77)), (0.32, note(783.99))])


def hold_tone() -> Signal:
    sig = _tone(196.0, 0.5, (1.0, 0.5, 0.2, 0.08))
    return sig * _env(len(sig), 0.03, 0.12)


def handoff_chime() -> Signal:
    def note(freq: float, seconds: float) -> Signal:
        sig = _tone(freq, seconds, (1.0, 0.25, 0.06))
        return sig * np.exp(-_t(seconds) / 0.3) * _env(len(sig), 0.008, 0.1)

    return _place(
        0.92,
        [
            (0.0, note(523.25, 0.9)),
            (0.12, note(659.25, 0.78)),
            (0.24, note(783.99, 0.66)),
            (0.36, note(1046.50, 0.56)),
        ],
    )


def twist_klaxon_lite() -> Signal:
    def blip(freq: float) -> Signal:
        sig = _tone(freq, 0.17, (1.0, 0.0, 0.2, 0.0, 0.07))
        return sig * _env(len(sig), 0.012, 0.04)

    return _place(
        0.72,
        [(0.0, blip(440.0)), (0.18, blip(554.37)), (0.36, blip(440.0)), (0.54, blip(554.37))],
    )


SOUNDS: dict[str, Callable[[], Signal]] = {
    "hail_ping": hail_ping,
    "advisory_chime": advisory_chime,
    "hold_tone": hold_tone,
    "handoff_chime": handoff_chime,
    "twist_klaxon_lite": twist_klaxon_lite,
}


# --- WO-088 begin ---
def responder_dispatch() -> Signal:
    """Two short notes, a fourth apart and rising: the platform goes to look."""

    def note(freq: float) -> Signal:
        sig = _tone(freq, 0.2, (1.0, 0.3, 0.08))
        return sig * np.exp(-_t(0.2) / 0.1) * _env(len(sig), 0.006, 0.05)

    return _place(0.42, [(0.0, note(659.25)), (0.2, note(880.0))])


SOUNDS["responder_dispatch"] = responder_dispatch
# --- WO-088 end ---


def to_pcm16(sig: Signal, peak_dbfs: float = PEAK_DBFS) -> bytes:
    """Peak-normalise to ``peak_dbfs`` and quantise to little-endian 16-bit PCM."""
    peak = float(np.max(np.abs(sig)))
    if peak <= 0.0:
        raise ValueError("silent signal")
    target = 10.0 ** (peak_dbfs / 20.0)
    pcm = np.round(sig / peak * target * FULL_SCALE).astype("<i2")
    return bytes(pcm.tobytes())


def write_wav(path: Path, sig: Signal) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(to_pcm16(sig))


def generate(out: Path = DEFAULT_OUT) -> list[Path]:
    paths = []
    for name, make in SOUNDS.items():
        path = out / f"{name}.wav"
        write_wav(path, make())
        paths.append(path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    for path in generate(args.out):
        print(path)


if __name__ == "__main__":
    main()
