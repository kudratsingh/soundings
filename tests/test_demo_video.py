"""WO-062B exit tests: the demo video, the voice-over script and the upload text.

The pure parts (narration budget, vocabulary, chapters, audio placement and cue finding,
caption contrast) always run. The recording runs headless Chromium for two full takes
(about 7 minutes) and is opt-in like the other browser smokes (CLAUDE.md section 8 A7):
``SOUNDINGS_E2E=1``. The merge check also needs macOS ``say``.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from soundings.demo import video as v
from soundings.schemas.denylist import find_denylisted

REPO = Path(__file__).resolve().parents[1]
DOCS = [
    REPO / "docs" / "demo-script.yaml",
    REPO / "docs" / "vo-script.md",
    REPO / "docs" / "youtube.md",
]
SHOTS, TOTAL = v.load_shots()


# --- narration -------------------------------------------------------------------------------


def _vo_lines() -> list[v.VoLine]:
    return v.parse_vo_script(v.VO_SCRIPT_PATH.read_text(encoding="utf-8"))


def test_vo_script_has_one_line_per_shot_at_its_mark() -> None:
    lines = _vo_lines()
    assert [line.start_s for line in lines] == [s.start_s for s in SHOTS]


@pytest.mark.parametrize("shot", SHOTS, ids=lambda s: v.fmt_mark(s.start_s))
def test_vo_word_budget(shot: v.Shot) -> None:
    line = next(x for x in _vo_lines() if x.start_s == shot.start_s)
    assert line.words <= v.word_budget(shot.duration_s), (
        line.words,
        v.word_budget(shot.duration_s),
    )


def test_simulated_said_once_in_the_evidence_shot() -> None:
    text = v.VO_SCRIPT_PATH.read_text(encoding="utf-8")
    assert len(re.findall(r"\bsimulated\b", text, re.I)) == 1
    evidence = next(x for x in _vo_lines() if x.start_s == 135.0)
    assert re.search(r"\bsimulated\b", evidence.text, re.I)


def test_vo_carries_the_wo088_lines() -> None:
    text = v.VO_SCRIPT_PATH.read_text(encoding="utf-8")
    glider = "The glider went quiet. The platform searched the drift box, and it is found in the"
    assert f"{glider} drift box." in text
    night = next(x for x in _vo_lines() if x.start_s == 60.0)
    assert "never closer than 25 m" in night.text and "The call is a person's." in night.text


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_no_denylisted_words_or_vendor_names(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not find_denylisted(text)
    assert not v.vendor_names(text)


def test_denylist_script_passes() -> None:
    proc = subprocess.run(
        ["bash", "scripts/denylist-check.sh"], cwd=REPO, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout


def test_vendor_names_are_caught() -> None:
    assert v.vendor_names("written by Claude, an LLM") == ["Claude", "LLM"]
    assert not v.vendor_names("the platform goes to look")


# --- upload text -----------------------------------------------------------------------------


def _section(text: str, name: str) -> str:
    m = re.search(rf"^## {name}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert m, name
    return m.group(1).strip()


def test_youtube_title_description_chapters_link() -> None:
    text = v.YOUTUBE_PATH.read_text(encoding="utf-8")
    title = _section(text, "Title")
    assert "\n" not in title and 0 < len(title) <= 60
    paragraphs = [p for p in _section(text, "Description").split("\n\n") if p.strip()]
    assert len(paragraphs) == 2
    chapters = re.findall(r"^(\d+:\d{2}) \S", _section(text, "Chapters"), re.M)
    assert chapters == [v.fmt_mark(s.start_s) for s in SHOTS]
    assert "https://github.com/kudratsingh/soundings" in text


def test_make_targets_exist_and_stay_on_replay() -> None:
    make = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "export MODEL_MODE ?= replay" in make
    assert re.search(r"^demo-video:", make, re.M) and re.search(r"^video-merge:", make, re.M)
    assert "soundings.demo.video record" in make and "soundings.demo.video merge" in make


def test_record_refuses_a_live_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from soundings.demo.record import record_demo

    monkeypatch.setenv("MODEL_MODE", "live")
    with pytest.raises(SystemExit, match="replay only"):
        record_demo(tmp_path)


# --- audio and frame helpers (pure) --------------------------------------------------------


def test_fit_length_and_rms() -> None:
    x = np.ones(10, dtype=np.float32)
    assert v.fit_length(x, 4).size == 4 and v.fit_length(x, 16)[10:].sum() == 0
    assert v.rms_dbfs(np.full(100, 0.5, dtype=np.float32)) == pytest.approx(-6.02, abs=0.01)
    assert v.rms_dbfs(np.zeros(5, dtype=np.float32)) == -math.inf


def test_sfx_cues_land_at_level_and_are_found_under_speech() -> None:
    rate = v.RATE
    cues = [{"t_s": 1.234, "sound": "hail_ping"}, {"t_s": 3.5, "sound": "hold_tone"}]
    track = v.sfx_track(cues, 6.0)
    peak_db = 20 * math.log10(float(np.max(np.abs(track))))
    assert peak_db == pytest.approx(v.SFX_PEAK_DBFS, abs=0.1)
    rng = np.random.default_rng(7)
    t = np.arange(track.size) / rate
    speech = (0.3 * np.sin(2 * np.pi * 180 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))).astype(
        np.float32
    )
    mix = track + speech + rng.normal(0, 0.01, track.size).astype(np.float32)
    for cue in cues:
        tpl = v.peak_normalise(v.load_sfx(cue["sound"]), v.SFX_PEAK_DBFS)
        # a steady tone under a louder steady tone can lock one period off (hold_tone: ~20 ms);
        # the exit tolerance is 100 ms
        assert abs(v.find_cue(mix, tpl, cue["t_s"] + 0.05) - cue["t_s"]) < 0.03


def test_caption_contrast_and_change() -> None:
    frame = np.full((219, 390, 3), 10, dtype=np.uint8)
    frame[-20:-10, 10:120] = 255  # white caption strokes on the dark bar
    assert v.caption_contrast(frame) > 7.0
    other = frame.copy()
    other[-20:-10, 10:120] = 10
    other[-20:-10, 140:250] = 255
    assert v.caption_changed(frame, other)
    assert not v.caption_changed(frame, frame.copy())


# --- the recording (opt-in) ----------------------------------------------------------------


def _e2e() -> None:
    if os.environ.get("SOUNDINGS_E2E") != "1" and os.environ.get("SOUNDINGS_E2E_REQUIRED") != "1":
        pytest.skip("the recording is opt-in: set SOUNDINGS_E2E=1 (two takes, about 7 minutes)")
    from soundings.demo.record import WEB_DIST, find_chromium

    if not (WEB_DIST / "index.html").is_file():
        pytest.skip("web/dist is not built (make web)")
    if find_chromium() is None:
        pytest.skip("no cached chromium")


@pytest.fixture(scope="module")
def takes(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[Path, dict[str, Any]]]:
    _e2e()
    from soundings.demo.record import record_demo

    out = []
    for name in ("a", "b"):
        d = tmp_path_factory.mktemp(f"wo062b-take-{name}")
        out.append((d, record_demo(d)))
    return out


def test_video_format(takes: list[tuple[Path, dict[str, Any]]]) -> None:
    d, _ = takes[0]
    p = v.probe(d / "demo-fallback.mp4")
    assert 170.0 <= p.duration_s <= 180.5, p
    assert (p.width, p.height) == (1920, 1080)
    assert p.fps == pytest.approx(30.0)
    assert p.audio is not None and p.audio.startswith("aac")
    head = (d / "thumbnail.png").read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    assert (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")) == (1280, 720)


def test_two_takes_match(takes: list[tuple[Path, dict[str, Any]]]) -> None:
    (da, ta), (db, tb) = takes
    fa, fb = v.probe(da / "demo-fallback.mp4").frames, v.probe(db / "demo-fallback.mp4").frames
    assert abs(fa - fb) <= 1, (fa, fb)
    ia = [(s["index"], s["mark_s"]) for s in ta["steps"]]
    assert ia == [(s["index"], s["mark_s"]) for s in tb["steps"]]
    for sa, sb in zip(ta["steps"], tb["steps"], strict=True):
        assert abs(sa["logged_s"] - sb["logged_s"]) <= 0.25, (sa, sb)
        assert sa["caption"] == sb["caption"]


def test_captions_land_on_the_yaml_marks(takes: list[tuple[Path, dict[str, Any]]]) -> None:
    for d, timeline in takes:
        steps = timeline["steps"]
        assert [s["index"] for s in steps] == list(range(len(SHOTS)))
        for s in steps:
            assert abs(s["logged_s"] - s["mark_s"]) <= v.CAPTION_TOL_S, s
        mp4 = d / "demo-fallback.mp4"
        if shutil.which("tesseract"):
            pytest.skip("tesseract present: OCR path not wired on this machine")  # pragma: no cover
        frames = [v.frame_at(mp4, shot.start_s + 1.2) for shot in SHOTS]
        for prev, cur in zip(frames, frames[1:], strict=False):
            assert v.caption_changed(prev, cur)


def test_captions_read_at_phone_size(takes: list[tuple[Path, dict[str, Any]]]) -> None:
    d, _ = takes[0]
    for shot in SHOTS:
        frame = v.frame_at(d / "demo-fallback.mp4", shot.start_s + 2.0, width=390)
        assert frame.shape[1] == 390
        assert v.caption_contrast(frame) >= 7.0, shot


def test_fallback_keeps_the_sfx(takes: list[tuple[Path, dict[str, Any]]]) -> None:
    d, timeline = takes[0]
    assert timeline["sfx"], "the run fired no sound event"
    audio = v.decode_audio(d / "demo-fallback.mp4")
    for cue in timeline["sfx"]:
        tpl = v.peak_normalise(v.load_sfx(cue["sound"]), v.SFX_PEAK_DBFS)
        assert abs(v.find_cue(audio, tpl, cue["t_s"]) - cue["t_s"]) <= v.SFX_TOL_S, cue


def test_merge_vo_and_sfx(takes: list[tuple[Path, dict[str, Any]]]) -> None:
    if not v.say_available():
        pytest.skip("macOS say is not on PATH")
    d, _ = takes[0]
    v.vo_draft(d)
    v.merge(d / "vo-draft.m4a", d)
    report = v.check_merge(d)
    for w in report["windows"]:
        assert w["rms_dbfs"] > v.VO_MIN_RMS_DBFS, w
    for cue in report["cues"]:
        assert abs(cue["found_s"] - cue["event_s"]) <= v.SFX_TOL_S, cue
    p = v.probe(d / "demo-youtube.mp4")
    assert p.audio is not None and abs(p.duration_s - TOTAL) < 0.5
