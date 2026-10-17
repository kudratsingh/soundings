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
- Agent loop: hand-rolled state machine (no framework), following a state-machine agent design the owner has used before: typed states, checkpoint per transition, idempotency key per action, deterministic guards.
- Model calls: `anthropic` SDK behind `soundings/llm/client.py` with `MODEL_MODE ∈ {replay, record, live}`. Model name is read from `MODEL_NAME` env, never hardcoded.
- Frontend: single-page React + Vite + TypeScript, Leaflet map with OpenSeaMap tiles (free), served by the backend. One screen: map, contact card, evidence ledger, rung indicator, one approval button.
- Sim: pure-Python, seeded, tick-based. Contact kinematics + a detection model that is honest about range, noise, biologics, and track gaps. Amended 2026-10-17 (owner directive, WO-089; until WO-089 merges, the WO-011 Gaussian falloff is what runs): detection uses the passive sonar equation (SE = SL − TL − NL − DT, TL = 20·log10 r + α·r, p = logistic(SE/k)) with ambient noise from sea state and nearby vessel transits; hail latency, camera visibility and responder battery drain each follow one stated law. Every parameter lives in `config/physics.yaml` with a source string and the label "demo value", and the About → Physics cards show the equations and what is not modeled. No sonar physics beyond that: no beam patterns, multipath, bottom type, hydrodynamics or signal processing, and the detection model stays labeled "simulated for this demonstration".

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

## 8. Amendments (owner directives recorded by the coordinator; dated; §2 hard rules unchanged)

Each item below is an owner decision from 2026-10-17 with its DECISIONS.md row. Where an amendment touches earlier text, the amendment wins; nothing here loosens §2.

- **A1 — Work-order numbering (2026-10-17 07:51 PDT).** On `main`: WO-087 bounded live mode (eval/model-run directive item 4), **WO-088 responder platform** (owner text said "WO-087"), **WO-089 physics cards** (owner text said "WO-088"), WO-090 deck + Devpost, WO-091 committee README, WO-092 mentor brief. Agents follow the numbers in `docs/work-orders/`, not the numbers in chat.
- **A2 — Responder platform is a probe (owner directive 07:45 PDT).** One small simulated responder vehicle per harbor may be dispatched **to look** at a contact. It is a mobile probe at rung *ask*, never an action against anything: standoff ≥ 25 m (it orbits at standoff, never inside), geofenced to declared security zones, re-stations if the contact moves, never closes faster than the contact, never places itself between the contact and an asset, one dispatch per contact. Dispatch inside the geofence at standoff is "platform decides alone"; leaving the geofence, standoff below 25 m, or a second vehicle is "a person approves"; nothing after `HANDED_OFF`. The planner may request a *look*, never a position (guard-enforced, logged to VIOLATIONS.md). This is the owner's refinement of the Phase 7 line "no vehicle commands": that line forbids commanding *contacts or operators' vehicles*; the platform's own sensor carrier is in scope under these rules. Vocabulary: look, station, standoff, dispatch to look, return to dock. §2.1 and §2.5 stand.
- **A3 — Detection physics (owner directive 07:50 PDT).** §4's sim line was amended the same day (WO-089 docs PR #40); this item records the directive: detection uses a passive-sonar-equation model (SL − TL − NL vs DT, TL = 20·log10 r + α·r, logistic detection probability) with every parameter in `config/physics.yaml` carrying a source string and a "demo value" label; ambient noise rises with nearby vessel transits and sea state; hail latency = 2r/1500 m/s; camera visibility = exp(−k·depth)·exp(−turbidity); responder battery drain ∝ v³; current field unchanged. The honesty line "simulated for this demonstration" stays, now with the equations shown (About → Physics cards). Catalog outcomes stay unchanged except where the eval report explains why.
- **A4 — Paid runs.** Budget **$15 total for now**; every paid run still waits for the owner's explicit `approve paid run …` phrase (§2.3 unchanged). The one planned run is the fixture recording (~60 calls) at ~12:30 after BT-01, WO-033 and WO-042 merge. Stage live mode (WO-087) exists but stays off: `MODEL_MODE=live` honors `LIVE_CALL_BUDGET`, 6 s per-call timeout, any error → deterministic fallback, every call audited with its path; enabled only by `approve paid run stage live budget 40` on Sunday; the UI badge says "model: live" or "model: fallback" so nothing is overclaimed.
- **A5 — One interruption definition.** An interruption is any event that requires the watch officer or backup approver to look or act: approval request, backup page, handoff SITREP to the officer, `request_watch_officer_look`. Operator notices (LOST_VEHICLE to the plan's contact) and watch-after-handoff updates do not count. Catalog `max_human_interruptions` and the eval use this one definition.
- **A6 — Dependencies approved** (§3 "ask" satisfied by DECISIONS rows): matplotlib (dev), websockets, imageio-ffmpeg (dev), shapely, ortools (lazy import), segno. No others without a DECISIONS row.
- **A7 — Local test policy.** Browser (Playwright) smokes are opt-in locally (`SOUNDINGS_E2E=1`); CI skips them (no browser). `make check` otherwise unchanged.
- **A8 — Twists.** Scored at final submission only; no per-twist deadline. Twists 2 and 3 must each produce a toggle, scenario, or feature with a test; the Devpost paragraphs live in `docs/DECK-INPUTS.md` → Twists.
- **A9 — Product line (Phase 7).** Soundings proposes slots and advisories, operators accept, it never steers anything, the work stops at the handoff. No route planning, no commands to contacts or to operators' vehicles. Separation advisories go through `guards.py` like any other `slow`; slot proposals change nothing until an operator accepts.
