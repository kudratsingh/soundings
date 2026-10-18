"""Demo video tooling (WO-062B): timeline, audio mix, conversion and checks.

The recording itself (Playwright over presenter mode) lives in ``soundings.demo.record``;
this module holds everything that does not need a browser, so the pure parts are tested on
every run. ffmpeg is the bundled ``imageio-ffmpeg`` binary (approved dev dependency,
DECISIONS 2026-10-16 23:05); no system ffmpeg, no network, no model call.

CLI (the Makefile targets call these)::

    python -m soundings.demo.video record   --out artifacts         # record + convert + thumbnail
    python -m soundings.demo.video vo-draft --out artifacts         # macOS `say` -> vo-draft.m4a
    python -m soundings.demo.video merge    --vo PATH [--music PATH] # -> demo-youtube.mp4
    python -m soundings.demo.video probe    PATH                    # duration, size, fps, audio
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from soundings.demo.script import DemoScript, parse_mark

REPO = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO / "docs" / "demo-script.yaml"
VO_SCRIPT_PATH = REPO / "docs" / "vo-script.md"
YOUTUBE_PATH = REPO / "docs" / "youtube.md"
SFX_DIR = REPO / "web" / "public" / "sfx"
ARTIFACTS = REPO / "artifacts"

FPS = 30
WIDTH, HEIGHT = 1920, 1080
THUMB_W, THUMB_H = 1280, 720
RATE = 48_000  # audio sample rate of every track we write
CRF = 18

# Levels (owner directive 2, item 3).
VO_LUFS = -16.0
SFX_PEAK_DBFS = -22.0
MUSIC_LUFS = -28.0

# Exit-test tolerances (WO-062B).
CAPTION_TOL_S = 0.5
SFX_TOL_S = 0.100
VO_MIN_RMS_DBFS = -40.0
WORDS_PER_10S = 28

# Model and vendor names that must not appear in the narration or the upload text.
VENDOR_NAMES = (
    "claude",
    "anthropic",
    "openai",
    "chatgpt",
    "gpt",
    "gemini",
    "llama",
    "mistral",
    "sonnet",
    "opus",
    "haiku",
    "copilot",
    "llm",
    "language model",
)
_VENDOR_RE = re.compile(r"\b(" + "|".join(re.escape(n) for n in VENDOR_NAMES) + r")\b", re.I)


# --- shot list ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Shot:
    index: int
    start_s: float
    end_s: float
    caption: str
    label: str

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def load_shots(path: Path = SCRIPT_PATH) -> tuple[list[Shot], float]:
    """The shots of the demo script with their windows, and the total length in seconds."""
    script = DemoScript.load(path)
    end = getattr(script, "end", None)
    marks = [float(parse_mark(s.t)) for s in script.steps]
    total = float(parse_mark(end)) if end else marks[-1] + 20.0
    shots = []
    for i, step in enumerate(script.steps):
        stop = marks[i + 1] if i + 1 < len(marks) else total
        label = step.scenario or (step.action or "") or ("eval" if step.eval else "")
        shots.append(Shot(i, marks[i], stop, step.caption or "", str(label)))
    return shots, total


def fmt_mark(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


# --- narration text ------------------------------------------------------------------------

_VO_LINE = re.compile(r"^\s*[-*]?\s*\*\*\[?(\d+:\d{2})\]?[^*]*\*\*\s*(.+?)\s*$")


@dataclass(frozen=True)
class VoLine:
    start_s: float
    text: str

    @property
    def words(self) -> int:
        return count_words(self.text)


def count_words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’.,%-]*", text))


def word_budget(duration_s: float) -> int:
    """At most 28 words per 10 s of shot (owner directive 1, item 4)."""
    return int(math.floor(WORDS_PER_10S * duration_s / 10.0))


def parse_vo_script(text: str) -> list[VoLine]:
    """Every narration line: a list item starting with a bold ``**m:ss ...**`` mark."""
    out = []
    for line in text.splitlines():
        m = _VO_LINE.match(line)
        if m:
            out.append(VoLine(float(parse_mark(m.group(1))), m.group(2).strip()))
    return out


def vendor_names(text: str) -> list[str]:
    return [m.group(0) for m in _VENDOR_RE.finditer(text)]


# --- ffmpeg ------------------------------------------------------------------------------


def ffmpeg_exe() -> str:
    import imageio_ffmpeg  # type: ignore[import-untyped]

    return str(imageio_ffmpeg.get_ffmpeg_exe())


def run_ffmpeg(args: list[str]) -> str:
    """Run the bundled ffmpeg; return stderr; raise with its tail on failure."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {proc.stderr[-1500:]}")
    return proc.stderr


@dataclass(frozen=True)
class Probe:
    duration_s: float
    width: int
    height: int
    fps: float
    frames: int
    audio: str | None  # e.g. "aac 48000 Hz stereo"; None when no audio stream


def probe(path: Path) -> Probe:
    """Duration, size, frame rate, decoded frame count and the audio stream of a file."""
    proc = subprocess.run(
        [
            ffmpeg_exe(),
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    err = proc.stderr
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    video = re.search(r"Stream #0:\d+.*Video: .*?, (\d{2,5})x(\d{2,5}).*?, ([\d.]+) fps", err)
    audio = re.search(r"Stream #0:\d+.*Audio: (\w+).*?, (\d+) Hz, ([\w.]+)", err)
    frames = re.findall(r"frame=\s*(\d+)", err)
    if not dur or not video:
        raise RuntimeError(f"could not probe {path}: {err[-800:]}")
    h, m, s = dur.groups()
    return Probe(
        duration_s=int(h) * 3600 + int(m) * 60 + float(s),
        width=int(video.group(1)),
        height=int(video.group(2)),
        fps=float(video.group(3)),
        frames=int(frames[-1]) if frames else 0,
        audio=f"{audio.group(1)} {audio.group(2)} Hz {audio.group(3)}" if audio else None,
    )


def decode_audio(path: Path, rate: int = RATE) -> np.ndarray:
    """Mono float32 PCM in [-1, 1] at ``rate``."""
    proc = subprocess.run(
        [
            ffmpeg_exe(),
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(rate),
        ]
        + ["-f", "f32le", "-"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"decode failed: {proc.stderr.decode(errors='replace')[-800:]}")
    return np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32)


def write_wav(path: Path, pcm: np.ndarray, rate: int = RATE) -> Path:
    """16-bit mono WAV (stdlib only)."""
    import wave

    data = (np.clip(pcm, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)
    return path


def frame_at(path: Path, t_s: float, width: int | None = None) -> np.ndarray:
    """One decoded RGB frame (H, W, 3, uint8) at ``t_s``, optionally scaled to ``width``."""
    vf = f"scale={width}:-2:flags=area" if width else "null"
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-ss", f"{t_s:.3f}", "-i", str(path)]
        + ["-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"no frame at {t_s}: {proc.stderr.decode(errors='replace')[-600:]}")
    w = width or WIDTH
    h = len(proc.stdout) // (3 * w)
    return np.frombuffer(proc.stdout, dtype=np.uint8).reshape(h, w, 3)


# --- audio math (pure) ---------------------------------------------------------------------


def db_to_gain(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def rms_dbfs(pcm: np.ndarray) -> float:
    if pcm.size == 0:
        return -math.inf
    rms = float(np.sqrt(np.mean(np.square(pcm.astype(np.float64)))))
    return -math.inf if rms <= 0 else 20.0 * math.log10(rms)


def peak_normalise(pcm: np.ndarray, peak_dbfs: float) -> np.ndarray:
    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    if peak <= 0:
        return pcm.astype(np.float32)
    return (pcm * (db_to_gain(peak_dbfs) / peak)).astype(np.float32)


def fit_length(pcm: np.ndarray, n: int) -> np.ndarray:
    """Trim or zero-pad to exactly ``n`` samples."""
    if pcm.size >= n:
        return pcm[:n].astype(np.float32)
    return np.concatenate([pcm, np.zeros(n - pcm.size, dtype=np.float32)]).astype(np.float32)


def place(track: np.ndarray, clip: np.ndarray, at_s: float, rate: int = RATE) -> None:
    """Add ``clip`` into ``track`` starting at ``at_s`` (in place; clipped at the end)."""
    i = int(round(at_s * rate))
    if i >= track.size or i + clip.size <= 0:
        return
    lo = max(0, -i)
    j = max(0, i)
    n = min(clip.size - lo, track.size - j)
    track[j : j + n] += clip[lo : lo + n]


def find_cue(
    audio: np.ndarray,
    template: np.ndarray,
    expect_s: float,
    search_s: float = 0.25,
    rate: int = RATE,
) -> float:
    """Where ``template`` starts in ``audio`` near ``expect_s``.

    A matched filter restricted to the template's own band (bins within 20 dB of its
    spectral peak), so speech outside the cue's notes cannot pull the peak.
    """
    lo = max(0, int((expect_s - search_s) * rate))
    hi = min(audio.size, int((expect_s + search_s) * rate) + template.size)
    seg = audio[lo:hi].astype(np.float64)
    tpl = template.astype(np.float64)
    if seg.size < tpl.size:
        raise ValueError("search window shorter than the template")
    nfft = 1 << int(math.ceil(math.log2(seg.size + tpl.size)))
    spec_t = np.fft.rfft(tpl, nfft)
    mag = np.abs(spec_t)
    band = mag >= 0.1 * float(np.max(mag))
    corr = np.fft.irfft(np.fft.rfft(seg, nfft) * np.conj(spec_t) * band, nfft)
    valid = corr[: seg.size - tpl.size + 1]
    return (lo + int(np.argmax(valid))) / rate


def load_sfx(name: str, rate: int = RATE) -> np.ndarray:
    return decode_audio(SFX_DIR / f"{name}.wav", rate)


def sfx_track(cues: list[dict[str, Any]], total_s: float, rate: int = RATE) -> np.ndarray:
    """The SFX stem: each cue's wav at ``SFX_PEAK_DBFS`` peak, placed at its time."""
    track = np.zeros(int(round(total_s * rate)), dtype=np.float32)
    cache: dict[str, np.ndarray] = {}
    for cue in cues:
        name = str(cue["sound"])
        if name not in cache:
            cache[name] = peak_normalise(load_sfx(name, rate), SFX_PEAK_DBFS)
        place(track, cache[name], float(cue["t_s"]), rate)
    return track


def loudnorm(src: Path, dst: Path, lufs: float, rate: int = RATE) -> Path:
    """EBU R128 normalisation with ffmpeg's loudnorm (two passes, linear when it can)."""
    first = run_ffmpeg(
        [
            "-i",
            str(src),
            "-af",
            f"loudnorm=I={lufs}:TP=-1.5:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", first, re.S)
    if m:
        st = json.loads(m.group(0))
        filt = (
            f"loudnorm=I={lufs}:TP=-1.5:LRA=11:measured_I={st['input_i']}:measured_TP={st['input_tp']}"
            f":measured_LRA={st['input_lra']}:measured_thresh={st['input_thresh']}"
            f":offset={st['target_offset']}:linear=true"
        )
    else:
        filt = f"loudnorm=I={lufs}:TP=-1.5:LRA=11"
    run_ffmpeg(["-y", "-i", str(src), "-af", filt, "-ac", "1", "-ar", str(rate), str(dst)])
    return dst


def integrated_lufs(path: Path) -> float:
    err = run_ffmpeg(["-i", str(path), "-af", "ebur128", "-f", "null", "-"])
    found = re.findall(r"I:\s+(-?[\d.]+) LUFS", err)
    return float(found[-1]) if found else -math.inf


# --- frames (pure) -------------------------------------------------------------------------


def luminance(rgb: np.ndarray) -> np.ndarray:
    """WCAG relative luminance of sRGB pixels (uint8, ..., 3) -> float in [0, 1]."""
    c = rgb.astype(np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return np.asarray(0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2])


def caption_region(
    frame: np.ndarray, bar_frac: float = 0.16, text_frac: float = 0.68
) -> np.ndarray:
    """The caption text area: bottom ``bar_frac`` of the rows, left ``text_frac`` of the columns."""
    h, w = frame.shape[:2]
    return frame[int(h * (1 - bar_frac)) :, : int(w * text_frac)]


def caption_contrast(frame: np.ndarray) -> float:
    """WCAG contrast between the caption text (bright tail) and the bar (dark body)."""
    lum = luminance(caption_region(frame)).ravel()
    hi = float(np.percentile(lum, 99.5))
    lo = float(np.percentile(lum, 30))
    return (hi + 0.05) / (lo + 0.05)


def caption_changed(a: np.ndarray, b: np.ndarray, threshold: float = 4.0) -> bool:
    """Whether the caption area differs between two frames (mean absolute difference)."""
    ra, rb = caption_region(a).astype(np.int16), caption_region(b).astype(np.int16)
    return float(np.mean(np.abs(ra - rb))) > threshold


# --- timeline (written by the recorder) -----------------------------------------------------


def load_timeline(out_dir: Path = ARTIFACTS) -> dict[str, Any]:
    return dict(json.loads((out_dir / "demo-fallback.json").read_text(encoding="utf-8")))


def caption_offsets(timeline: dict[str, Any]) -> list[tuple[int, float, float]]:
    """(index, yaml mark, logged landing) per step from the presenter log."""
    return [(int(s["index"]), float(s["mark_s"]), float(s["logged_s"])) for s in timeline["steps"]]


# --- conversion ----------------------------------------------------------------------------


def encode_mp4(webm: Path, out: Path, offset_s: float, total_s: float, audio_wav: Path) -> Path:
    """webm -> H.264 yuv420p CRF 18, 30 fps, AAC, faststart, cut to [offset, offset+total]."""
    run_ffmpeg(
        ["-y", "-ss", f"{offset_s:.3f}", "-i", str(webm), "-i", str(audio_wav)]
        + ["-t", f"{total_s:.3f}", "-map", "0:v:0", "-map", "1:a:0"]
        + ["-vf", f"fps={FPS},scale={WIDTH}:{HEIGHT}:flags=bicubic,format=yuv420p"]
        + ["-c:v", "libx264", "-preset", "medium", "-crf", str(CRF), "-r", str(FPS)]
        + ["-c:a", "aac", "-b:a", "192k", "-ar", str(RATE), "-ac", "2"]
        + ["-movflags", "+faststart", str(out)]
    )
    return out


def mux_audio(video_mp4: Path, audio_wav: Path, out: Path, total_s: float) -> Path:
    run_ffmpeg(
        ["-y", "-i", str(video_mp4), "-i", str(audio_wav), "-t", f"{total_s:.3f}"]
        + ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy"]
        + [
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            str(RATE),
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    return out


def write_thumbnail_frame(mp4: Path, t_s: float, out_png: Path) -> Path:
    run_ffmpeg(["-y", "-ss", f"{t_s:.3f}", "-i", str(mp4), "-frames:v", "1", str(out_png)])
    return out_png


# --- voice-over draft ------------------------------------------------------------------------


def say_available() -> bool:
    return shutil.which("say") is not None


def vo_draft(out_dir: Path = ARTIFACTS, vo_script: Path = VO_SCRIPT_PATH) -> dict[str, Any]:
    """One macOS ``say -v Samantha -r 175`` per shot, placed at its mark -> vo-draft.m4a."""
    if not say_available():
        raise SystemExit("macOS `say` is not on PATH; the draft is optional (record your own take)")
    shots, total = load_shots()
    lines = parse_vo_script(vo_script.read_text(encoding="utf-8"))
    track = np.zeros(int(round(total * RATE)), dtype=np.float32)
    report = []
    with tempfile.TemporaryDirectory(prefix="wo062b-vo-") as tmp:
        for k, line in enumerate(lines):
            aiff = Path(tmp) / f"shot{k}.aiff"
            subprocess.run(
                ["say", "-v", "Samantha", "-r", "175", "-o", str(aiff), line.text], check=True
            )
            pcm = decode_audio(aiff)
            window_end = next(
                (s.end_s for s in shots if abs(s.start_s - line.start_s) < 0.01), total
            )
            spoken = pcm.size / RATE
            place(track, pcm, line.start_s + 0.3)
            report.append(
                {
                    "start_s": line.start_s,
                    "spoken_s": round(spoken, 2),
                    "window_s": round(window_end - line.start_s, 2),
                    "fits": line.start_s + 0.3 + spoken <= window_end,
                }
            )
        wav = write_wav(Path(tmp) / "vo.wav", track)
        out = out_dir / "vo-draft.m4a"
        run_ffmpeg(
            ["-y", "-i", str(wav), "-c:a", "aac", "-b:a", "128k", "-ar", str(RATE), str(out)]
        )
    (out_dir / "vo-draft.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return {"path": str(out), "shots": report}


# --- merge ----------------------------------------------------------------------------------


def merge(vo: Path, out_dir: Path = ARTIFACTS, music: Path | None = None) -> dict[str, Any]:
    """VO trimmed/padded to the timeline at -16 LUFS, SFX at -22 dBFS, optional music at -28."""
    timeline = load_timeline(out_dir)
    total = float(timeline["total_s"])
    n = int(round(total * RATE))
    with tempfile.TemporaryDirectory(prefix="wo062b-merge-") as tmp:
        t = Path(tmp)
        vo_fit = write_wav(t / "vo_fit.wav", fit_length(decode_audio(vo), n))
        vo_norm = loudnorm(vo_fit, t / "vo_norm.wav", VO_LUFS)
        mix = fit_length(decode_audio(vo_norm), n)
        mix += sfx_track(timeline["sfx"], total)
        if music is not None:
            looped = decode_audio(music)
            if looped.size:
                looped = np.tile(looped, int(math.ceil(n / looped.size)))
            bed = write_wav(t / "music_fit.wav", fit_length(looped, n))
            mix += fit_length(decode_audio(loudnorm(bed, t / "music_norm.wav", MUSIC_LUFS)), n)
        peak = float(np.max(np.abs(mix))) if mix.size else 0.0
        if peak > 0.98:  # never clip: scale the sum down
            mix = (mix * (0.98 / peak)).astype(np.float32)
        wav = write_wav(t / "mix.wav", mix)
        out = mux_audio(out_dir / "demo-fallback.mp4", wav, out_dir / "demo-youtube.mp4", total)
        lufs = integrated_lufs(vo_norm)
    return {"path": str(out), "vo_lufs": round(lufs, 1), "music": str(music) if music else None}


def check_merge(out_dir: Path = ARTIFACTS) -> dict[str, Any]:
    """VO in every shot window (RMS > -40 dBFS) and each SFX cue within 100 ms of its event."""
    timeline = load_timeline(out_dir)
    audio = decode_audio(out_dir / "demo-youtube.mp4")
    shots, _ = load_shots()
    windows = []
    for s in shots:
        seg = audio[int(s.start_s * RATE) : int(s.end_s * RATE)]
        windows.append({"shot": s.index, "start_s": s.start_s, "rms_dbfs": round(rms_dbfs(seg), 1)})
    cues = []
    for cue in timeline["sfx"]:
        tpl = peak_normalise(load_sfx(str(cue["sound"])), SFX_PEAK_DBFS)
        found = find_cue(audio, tpl, float(cue["t_s"]))
        cues.append({"sound": cue["sound"], "event_s": cue["t_s"], "found_s": round(found, 3)})
    return {"windows": windows, "cues": cues}


# --- CLI ------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m soundings.demo.video", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="record presenter mode, convert, thumbnail")
    rec.add_argument("--out", type=Path, default=ARTIFACTS)
    rec.add_argument("--executable-path", default=None)
    vod = sub.add_parser("vo-draft", help="macOS say draft of docs/vo-script.md")
    vod.add_argument("--out", type=Path, default=ARTIFACTS)
    mer = sub.add_parser("merge", help="VO + SFX (+ music) over demo-fallback.mp4")
    mer.add_argument("--vo", type=Path, required=True)
    mer.add_argument("--music", type=Path, default=None)
    mer.add_argument("--out", type=Path, default=ARTIFACTS)
    prb = sub.add_parser("probe", help="print duration, size, fps, frames, audio")
    prb.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    if args.cmd == "record":
        from soundings.demo.record import record_demo

        result = record_demo(args.out, executable_path=args.executable_path)
        print(json.dumps(result, indent=2))
    elif args.cmd == "vo-draft":
        print(json.dumps(vo_draft(args.out), indent=2))
    elif args.cmd == "merge":
        music = args.music if args.music and str(args.music) else None
        result = merge(args.vo, args.out, music)
        result["check"] = check_merge(args.out)
        print(json.dumps(result, indent=2))
    elif args.cmd == "probe":
        print(json.dumps(probe(args.path).__dict__, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
