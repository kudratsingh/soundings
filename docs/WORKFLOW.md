# Workflow — git, worktrees, work orders, paid runs

## 1. Branching

- `main` is protected: PRs only, CI green required, no force-push, linear history (squash merge).
- One work order = one feature branch = one worktree = one PR. Branch name: `wo/<id>-<slug>`, e.g. `wo/031-probes`.
- Twist responses: `twist/<n>-<slug>`; merged to `main` before the portal submission so the proof commit hash is on `main`.
- Hotfixes during demo prep: `fix/<slug>`.

## 2. Worktrees (keep the main checkout clean)

The owner's convention: worktrees live in a sibling folder, not inside the repo.

```
~/hackathons/soundings/            # main checkout (stays on main)
~/hackathons/soundings-worktrees/  # one folder per branch
```

```bash
# start a work order
git -C ~/hackathons/soundings fetch origin
git -C ~/hackathons/soundings worktree add ../soundings-worktrees/wo-031-probes -b wo/031-probes origin/main
cd ~/hackathons/soundings-worktrees/wo-031-probes && make setup

# finish
gh pr create --fill --base main
# after merge
git -C ~/hackathons/soundings worktree remove ../soundings-worktrees/wo-031-probes
git -C ~/hackathons/soundings branch -d wo/031-probes
```

Several agents may work in parallel, one per worktree. They never share a worktree. Shared files (`docs/STATE.md`, `config/*.yaml`) are edited in small commits and rebased early to avoid conflicts.

## 3. Commits

Conventional commits: `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `twist:`. Subject ≤ 72 chars, body says why. Every commit passes `make check`. Commit early and often; squash on merge.

## 4. Work orders

Each WO is a file in `docs/work-orders/WO-NNN-<slug>.md` with: goal, scope in/out, files expected, exit tests (copied from `BUILD-PLAN.md` and extended), estimated hours, and a "done" section the agent fills with the PR link and the test output summary. Template in `docs/work-orders/WO-TEMPLATE.md`. The agent writes the WO before writing code; a WO without exit tests is not a WO.

## 5. PR checklist (in `.github/pull_request_template.md`)

- [ ] Exit tests from the WO pass locally with `MODEL_MODE=replay`
- [ ] `make check` green
- [ ] Which ladder rung(s) this touches
- [ ] Which hard rules (CLAUDE.md §2) this exercises, and how the tests prove them
- [ ] No paid runs, or a line added to `docs/log/PAID-RUNS.md`
- [ ] `docs/STATE.md` updated
- [ ] No new words from `config/denylist.txt`

## 6. Review

Given the clock, the default is **no separate review pass** — the exit tests and CI are the review. The owner may request a review pass on Phase 3 and Phase 4 PRs (the guards) because those are the safety-critical code; if requested, the reviewer is a fresh agent with only the spec and the diff, and the review is a checklist against `SPEC-LADDER §3–§7`, not a style pass. This is a deliberate cost decision; see `docs/log/DECISIONS.md`.

## 7. Paid runs

A paid run is any live model call, anywhere. The procedure, every time:

1. Print: role, number of calls, model name, estimated input/output tokens, estimated USD, what will be recorded to fixtures.
2. Stop and wait for the owner to type exactly `approve paid run <short description>`.
3. Run with `MODEL_MODE=record`. Fixtures land in `tests/fixtures/model_calls/<role>/<inputs_hash>.json`.
4. Append a line to `docs/log/PAID-RUNS.md`: time, role, calls, cost, fixture count, approver.
5. Switch back to `MODEL_MODE=replay` immediately.

Suggested paid-run moments (owner decides): once after Phase 3 to record probe-planner fixtures for S04–S08 (~40 calls); once after Phase 4 for SITREPs (~10 calls); once before the demo for the judge-injection paths (~20 calls). Everything else runs on fixtures and fallbacks. Estimated total under $5 at current rates; the agent must still compute and show the number each time.

## 8. Determinism checklist

- All randomness via `numpy.random.default_rng(seed)` or `random.Random(seed)` passed explicitly; no module-level global RNG.
- No wall-clock reads in the sim or scorer; time is the tick. The ladder's timers use the tick clock in sim mode and wall-clock only in live UI mode, behind one `Clock` interface.
- Dict iteration order is insertion order; sets are sorted before iteration where output depends on them.
- Floats are rounded at output boundaries (6 dp) before hashing.
- Model calls are keyed by `sha256(role + canonical_json(inputs))`; replay misses raise in CI and use the fallback outside CI with a logged warning.

## 9. State and logs

- `docs/STATE.md` — what's done, what's next, what's blocked, with timestamps. Updated at the end of every WO and after every twist.
- `docs/log/DECISIONS.md` — one line per decision that changed scope, a threshold, or the cost model.
- `docs/log/PAID-RUNS.md` — every paid run.
- `docs/log/VIOLATIONS.md` — every rejected model output (role, reason, fixture key). This is also demo material: "here is every time the model tried to do something it isn't allowed to."
