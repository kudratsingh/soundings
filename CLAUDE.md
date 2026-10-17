# Soundings — Constitution

Resolve every unknown underwater contact at the gentlest rung that works. This file is binding on every coding agent working in this repo. Read it fully before touching anything. The files in `docs/` are mandatory reading in this order: `docs/BUILD-PLAN.md` → `docs/SPEC-LADDER.md` → `docs/SPEC-INVESTIGATION.md` → `docs/SPEC-SIM.md` → `docs/WORKFLOW.md` → `docs/STATE.md`.

## 1. What this is

A 48-hour hackathon build (InnovateLA Dual-Use Hackathon, USC, Oct 16–18 2026, Track 01 Marine with NULL Labs). Devpost submission is due **Saturday Oct 17, 10:00 PM PT, no extensions**. Three twists land (Fri 11:30 PM, Sat 4:30 AM, Sat 7:00 AM); each gets a response within six hours that changes the build, not the slides.

The seat: the harbor watch officer. The moment: an unauthorized autonomous vehicle is in a working harbor. The question the sponsor asked: what sits between "seen" and "handled"; what should a small, low-cost platform decide alone, and what must a person approve. The ordinary-day use: a registry for planned underwater work, and finding the research vehicle that went silent.

The organizers' ladder: **watch → ask → slow → hold → hand to a person**. Soundings lives on **ask** and **hand to a person**. Watch is the floor. Slow and hold run only under signed standing orders.

## 2. Hard rules (never violate)

1. **The work stops at the handoff.** No intercept, halt, capture, pursuit, or "neutralize" state, tool, word, or UI element. Anything after `HANDED_OFF` is a human's call and is out of scope by the sponsor's brief. Watching after handoff (did the contact leave, did the human act) is allowed.
2. **No model output ever becomes a control.** The rung, the guards, and every state transition are decided by deterministic code over a typed evidence table. The LLM may (a) propose which probe to run next from an allowlist, (b) propose hypotheses and the questions to ask a human, (c) write the SITREP, (d) parse a twist notice into a constraint diff. It may never set a rung, change a threshold, approve an action, or call a tool directly. Guard code rejects anything else.
3. **No paid runs without the owner's explicit approval.** A paid run is any call to a hosted model API, any run of the test suite with `--live`, any eval campaign, any CI job that calls a model. Default everywhere is recorded fixtures and `MODEL_MODE=replay`. Before the first paid run of any kind, print the estimated call count and cost, stop, and wait for the owner to type `approve paid run` for that specific run. Never loop a paid call. Never run live calls in CI.
4. **Determinism where it matters.** Simulator, evidence table, scoring, guards, scenario generation, and replay are seeded and reproducible: same seed, same inputs, byte-identical outputs. Every model call is recorded to a fixture keyed by a hash of its inputs and replayed from the fixture by default.
5. **Nothing kinetic in the vocabulary.** The physical halting of a vehicle is out of scope this weekend. Do not name a weapon, net, interceptor, or jammer anywhere in code, docs, or UI.
6. **Evidence first, always.** Every SITREP line links to the evidence row it rests on. Facts and assumptions are labeled separately. A probability is a stress-test number from a declared, visible catalog — never presented as a real operational forecast.
7. **Git discipline** (see `docs/WORKFLOW.md`): one work order = one branch = one worktree = one PR. Conventional commits. `main` is protected. Never force-push. Never commit secrets, API keys, or `.env`.

## 3. Rules that bend with approval

- Adding a dependency outside `pyproject.toml` / `package.json` → ask.
- Changing a threshold in `config/ladder.yaml` → ask and record why in `docs/log/DECISIONS.md`.
- Changing scope (adding a feature not in `docs/BUILD-PLAN.md`) → ask. The owner decides what is cut when time runs short; the default answer to "should we add X" is no until Phase 6 is green.

## 4. Stack (fixed)

- Python 3.12, `uv` for env, `ruff` + `pytest` (+ `hypothesis` where useful).
- Backend: FastAPI + Pydantic v2 + WebSocket for the live contact feed. SQLite via SQLModel for registry, contacts, evidence, audit log. No external DB.
- Agent loop: hand-rolled state machine (no framework), same pattern as the owner's Incident Commander: typed states, checkpoint per transition, idempotency key per action, deterministic guards.
- Model calls: `anthropic` SDK behind `soundings/llm/client.py` with `MODEL_MODE ∈ {replay, record, live}`. Model name is read from `MODEL_NAME` env, never hardcoded.
- Frontend: single-page React + Vite + TypeScript, Leaflet map with OpenSeaMap tiles (free), served by the backend. One screen: map, contact card, evidence ledger, rung indicator, one approval button.
- Sim: pure-Python, seeded, tick-based. Contact kinematics + a detection model that is honest about range, noise, biologics, and track gaps. No sonar physics beyond that.

## 5. Model-call policy

| Role | Mode by default | May it be live? | Budget |
|---|---|---|---|
| `probe_planner` (picks next probe from allowlist) | replay | yes, after approval | ≤ 4 calls per contact |
| `hypothesis_writer` (hypotheses + questions for the human) | replay | yes, after approval | 1 per investigation |
| `sitrep_writer` | replay | yes, after approval | 1 per handoff |
| `twist_parser` (twist notice → constraint diff) | replay | yes, after approval | 1 per twist |
| LLM-as-judge on SITREP quality | replay | yes, after approval | eval only |

Deterministic fallbacks exist for every role (`soundings/llm/fallback.py`): a rule-based probe order, template hypotheses, a template SITREP. The system must run end-to-end with `MODEL_MODE=replay` and zero fixtures present, using fallbacks. Demo day runs on fixtures + fallbacks unless the owner approves live.

## 6. Scope line

In: contact ingestion, evidence table, investigation loop, ladder with guards, registry, acoustic challenge-response (simulated), standing orders, SITREP, watch-after-handoff, baselines, eval harness, twist pipeline, one-screen UI, judge-injection panel, demo video, deck inputs.

Out: physical interdiction of any kind, real sonar signal processing, identity inference about people, surveillance outside declared security zones, retention beyond 30 days, autonomous calls to USCG/EOD (the human makes the call; the system drafts it).

## 7. Definition of done for any work order

- The exit tests in the work order pass locally with `MODEL_MODE=replay`.
- `make check` is green (ruff, pytest, type check, frontend build).
- `docs/STATE.md` updated: what shipped, what's next, what's blocked.
- The PR description lists which rung(s) of the ladder the change touches and which hard rules it exercised.
- No new paid calls were made without a line in `docs/log/PAID-RUNS.md`.
