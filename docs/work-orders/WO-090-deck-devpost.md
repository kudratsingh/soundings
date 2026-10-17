# WO-090 — deck-devpost

Phase: 8 · Branch: `wo/090-deck-devpost` · Worktree: `../soundings-worktrees/wo-090-deck-devpost` · Est: 1.8 h · Owner: domain / eval / deck (coordinator Kudrat)
Directive: owner Phase 8 directive, 2026-10-17 06:45 PDT, items 2 (deck) and 3 (Devpost). Items 1 (hourly STATUS), 4 (README), 5 (mentor brief) are other work orders.

## Goal
`make deck` renders an 11-slide, 16:9, dark HTML pitch deck (`docs/deck/index.html`, one self-contained file in the watch-officer UI's visual language) from a template, pulling every number from `artifacts/eval/report.json`, the boundary table from `config/ladder.yaml`, the twist rows from `docs/DECK-INPUTS.md`, and exports `docs/deck/deck.pdf` (one 1920x1080 page per slide) with the cached Playwright chromium. `docs/DEVPOST.md` holds every Devpost field ready to paste, without IBM Bob.

## Scope
In:
- `docs/deck/template.html` — the slide template. Slides in the hub order (0:30 the problem · show it working · twists · next and feasibility): 1 Title, 2 The seat and the moment, 3 The ladder, 4 Two lives, 5 Live demo (intentionally empty), 6 The boundary table, 7 Evidence, 8 Twists, 9 What it runs on, 10 Honesty + responsible use, 11 Next. Keyboard navigation (arrows, space, PageUp/PageDown, Home/End); print CSS lays one slide per page.
- `docs/deck/deck.yaml` — deck-owned inputs that are not eval numbers: team line, BOM lines (every figure labeled estimate), tenth-of-budget version.
- `scripts/build_deck.py` — `{{ key }}` string-template render (no Jinja2: not a dependency). Every number is emitted inside an element carrying `data-src` (`report:<json path>`, `config:ladder.<path>`, `deck:<path>`, `inputs:<file>`); externally sourced text (boundary cells, twist rows, notes) carries `data-src` too. Missing `report.json` → every metric renders `[eval pending]`, a warning goes to stderr, exit 0. Denylist check over the rendered HTML (exit 1 on a hit). Also refreshes the evidence block between `<!-- eval:begin -->` and `<!-- eval:end -->` in `docs/DEVPOST.md`.
- `scripts/export_deck.py` — Playwright (Python, dev dependency) prints `docs/deck/deck.pdf` at 1920x1080 per slide. It finds a cached chromium under `~/Library/Caches/ms-playwright` (or `$PLAYWRIGHT_BROWSERS_PATH`, or `--executable-path`) and never downloads.
- `Makefile` target `deck` (build + export) only.
- `docs/DEVPOST.md` — every field; `docs/DECK-INPUTS.md` — IBM Bob removed from Tools used.
- `tests/test_deck.py`, `tests/fixtures/deck/report.json` (a synthetic report in the WO-051 `EvalReport` shape; its numbers are test data, never shipped).
- `.gitignore`: `docs/deck/deck.pdf`.

Out: README (directive item 4), mentor brief (item 5), hourly STATUS lines (item 1), screenshots, running the eval, editing `config/ladder.yaml`, any figure typed into the template.

## Interfaces
Consumed:
- `artifacts/eval/report.json` (WO-051 `EvalReport`): `outcomes.<policy>.{gt_contacts, at_or_below_ask_pct, watch, ask, slow, hold, hand_to_person, missed_threats, false_shutdowns, median_s_seen_to_handled, unhandled, interruptions_per_hour, shutdown_minutes, shutdown_usd, probes_used}`, `false_alarms.<policy>`, `policies_pending`, `suite`, `seeds`, `inputs_sha256`, `label`, `llm.sentence`.
- Threshold curve (not in WO-051 yet): `report.json["threshold_curve"]["png"]` (repo-relative path) if present, else `artifacts/eval/threshold_curve.png` if it exists; embedded as a data URI. Absent → `[threshold curve pending]`.
- `config/ladder.yaml` `boundary`, `criteria`, `lives_on`, `rungs`; `docs/DECK-INPUTS.md` sections `One-liner (Devpost)`, `Slide: twists` (markdown table), `Responsible-use note (two lines, Devpost)`.
Exposed:
- `python scripts/build_deck.py [--report PATH] [--out PATH] [--devpost PATH|--no-devpost]` (exit 0; 1 on denylist hit or template key error).
- `python scripts/export_deck.py [--html PATH] [--pdf PATH] [--executable-path BIN]` (exit 0; 2 when no cached chromium is found).
- `make deck`.

## Files expected
- `docs/deck/template.html`, `docs/deck/deck.yaml`, `docs/deck/index.html` (rendered, committed with placeholders until the eval lands)
- `scripts/build_deck.py`, `scripts/export_deck.py`
- `tests/test_deck.py`, `tests/fixtures/deck/report.json`
- `docs/DEVPOST.md`, `docs/DECK-INPUTS.md`, `Makefile` (`deck`), `.gitignore`, `docs/STATE.md` (one row), `docs/log/DECISIONS.md` (one line)

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
- [ ] `mv artifacts/eval/report.json{,.bak} 2>/dev/null; make deck; echo $?` → 0, warning printed, `docs/deck/deck.pdf` exists with 11 pages: `grep -ac '/Type */Page[^s]' docs/deck/deck.pdf` style count (`tests/test_deck.py::count_pdf_pages`).
- [ ] `mkdir -p artifacts/eval && cp tests/fixtures/deck/report.json artifacts/eval/report.json && make deck` → 0, 11 pages, no `[eval pending]` in `docs/deck/index.html`; then remove the copied fixture.
- [ ] `uv run pytest tests/test_deck.py -q` — proves: 11 `<section class="slide">` in hub order with slide 5 empty; every `data-src="report:…"` value equals the value at that path in the fixture report; with all `data-src` elements removed, the rendered text has no digit outside a short identifier allowlist; the template text (outside `<style>`/`<script>`) has no digits and no `%`; absent report → `[eval pending]` placeholders and exit 0; the boundary table rows equal `config/ladder.yaml`; twist rows equal the DECK-INPUTS table; rendered HTML and DEVPOST.md denylist-clean; DEVPOST description 200–260 words; PDF has 11 pages (skipped when no cached chromium, e.g. CI).
- [ ] `bash scripts/denylist-check.sh` → clean (covers DEVPOST.md, DECK-INPUTS.md, deck.yaml; the HTML is covered by the build and `test_deck.py`).
- [ ] `grep -i "ibm bob" docs/DECK-INPUTS.md docs/DEVPOST.md` → nothing (the Devpost "Best Build with IBM Bob" field is named without the product name and left blank).
- [ ] `git check-ignore docs/deck/deck.pdf` → ignored (decision: the PDF is a build output; the 8 PM pass uploads it to Devpost and may `git add -f` the final one).
- [ ] `make check` → 0.

## 8 PM final pass (another agent)
1. `make eval` (WO-051 merged with runner v3) so `artifacts/eval/report.json` has the soundings policy; optional threshold curve at `artifacts/eval/threshold_curve.png` or `report.json["threshold_curve"]["png"]`.
2. Fill twist rows 2 and 3 in `docs/DECK-INPUTS.md` "Slide: twists" (`| n | what changed | proof |`).
3. `make deck` → re-renders `docs/deck/index.html`, refreshes the DEVPOST evidence block, prints `docs/deck/deck.pdf`. Check no `[eval pending]` remains: `grep -c "eval pending" docs/deck/index.html` → 0.
4. Team names in `docs/deck/deck.yaml` `team`, BOM figures confirmed or relabeled.

## Model calls
None. Paid run needed? No.

## Rungs touched
None changed; the deck describes watch / ask / slow / hold / hand to a person.

## Hard rules exercised
§2.1 and §2.5 (denylist over the rendered deck and DEVPOST: `test_deck.py::test_denylist_clean`); §2.6 (honesty slide text and "estimate" labels on every BOM figure: `test_deck.py::test_honesty_labels`; no hand-typed figure: `test_deck.py::test_no_hand_typed_numbers`).

## Cut line
If late, drop in order: the DEVPOST evidence-block refresh (leave placeholders); the threshold-curve embed (placeholder); keyboard Home/End. Never cut: placeholders-not-figures, the denylist check, 11 pages.

## Done
PR: (see branch PR) · Merged commit: (squash on merge) · Test summary: `tests/test_deck.py` 27 passed; full suite 1472 passed after rebase; `make check` exit 0 · STATE.md updated: yes (one row)
- `make deck` exit 0 without `report.json` (warning, 8 `[eval pending]` sites, 11-page PDF) and with the synthetic fixture copied in (0 pending, 11 pages); the fixture copy was removed and `docs/deck/index.html` / `docs/DEVPOST.md` were re-rendered with placeholders before commit.
- Deviations: file named `WO-090-deck-devpost.md` (the task text said `WO-090-090-...`, a doubled number); OR-Tools and shapely are listed with an owner check (they arrive with WO-081/WO-082, unmerged at this WO); DEVPOST Twists mirrors the DECK-INPUTS 'Twists (Devpost writeup)' section that landed on main during this WO; `config/denylist.txt`'s check script does not scan `.html`, so the rendered deck is checked by `build_deck.py` and `test_deck.py` instead.
