# WO-092 — mentor-brief

Phase: 8 · Branch: `wo/092-mentor-brief` · Worktree: `../soundings-worktrees/wo-092-mentor-brief` · Est: 0.75 h · Owner: Claude (for Kudrat)

## Goal
`docs/MENTOR-BRIEF.md` exists for the owner's 10:00 Saturday sessions: one page for William Doyle (AltaSea / Port of LA startup hub) and one for Dr. Nick Wettels (retired submarine commander). Each page opens with the one decision we are stuck on (a question, our current default, and what changes if the answer differs), then three questions, then exactly what to show (`make demo` step numbers from `docs/demo-script.yaml`) and the two or three repo files to have open. A closing checklist lists what to record from each session and where it lands (`docs/DECK-INPUTS.md`, `docs/log/DECISIONS.md`, `config/ladder.yaml` `costs.shutdown_usd_per_min_default`). Every claim about the build is grounded in a repo file.

## Scope
In: `docs/MENTOR-BRIEF.md`; a test that checks the brief's shape and that every cited demo step exists; one WO-092 row in the `docs/STATE.md` work-order table and a one-line pointer under "Mentor sessions".
Out: the deck, `docs/DEVPOST.md`, README (other Phase 8 WOs); any config or threshold change (the mentors' answers are recorded later, with owner approval); removing the IBM Bob line from `docs/DECK-INPUTS.md` (Devpost WO owns that file's tools line).

## Spec references
`docs/BUILD-PLAN.md` Phase 8 · `docs/SPEC-SIM.md` §2, §3 · `docs/SPEC-LADDER.md` §6 (SITREP sections) · `config/ladder.yaml` boundary, criteria, costs · `config/standing_orders.yaml` · `config/likelihoods.yaml` · `config/sim.yaml` · `config/harbor.geojson` · `scenarios/S0*.yaml` · `docs/DECK-INPUTS.md` (second customer, three tests) · `twists/01/response.md` · `docs/log/DECISIONS.md` 2026-10-16 21:20 (USD 0 until a mentor figure).

## Files expected
- `docs/MENTOR-BRIEF.md`
- `tests/test_mentor_brief.py`
- `docs/STATE.md` (one row + one pointer line)
- `docs/work-orders/WO-092-092-mentor-brief.md`

## Exit tests (must all pass with `MODEL_MODE=replay`)
- [ ] `uv run pytest -q tests/test_mentor_brief.py` — the brief exists; has a Doyle page and a Wettels page, each with "Decision we are stuck on", "Three questions" (exactly three numbered items), "What to show"; has the "What we will record" checklist; every `step N (m:ss)` it cites exists in `docs/demo-script.yaml` with that time mark; no model or vendor names in the question sections.
- [ ] `bash scripts/denylist-check.sh` — clean.
- [ ] `make check` — exits 0.
- [ ] manual: `uv run python -m soundings.demo --script docs/demo-script.yaml --pace fast` prints the seven steps the brief cites.

## Model calls
None. Paid run needed? No.

## Rungs touched
None in code. The brief discusses ask (registry, hail), hold (criteria) and hand to a person (SITREP for the external call).

## Hard rules exercised
§2.1 / §2.5 (denylist check over the brief); §2.6 (probabilities named as stress-test estimates, detection named as simulated, in the brief text; checked by `test_mentor_brief.py::test_honesty_labels`).

## Interfaces
Reads `docs/demo-script.yaml` step order: step N is the N-th entry of `steps` (1-based), cited as `step N (m:ss)`. If the demo script is reordered, the test fails and the brief's step numbers must be updated.

## Cut line
If short on time: ship the two pages and the checklist; drop the per-page "if it goes wrong" fallback notes.

## Done
PR: · Merged commit: · Test summary: · STATE.md updated: yes/no
