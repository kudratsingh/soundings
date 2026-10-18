# WO-060E — quiet the other cases

Phase 6 (watch-officer UI), follow-up to WO-060D (focus one case). Branch `wo/060e-quiet-others`.

## Goal

Owner directives 2026-10-17 17:52–17:58 PDT: "still seeing many cases at once, should only be c002 for s05"; "for all scenarios it should only focus on the 1 agent being paged"; "events from all diff agents makes the UI too confusing for now and for the demo". Earlier the same day: every agent instance keeps running concurrently; the fix is organisation, not serialisation.

The screen shows one case; `others · n` opens the rest; `show all` reveals every event.

## Scope (default behaviour during a run)

1. **Cases list.** Only the focused case is drawn as a row; every other case folds into one `others · n` row under it with a severity summary (most severe group first, e.g. `1 handed to a person · 4 cleared at ask`). Clicking it opens the list below the focus (a click on a case there pins the focus to it); clicking it again folds it. It is folded on every run start.
2. **Sounds** (`web/src/sfx.ts`). A transition sounds only when its contact belongs to the focused case; a responder dispatch sounds only when it looks at the focused case; the twist cue stays run-wide. `show all` lets every case's cues through.
3. **Key moments.** The focused case's moments (oldest first) and one trailing `others` chip (the same component change as WO-060D).
4. **Drawers.** `All evidence for this case` and `All transitions (every case)` start collapsed; the every-case drawer is not drawn unless `show all` is on. The per-step evidence rows of the focused case are unchanged.
5. **Map.** Contacts outside the focused case are drawn at 0.35 opacity with no label and no velocity leader; the focused case keeps its label, trail, leader and ellipse; the responder and the legend are unchanged.
6. **Presenter mode and the recorder** (`?presenter=1`) default to the same focused-only screen; the `show all` control is not drawn there.
7. **No layout shift.** The cases panel keeps its fixed 196 px height; the `others` row has the case row's 32 px height; unfolding animates opacity only.

Focus: until WO-060D's focus policy merges, `web/src/quiet.ts` carries a minimal one in a marked block — pinned on click, else sticky: the first case that reaches a rung or a handoff (the paged case); before that the first case past ASK that is still open; else the first detected. WO-060D's `focusedCase` supersedes it on merge; everything above keys off whichever case is focused.

Display only: no stream schema change, no ladder/guard/threshold/sim change, no caption wording change, no model call.

## Files

`web/src/quiet.ts` (new, pure), `web/src/components/{CaseList,CaseView,GlobalStrip,HarborMap,PresenterMode}.tsx`, `web/src/sfx.ts`, `web/src/App.tsx`, `web/src/index.css` (one marked block); `tests/test_quiet_others.py` + `tests/js/quiet_check.mjs`; `tests/e2e/test_quiet_smoke.py` + `tests/e2e/_quiet.py`; existing smokes and `scripts/readme_shots.py` open `others` before picking a non-focused row.

## Exit tests (`MODEL_MODE=replay`, no network)

- `tests/test_quiet_others.py` (node ≥ 22.6 runs `tests/js/quiet_check.mjs`; skips without it): the default view draws only the focused row and folds the other five; `others · 5` and `1 handed to a person · 4 cleared at ask`; opening keeps the focus on top; `show all` is the old list; the others row starts folded on every run (`useState(false)`, reset on `runId`); only the focused case's transitions and dispatches sound, the twist stays run-wide; key moments are the focused case's; the every-case drawer only with `show all`; other contacts quiet on the map; `show all` hidden in presenter mode; the unfold keyframes touch `opacity` only.
- `tests/e2e/test_quiet_smoke.py` (opt-in `SOUNDINGS_E2E=1`): S05 seed 11 at 20x, sampled every 250 ms until the focused case holds: exactly one case row while ≥ 3 cases exist, `others · n` = cases − 1, the NOW line names the focused case at every sample, the focus ends on `c-0002` and moves at most once, no transitions drawer, CLS < 0.05 while streaming; then `others` opens every row with the focus on top, a click pins another case, `show all` draws the transitions drawer. A second test: `?presenter=1` draws no `show all`.
- Existing: `tests/e2e/test_design_pass.py`, `tests/e2e/test_v3_case_view.py`, the other smokes, `tests/test_ui_tokens.py`, `scripts/readme_shots.py` (three screenshots) stay green.

## Rungs touched

None decided here; the screen follows the case on *ask* → *hand to a person*. Display only.

## Hard rules exercised

§2.1 (nothing past the handoff), §2.2 (no model output becomes a control: display only), §2.3 (no paid runs), §2.5 (vocabulary: denylist test over `web/src`).
