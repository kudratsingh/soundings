# WO-062 — demo-runner

Phase: 6 · Branch: `wo/062-demo-runner` · Worktree: `../soundings-worktrees/wo-062-demo-runner` · Est: 4 h · Owner: frontend / demo (unassigned); coordinator Kudrat
Depends on: WO-010 (`ToggleName`, `LadderState`, `assert_clean`), WO-051 (eval report and CLI), WO-060 (API, run engine, event schema, `python -m soundings.stream`), WO-061 (`/api/inject`, the approve endpoint, the stepped source). Wave 10, the last merge.

## Goal
`make demo` replays the pitch sequence in `docs/demo-script.yaml` end to end, headless, with `MODEL_MODE=replay` and no network. It starts the API on loopback, runs each step's scenario through the real run engine, acts as the scripted watch officer on the approval request, prints each `say` line at its time mark with the state reached, and ends on the eval table. A `--record` mode drives the real UI in Playwright chromium with video recording. This produces `artifacts/demo-fallback.webm`, which is converted to `artifacts/demo-fallback.mp4` (≤ 3 min) when ffmpeg is available. That video is the fallback if the live demo fails on stage, and the Devpost upload.

Parallel start: the script parser, the step model and the transcript writer need nothing beyond WO-000, so they can be built while WO-060/061 land. Wiring to `/api/runs` and `/api/inject` follows the WO-060 Interfaces contract.

## Scope
In:
- `soundings/demo/script.py`. `DemoStep` (`extra="forbid"`) has these fields:
  - `t: "m:ss"`
  - `scenario: S\d\d | "live" | "twist"`
  - `eval: bool`
  - `say: str` (a FreeText that passes the denylist)
  - optional `seed`, `scenario_ref` (the catalog id a `twist` step runs), `toggles: list[ToggleName]`, `continue_run: bool` (keep the previous step's run, e.g. the second S05 step)
  - `approve: bool` (approve the first pending request)
  - `inject: InjectRequest` (for `live`)
  - `expect_state: LadderState`

  `DemoScript.load(path)` validates that the t marks are strictly increasing, that a `live` step has `inject`, and that a `twist` step has `toggles`.
- `docs/demo-script.yaml` edits that add only the new optional keys:
  - the second S05 step gets `continue_run: true, approve: true`
  - `live` gets a fixed closing request near `fuel_berth_2` and a fixed seed
  - `twist` gets `scenario_ref: S05, toggles: [network_down]` until Twist 1 says otherwise (Phase 7 may edit this step)
  - each step gets `expect_state`

  The `say` lines stay verbatim.
- `soundings/demo/runner.py`, `DemoRunner`:
  - It starts uvicorn on `127.0.0.1:<free port>` in a thread.
  - For each step it calls POST /api/runs or POST /api/inject, follows WS /ws/runs/{id}, and posts /approve when `approve` and an `approval_request` arrives. A step that has `expect_state` fails if that state is not reached before the next step's mark.
  - It writes a transcript line per step: `[m:ss] <scenario> seed=<n> -> <state> tick=<n> | say: <line>`. The transcript holds sim data only and no wall times.
  - It prints elapsed wall time to stderr.
- The eval step prints the rung histogram, missed threats, false shutdowns and interruptions per policy from `artifacts/eval/report.json` when present. Otherwise it runs `python -m soundings.eval --suite catalog --seeds 1 --mode replay --out artifacts/demo/eval` first.
- `soundings/demo/__main__.py` (typer CLI, invoked exactly as the Makefile does):
  - `--script PATH --mode replay`
  - `--pace realtime|fast|video` (default `realtime`). realtime honours the t marks; fast never sleeps (speed 0) and is used by tests; video scales t marks by `video_scale` so the total is ≤ 170 s.
  - `--transcript PATH`
  - `--record PATH.webm`
  - `--mode live|record` exits 2 with "paid runs need `approve paid run` (docs/WORKFLOW.md §7)" and makes no call.
- `soundings/demo/record.py`, which is imported only when `--record` is set and uses Python Playwright:
  - It opens the UI at 1280×720 with `record_video_dir`.
  - Its follow mode opens `/?follow=1` (see the web change below).
  - It overlays each `say` line as a caption via `page.evaluate`, so no UI change is needed.
  - It clicks `[data-testid=approve]` instead of POSTing.
  - It copies the video to the `--record` path.
- Web, small: `web/src/useFollow.ts` (new) plus one marked block in `App.tsx` (shared with WO-060/061): `?follow=1` polls GET /api/runs every 1 s and switches to the newest run.
- `scripts/webm-to-mp4.sh` runs `command -v ffmpeg` and then `ffmpeg -y -i in.webm -c:v libx264 -pix_fmt yuv420p -movflags +faststart out.mp4`. If ffmpeg is absent, it prints the owner step and exits 3, leaving the webm in place.
- Makefile (shared; this WO adds only the `demo-video` target): `demo-video:` runs the record pace, then the convert script. `demo` itself stays as it is.
- `artifacts/demo-fallback.json` holds the recorded timeline (`total_s`, per-step offsets), so the duration can be checked without ffprobe.

Video capture runbook (owner or frontend seat, Saturday after Phase 6 is green):
1. `make setup && pnpm -C web build && make eval`, so the eval step prints from a fresh report.
2. `uv run playwright install chromium` (one-time; skip if present).
3. `make demo-video`. Tiles load if the network is up; otherwise the UI falls back to `?tiles=off` (geojson only), which is acceptable for the fallback video.
4. If the convert script exits 3, install ffmpeg after owner approval and rerun `bash scripts/webm-to-mp4.sh artifacts/demo-fallback.webm artifacts/demo-fallback.mp4`, or upload the webm.
5. Watch it once, record the duration and commit hash in `docs/STATE.md`, and keep the file in `artifacts/`, which is gitignored and uploaded by hand in Phase 8.

Out: narration audio, editing the video, live model calls, a separate demo UI, changing the say lines (owner and deck seat), and the Devpost upload (Phase 8).

## Spec references
`docs/BUILD-PLAN.md` Phase 6 and Phase 8 (video ≤ 3 min); `docs/demo-script.yaml`; `docs/DECK-INPUTS.md` (pitch order: show it working first, then the twist); `docs/WORKFLOW.md §7` (paid runs), `§8` (determinism); `CLAUDE.md §2.3, §5`.

## Files expected
- `soundings/demo/{__init__,__main__,script,runner,record}.py`
- `docs/demo-script.yaml` (additive keys only), `scripts/webm-to-mp4.sh`, `Makefile` (`demo-video` target only), `web/src/useFollow.ts`, `web/src/App.tsx` (one marked block)
- `tests/test_demo_script.py`, `tests/test_demo_runner.py`, `tests/e2e/test_demo_record.py`
- `docs/STATE.md`

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
- [ ] (BUILD-PLAN, verbatim) `make demo` runs the five-minute sequence end to end with `MODEL_MODE=replay` and no network; a recorded screen capture is saved to `artifacts/demo-fallback.mp4`.
  - Concretely, `time make demo` exits 0 in ≤ 5 min 30 s wall and prints all 7 say lines in order, each with its reached state.
  - `make demo-video` then writes `artifacts/demo-fallback.webm` and, when ffmpeg is present, `artifacts/demo-fallback.mp4`.
- [ ] Supporting check for Phase 8, which owns the upload (not a Phase 6 exit test): `artifacts/demo-fallback.mp4` ≤ 3 min. Concretely, `ffprobe -v error -show_entries format=duration -of csv=p=0 artifacts/demo-fallback.mp4` prints ≤ 180. Without ffprobe, `uv run python -c "import json;assert json.load(open('artifacts/demo-fallback.json'))['total_s']<=180"` exits 0.
- [ ] `uv run pytest tests/test_demo_script.py` checks the following. The real `docs/demo-script.yaml` loads. t marks parse and are strictly increasing. Unknown keys, a `live` step without `inject`, a `twist` step without toggles, an unknown scenario id, and a denylisted word in `say` are each rejected. This proves the script is a validated input, not free text.
- [ ] `uv run pytest tests/test_demo_runner.py::test_demo_fast_offline` is an integration test over the whole real script at `--pace fast`. It replaces `socket.socket.connect` with a guard that raises on any non-loopback address. The transcript must show the following:
  - S01 → CLEARED_AT_ASK
  - S02 → LOST_VEHICLE
  - S05 → HOLDING, then after the scripted approval HANDED_OFF
  - live → HOLDING or AWAITING_APPROVAL
  - twist S05+network_down → HANDED_OFF
  - eval → a table with three policy rows

  This proves the pitch sequence runs on real catalog scenarios with no network.
- [ ] Determinism: `uv run python -m soundings.demo --script docs/demo-script.yaml --mode replay --pace fast --transcript /tmp/a.txt; uv run python -m soundings.demo --script docs/demo-script.yaml --mode replay --pace fast --transcript /tmp/b.txt; diff /tmp/a.txt /tmp/b.txt` prints nothing, which proves the same script gives the same states and ticks.
- [ ] Rule proof §2.3: `tests/test_demo_runner.py::test_live_mode_refused` checks that `--mode live` and `--mode record` exit 2 before any `ModelClient` is constructed (a spy on `ModelClient.__init__` counts 0), and that the Makefile line still passes `--mode replay`.
- [ ] Rule proof §2.1: `grep -iEf config/denylist.txt docs/demo-script.yaml soundings/demo/*.py` prints nothing, and the `say` validator in `test_demo_script.py` rejects a denylisted line. Neither the narration nor the captions can carry kinetic vocabulary.
- [ ] `make e2e` runs `tests/e2e/test_demo_record.py`. It skips without chromium, and fails instead under `SOUNDINGS_E2E_REQUIRED=1`. It records the first two steps at `--pace video` into a tmp webm whose size is > 0 bytes, and checks that `?follow=1` switched runs. This proves the recorder works before the full capture.
- [ ] `bash scripts/webm-to-mp4.sh` exits 3 with the owner-step message when `PATH` lacks ffmpeg (test: `PATH=/usr/bin:/bin bash scripts/webm-to-mp4.sh in.webm out.mp4; test $? -eq 3`).
- [ ] `pnpm -C web exec tsc --noEmit && pnpm -C web build` exits 0; `make check` green.
- [ ] Manual: the owner watches `artifacts/demo-fallback.mp4` (or the webm) once end to end and notes the duration in `docs/STATE.md`.

## Interfaces
Exposed by WO-062:
- `soundings.demo.script.DemoScript`, `soundings.demo.script.DemoStep`, `soundings.demo.script.parse_mark`
- `soundings.demo.runner.DemoRunner` (`run(pace) -> list[TranscriptLine]`), `soundings.demo.runner.TranscriptLine`
- `soundings.demo.record.record_demo(script, out_webm, video_scale) -> Path`
- CLI `python -m soundings.demo --script PATH --mode replay [--pace realtime|fast|video] [--transcript PATH] [--record PATH]`
- `make demo` (unchanged), `make demo-video`, `scripts/webm-to-mp4.sh`
- Web: the `?follow=1` URL param
- Files: `artifacts/demo-fallback.webm`, `artifacts/demo-fallback.mp4`, `artifacts/demo-fallback.json`

Consumed:
- WO-000 (merged): `soundings.api.create_app`.
- WO-060: POST /api/runs (`source="stepped"`), GET /api/runs, WS /ws/runs/{run_id}, `soundings.stream.events.StreamEvent`, `data-testid=approve`.
- WO-061: POST /api/inject, POST /api/runs/{run_id}/approve, `soundings.inject.InjectRequest`.
- WO-051: `artifacts/eval/report.json` and CLI `python -m soundings.eval`.
- WO-010: `ToggleName`, `LadderState`, `soundings.schemas.denylist.assert_clean`.

## Cut line
Drop these, in order: the captions overlay in the video (narrate over it instead), the `video_scale` timing (trim the webm by hand), and the eval step running eval itself (print a "run make eval first" line instead). Never drop an exit test, the offline guard, or the ≤ 3 min check.

## Model calls
None (`use_llm=False` everywhere in the demo). Paid run needed? No.
Dependency adds needing owner approval (CLAUDE.md section 3): the system `ffmpeg` binary. It is not on the coordinator laptop (`command -v ffmpeg` was empty on 2026-10-16); Playwright's bundled ffmpeg only records webm. Owner step: `brew install ffmpeg`, or upload the webm. One-time `uv run playwright install chromium` for recording (already present locally).

## Rungs touched
None. The demo replays runs; the only action it takes is the scripted approval, through the same endpoint and guard as the button.

## Hard rules exercised
§2.1 (denylist grep over the script and the captions), §2.3 (`test_live_mode_refused`, offline socket guard), §2.4 (transcript diff).

## Done
PR: · Merged commit: · Test summary: · STATE.md updated:

## Coordinator addenda (2026-10-16 23:05 PT, binding; see docs/log/DECISIONS.md)
- Approved dev dependency: `imageio-ffmpeg` (bundled ffmpeg binary) for the webm→mp4 conversion; no system ffmpeg install. If conversion fails, keep the webm and flag it.
- Video: squeeze all demo-script steps to ≤ 170 s including a short eval card; do not drop the eval step.
