# WO-000 — repo-and-scaffold

Phase: 0 · Branch: `wo/000-scaffold` · Worktree: n/a (first commit lands on main before protection is enabled; everything after goes through PRs) · Est: 1 h · Owner: Kudrat

## Goal
A public GitHub repo `kudratsingh/soundings` with the constitution, build plan, specs, CI, branch protection, and a skeleton where `make check`, `make dev`, and `make sim` all run.

## Scope
In: repo creation, protection, CI, Python + web skeleton, Makefile, sim smoke scenario, health route, this kit copied in.
Out: any product logic.

## Spec references
`CLAUDE.md §4` (stack), `docs/WORKFLOW.md §1–§3`, `docs/BUILD-PLAN.md` Phase 0.

## Steps
1. `gh auth status` — confirm the account is `kudratsingh`. If not, stop and ask.
2. `gh repo create kudratsingh/soundings --public --description "Resolve every unknown underwater contact at the gentlest rung that works. InnovateLA Dual-Use Hackathon 2026, Track 01 Marine." --clone`
3. Copy the starter kit into the repo root; `git add -A && git commit -m "chore: constitution, build plan, specs, workflow"`.
4. `uv init --name soundings --python 3.12`; add `fastapi uvicorn pydantic sqlmodel numpy pyyaml anthropic typer rich`; dev: `pytest hypothesis ruff mypy httpx playwright`.
5. `pnpm create vite web --template react-ts`; add `leaflet react-leaflet`.
6. Create `Makefile` targets: `setup`, `check` (ruff + mypy + pytest + `pnpm -C web build` + denylist grep), `dev`, `sim`, `sim-plot`, `demo`, `eval`.
7. Commit `.github/workflows/ci.yml` and `.github/pull_request_template.md` (provided in kit).
8. Push; run `scripts/protect-main.sh`.
9. `soundings/sim/__main__.py` with `--seed --ticks --scenario smoke` emitting a deterministic contact table.
10. `soundings/api.py` with `/health` returning `{"ok": true, "model_mode": os.environ.get("MODEL_MODE","replay")}` and static serving of `web/dist`.

## Files expected
- `Makefile`, `pyproject.toml`, `uv.lock`, `web/`, `soundings/__init__.py`, `soundings/api.py`, `soundings/sim/__main__.py`, `soundings/llm/client.py` (mode switch + fixture store, no calls yet), `scripts/protect-main.sh`, `config/denylist.txt`, `.github/workflows/ci.yml`, `.github/pull_request_template.md`, `.gitignore` (includes `.env`, `artifacts/`, `tests/fixtures/model_calls/**/*.tmp`).

## Exit tests
- [ ] `gh repo view kudratsingh/soundings --json name,visibility` → name `soundings`, visibility `PUBLIC`
- [ ] `gh api repos/kudratsingh/soundings/branches/main/protection` → required status checks include `check`; `allow_force_pushes.enabled == false`; `required_pull_request_reviews` present (0 approvals is fine for a hackathon)
- [ ] fresh clone: `make setup && make check` exits 0
- [ ] `make dev` then `curl -s localhost:8000/health` → `{"ok":true,"model_mode":"replay"}`
- [ ] `python -m soundings.sim --seed 7 --ticks 60 --scenario smoke > a.txt; python -m soundings.sim --seed 7 --ticks 60 --scenario smoke > b.txt; diff a.txt b.txt` → empty
- [ ] `grep -riEf config/denylist.txt soundings web/src docs --exclude-dir=node_modules` → only matches inside `config/denylist.txt`, `docs/SPEC-LADDER.md §1`, and `CLAUDE.md §2.5` (the places that define the list)
- [ ] `docs/STATE.md` Phase 0 entry with commit hash

## Model calls
None. Paid run needed? No.

## Rungs touched
None.

## Hard rules exercised
§2.3 (CI never sets live mode — proven by reading `ci.yml`), §2.4 (sim determinism diff), §2.7 (protection API output).

## Done
PR: · Merged commit: · Test summary: · STATE.md updated:
