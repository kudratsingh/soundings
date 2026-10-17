# WO-091 — README for the review committee

Phase: 8 · Branch: `wo/091-readme-committee` · Worktree: `../soundings-worktrees/wo-091-readme-committee` · Est: 1.5 h · Owner: Kudrat (coordinator), Claude implementing

Written 2026-10-17 06:36 PDT from the owner's Phase 8 directive (06:45 PDT, item 4). Deadline: README by Sat 6:00 PM.

## Goal
The review committee opens the repo link Saturday night. When this is done, `README.md` fits in two screens and says, top to bottom: one paragraph (what Soundings is, the ladder, the two rungs we live on, the work stops at the handoff), one hand-written architecture diagram `docs/architecture.svg`, three UI screenshots, a quickstart (`make setup && make demo`) verified on a fresh clone, where the eval report and the deck live, the honesty section, the read-in-order list, and a repo map.

## Scope
In:
- `README.md` rewritten top to bottom (≤ 120 lines, no section longer than 12 lines).
- `docs/architecture.svg`: dark theme matching the UI tokens (`--bg #07161f`, `--panel #0d2230`, `--line #23404f`, `--fg #e3eef3`, `--muted #8fa9b6`, `--accent #0e7490`, `--fact #1f6feb`, `--assumption #b35900`). Five stages: contact feed → evidence table → investigation → ladder / guards → handoff. Model roles `probe_planner`, `hypothesis_writer`, `sitrep_writer` attached to their stages and marked "propose only". Registry + acoustic hail feed "ask".
- `docs/screenshots/{01-map-evidence,02-rung-mid-run,03-sitrep-or-lost}.png` captured with Playwright from the UI (WO-060 / PR #20). If the UI is not on `main` by the time budget, placeholders are committed and the gap is stated.
- `scripts/readme_shots.py`: the Playwright capture script (re-runnable for the 8 PM pass).
- Links: `artifacts/eval/report.md` (gitignored; regenerate with `make eval`) and `docs/deck/deck.pdf` (built by the deck WO).
- One row for WO-091 in `docs/STATE.md`.
- IBM Bob was not used (owner, 2026-10-17): drop the "IBM Bob if used" clause from the tools line in `docs/DECK-INPUTS.md`.

Out: the deck, `docs/DEVPOST.md`, the mentor brief (separate WOs); any Python or UI behavior change; new Makefile targets; new dependencies (Playwright is already a dev extra).

## Spec references
`CLAUDE.md §1, §2.1, §2.2, §2.5, §5`; `docs/BUILD-PLAN.md` Phase 8 exit test 1; `config/ladder.yaml` (boundary).

## Files expected
- `README.md`
- `docs/architecture.svg`
- `docs/screenshots/*.png` (3)
- `scripts/readme_shots.py`
- `docs/work-orders/WO-091-091-readme-committee.md`
- `docs/STATE.md` (one row), `docs/DECK-INPUTS.md` (one clause)

## Exit tests (must all pass with `MODEL_MODE=replay`)
- [ ] two screens: `test $(wc -l < README.md) -le 120` and `awk '/^## /{if(n>12)bad=1;n=0;next}{n++}END{if(n>12)bad=1;exit bad}' README.md`
- [ ] SVG: `uv run python -c "import xml.etree.ElementTree as E;t=open('docs/architecture.svg').read();E.fromstring(t);[t.index(s) for s in ['contact feed','evidence table','investigation','ladder','handoff','propose only','probe_planner','hypothesis_writer','sitrep_writer','registry']]"`
- [ ] screenshots: `ls docs/screenshots/*.png | wc -l` = 3 and `find docs/screenshots -name '*.png' -size +400k` empty
- [ ] fresh clone: `git clone` into the scratchpad, `make setup && MODEL_MODE=replay make demo`, tail pasted in the PR body (network: uv and pnpm package registries only)
- [ ] `bash scripts/denylist-check.sh` clean
- [ ] `! grep -ri 'ibm bob' README.md docs/architecture.svg docs/DECK-INPUTS.md`
- [ ] `make check` exits 0

## Interfaces
- Consumes: `make setup`, `make demo` (WO-062), the UI at `make dev` / `soundings.api:app` serving `web/dist` (WO-060), `artifacts/eval/report.md` (WO-051), `docs/deck/deck.pdf` (deck WO).
- Produces: `docs/architecture.svg` and `docs/screenshots/*.png`, reusable by the deck and Devpost (same paths).

## Cut line
Keep: paragraph, SVG, quickstart verified, honesty, links. Cut first: screenshots (placeholders + a note if PR #20 is not merged in time), then the repo map.

## Model calls
None. No paid run.

## Rungs touched
None in code. The README and diagram describe watch / ask / slow / hold / hand to a person.

## Hard rules exercised
§2.1 and §2.5 (vocabulary: denylist check over README and SVG text); §2.2 (diagram marks every model role "propose only"); §2.3 (quickstart runs with `MODEL_MODE=replay`, no key).

## Done
PR: · Merged commit: · Test summary: · STATE.md updated: yes/no
