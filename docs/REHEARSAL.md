# Rehearsal: the watch-officer screen, click by click

Rehearsed 2026-10-17 17:40–18:10 PDT on `fix/demo-rehearsal` (main at 608c852 plus this
branch's fixes), headless Chromium at 1440×900 and 1920×1080, `tiles=off`, `MODEL_MODE=replay`,
zero paid calls. Times are **wall seconds after the page loads, at 20×** (sim time = tick × 2 s;
at 20× one tick is 0.1 s). Expect ±3 s on a loaded laptop. The URL starts its run once per
page load; the **Reset** button starts the same scenario again.

Before you start: one tab only. The server holds four runs; a fifth start (another tab, the
presenter, a judge run) drops the oldest one, and an approval on a dropped run answers
`HTTP 404` on the card.

## 1. Harbor Night (S05, seed 11): the one to present

Use the stepped source, so a person approves:

`/?scenario=S05&seed=11&speed=20&tiles=off&source=stepped`

| at | what appears | where to point |
|---|---|---|
| ~3 s | contacts on the radar; CASES fills; ledger counts start | map, CASES |
| ~8 s | c-0002 handed to a person (budget stop clause); **SITREP (1)** on the top bar | top bar SITREP button |
| ~12 s | c-0002 re-acquired as c-0004: **advisory** (rung *slow*, guard `slow_permitted: permit`) | NOW line, ladder |
| ~20 s | **hold placed** on fuel_berth_1 under standing orders (rung *hold*) | ladder, KEY MOMENTS |
| ~65 s | **Approval requested · c-0004 · Keep the hold on fuel_berth_1 longer (up to 15 min)**; the case jumps to the top of CASES and the NOW line names it; pacing drops to 1× while it waits | approval card (bottom of the right rail) |
| click | **Approve** → "answer sent · the ladder's guard checks it on the next tick" | approval card |
| +2–6 s | KEY MOMENTS: `approved`, `handed off`; c-0002 shows WATCH AFTER HANDOFF | KEY MOMENTS |
| any time | **SITREP** opens the drawer over the map (it no longer covers the approval card); every fact line ends in `[evidence: <row id>]`; Esc closes it | drawer |

The request expires 120 sim-seconds after it opens (the card counts down; 2 minutes of wall
time at 1×), so there is time to talk, but click before the countdown ends: unanswered, it
pages the backup approver (`backup paged` moment) and a second request opens, which is the
one to **Decline** if you want to show a person's no. With a prompt Approve, this seed has
only the one request.

Without `&source=stepped` the card says "simulated approver answers on this run" and the
simulated approver grants it after 60 sim-seconds, which is a full minute of 1× pacing on
stage: present the stepped URL.

What to say when a checkpoint is late: "It is investigating; the planner picks the next
probe from an allowlist, and nothing moves until the evidence does." Recovery: **Reset**
(same scenario and seed; the picker follows the run on screen), or reload the URL. If the
approval card never shows by ~90 s, reload; do not switch seed (seed 11 is the rehearsed one).

## 2. Silent glider (S02, seed 3)

`/?scenario=S02&seed=3&speed=20&tiles=off`

| at | what appears |
|---|---|
| ~3 s | c-0001 (the filed glider) registry exact match, hail answered: **cleared** at *ask* |
| ~8 s | c-0002 (unfiled) handed to a person; the NOW line moves to it |
| ~10 s | c-0001 re-acquired (CASES: "re-acquired ×n") |
| ~60 s | c-0001's lineage goes silent: **lost vehicle** moment; SITREP drawer shows a `LOST_VEHICLE notice` to the plan's operator with a Search box section (an operator notice, not an interruption) |
| ~60 s | KEY MOMENTS: **responder dispatched to look** (resp_1 leaves the dock toward the lineage) |
| ~70 s | **responder returning to dock**; a second LOST_VEHICLE notice |

Point at the case: the lost glider is a benign case, so the screen auto-selects the latest
handed-off case. **Click c-0001 in CASES** before narrating the glider. There is no "found in
the drift box" notice in this build (see Left for the owner).

## 3. Judge panel on a quiet day (S01, seed 2)

`/?scenario=S01&seed=2&speed=20&tiles=off&judge=1` (the Judge tab shows only with `judge=1`).

1. **Judge** tab → panel opens with the QR code and the registry URL; on loopback it says
   "A phone cannot reach localhost; open the screen on the LAN address". The `demo network
   mode` banner shows only when the server runs on a LAN address.
2. **Place contact**, click the water south of the fuel berth, pick **Closing on an asset**,
   wait for the choice to read back (~2 s), **Go**.
3. Within 0.3 s the screen follows an `L-…` run; the contact is on the map; *ask* by ~2 s;
   **advisory (slow)** by ~3 s.
4. Open the QR URL (`/registry`) in a phone or a second tab and file a plan: the plan marker
   appears within 2 s; a conflicted plan opens the proposal card (Accept / Decline; nothing
   changes until the operator accepts).

## 4. Presenter (`/?presenter=1&tiles=off`)

Re-rehearsed on main after #78 (presenter on runner v3) and #80 (WO-062C) merged. Space
advances (7 steps), ArrowLeft goes back, **reset to step 0** restores step 0 in ~1.3 s. Each
step lands within ~6–7 s; the twist step (5/7) starts its own S05 run with the toggles (no
400 any more); the caption bar no longer covers the approval card, the ladder or the ledger
at 1920×1080; no console errors. The scenario picker follows each step's run, so the top-bar
**Reset** restarts what is on screen.

The Busy Tuesday step (3/7) opens the judge panel over the bottom-left of the map and it stays
open into Harbor Night: click its **Close** when step 4/7 lands (the recorder does the same).

## 5. Controls

- **Reset** restarts the scenario on screen (~1 s); old cases clear; one run per click.
- Speed select + **Reset** applies the speed (top bar shows `50×`).
- **About** → focus lands on Close; Physics tab shows 5 cards; Esc closes.
- **Legend** toggles; **follow selected** toggles (`aria-pressed`).
- No console errors or page errors in any rehearsal above; layout shift over 30 s of
  streaming: CLS 0.005 (1440×900), 0.003 (1920×1080); no horizontal scroll at either size.

## Left for the owner (not changed here: ladder, sim, catalog, captions, config)

- S05/S02/S01 on the UI source (runner v3 with the responder) hand off several low-threat
  contacts through the budget stop clause (`C_BUDGET`, threat mass 0.00–0.09): S05 has 5
  handoffs (catalog `max_human_interruptions: 1`), S02 has 4 (catalog 0), the "quiet day"
  S01 has 4. Runs continue to tick 1199/1799, past the catalog horizon.
- S05 seed 11 never dispatches the responder; S02 dispatches it once (tick 599) and has no
  "found in the drift box" notice; LOST_VEHICLE fires three times on re-acquired ids.
- `awaiting_approval_speed: 1.0` makes the simulated approver's 60 s wait a minute of
  stillness on the default source.
- `max_live_runs: 4`: a second tab or a rehearsal script can evict the run on screen.
- A judge **Go** writes `scenarios/live/L-<stamp>.yaml` (gitignored) with `expected: {}`;
  while that file exists, `make check` fails three scenario-parsing tests
  (`test_every_scenario_parses`, `test_every_scenario_file_validates_via_resolver`,
  `test_roundtrip_all_loaded_schemas`). Before `make check` after a judge rehearsal:
  `rm scenarios/live/L-*.yaml`. The fix (skip `scenarios/live/` in `scenario_paths` /
  `load_scenarios`) is in sim code, so it is left for the owner.
