# WO-062B — recording, voice-over prep, YouTube notes

Phase: 6/8 · Branch: `wo/062b-recording` · Worktree: `../soundings-worktrees/wo-062b-recording` · Est: 3 h · Owner: demo seat (agent); coordinator Kudrat
Depends on: WO-062 Part A (scheduler, `docs/demo-script.yaml`), WO-062P (presenter mode `/?presenter=1`, `web/public/sfx`, `web/src/sfx.ts`), WO-060 part B (run engine over runner v3), WO-061 (approval path, judge panel, QR, model badge), WO-085 (surge triage). Authority: `WO-062-ADDENDUM-video-sound.md` Directive 1 items 2-5, Directive 2 items 3-5, the coordinator notes and the WO-088 addendum.

## Goal
`make demo-video` records the presenter-mode run headless and offline into `artifacts/demo-fallback.mp4` (captions baked in, SFX on the audio track) and `artifacts/thumbnail.png`; the voice-over is prepared (`docs/vo-script.md`, a `say` draft `artifacts/vo-draft.m4a`) and `make video-merge VO=take.m4a` produces `artifacts/demo-youtube.mp4`; `docs/youtube.md` holds the upload text. The final render is due 20:00 PDT.

## Scope
In:
- `soundings/demo/record.py` (`record_demo(out_dir, executable_path=None)`): uvicorn on a free loopback port serving the API and `web/dist`; cached `chromium_headless_shell` (never downloaded); 1920×1080, deviceScaleFactor 1, Playwright video on; every non-loopback request aborted (`tiles=off`); MODEL_MODE must be `replay`. The timeline origin is the presenter log's step-0 entry; one Space per later step at its yaml mark; a `pause_for_click` step gets one click 2 s after it lands (the judge panel's Close when open, else the caption). After `end` (+1.5 s) it reads `window.__soundingsSfx` (the sound module's log of the audit events) and the presenter log, cuts the webm at the origin, and writes `demo-fallback.mp4`, `thumbnail.png` and `demo-fallback.json` (step landings, clicks, SFX cues, console lines, offsets).
- `soundings/demo/video.py`: shot list windows, vo-script parser and word budget, vendor-name check, bundled ffmpeg (`imageio-ffmpeg`) for probe / decode / encode (H.264 yuv420p CRF 18, 30 fps, AAC 48 kHz, faststart) / loudnorm, the SFX stem (each cue at −22 dBFS peak), the VO draft (`say -v Samantha -r 175`, one call per shot, placed 0.3 s after its mark), the merge (VO trimmed/padded to the timeline, two-pass loudnorm to −16 LUFS, SFX −22 dBFS at the recorded event times, music −28 LUFS only with `MUSIC=`), the merge check (RMS per shot window, SFX cue alignment by normalised cross-correlation), caption contrast at phone width. CLI `python -m soundings.demo.video record|vo-draft|merge|probe`.
- `Makefile`: `demo-video` (record + convert + thumbnail, then the draft VO and its merge when `say` exists) and `video-merge VO=… [MUSIC=…]`, one marked block.
- `docs/vo-script.md`, `docs/youtube.md`, this file, `docs/STATE.md` (tech-check lines and the `| 062B |` row), one DECISIONS row.
- `docs/demo-script.yaml`: Harbor Night `speed: 20x` and its subcaption corrected (see Deviations); `tests/test_demo_script.py` and `tests/test_demo_responder_beats.py` follow those two values only.
- `pyproject.toml`/`uv.lock`: `imageio-ffmpeg` dev dependency (approved 2026-10-16 23:05 and CLAUDE.md §8 A6, never added to the lock until now).

Out: the presenter UI, the SFX files, caption wording other than the corrected subcaption, the owner's VO take, the unlisted upload (owner step), any model call.

## Exit tests (`tests/test_demo_video.py`; MODEL_MODE=replay, no network)
- [ ] `SOUNDINGS_E2E=1 make demo-video` writes `artifacts/demo-fallback.mp4` and `artifacts/thumbnail.png`; the mp4 is 170–180 s (the yaml timeline is 3:00 = 180 s), 1920×1080, 30 fps, with an AAC audio stream (`test_video_format`).
- [ ] Two recordings have the same frame count (±1) and the same caption landings (within 0.25 s of each other) (`test_two_takes_match`).
- [ ] Caption check per shot boundary: tesseract is not installed, so the presenter log landings match the yaml marks within 0.5 s, and the caption area of a frame 1.2 s into each shot differs from the previous shot's (`test_captions_land_on_the_yaml_marks`).
- [ ] vo-script: one line per shot at its mark, ≤ 28 words per 10 s, "simulated" exactly once and in the evidence shot, WO-088 lines present; denylist clean and no model or vendor names over `docs/demo-script.yaml`, `docs/vo-script.md`, `docs/youtube.md`; `bash scripts/denylist-check.sh` exits 0.
- [ ] `make video-merge VO=artifacts/vo-draft.m4a` → `artifacts/demo-youtube.mp4` with RMS > −40 dBFS in every shot window and each SFX cue within 100 ms of its recorded event time (`test_merge_vo_and_sfx`); the fallback keeps the SFX with no VO (`test_fallback_keeps_the_sfx`).
- [ ] Phone size: each shot's frame scaled to 390 px wide keeps a caption contrast ≥ 7:1 (`test_captions_read_at_phone_size`).
- [ ] `make check` green; the pure tests run in `tests-core` (the recording tests skip without `SOUNDINGS_E2E=1`, so no HEAVY list change).

## Model calls
None. Paid run needed? No.

## Rungs touched
None. The recording presses Space and clicks the caption or the judge panel's Close; it never approves or sets a rung (the S05 handoff is the run's simulated approver, as on stage).

## Hard rules exercised
§2.1/§2.5 (denylist over the three texts), §2.3 (record refuses MODEL_MODE other than replay; no network), §2.4 (two takes compared), §2.6 (the VO describes what the screen shows; "simulated" stays in the evidence line).

## Done
PR: · Merged commit: · Test summary: · STATE.md updated:
