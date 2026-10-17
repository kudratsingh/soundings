# Soundings

Resolve every unknown underwater contact at the gentlest rung that works.

InnovateLA Dual-Use Hackathon 2026 · USC Viterbi · Track 01 Marine (Crisis Mitigation, with NULL Labs).

The organizers' ladder is **watch → ask → slow → hold → hand to a person**. Soundings lives on *ask* and *hand to a person*: a registry of planned underwater work and a simulated acoustic challenge-response clear most contacts for free; a hypothesis-driven investigation handles the rest with the fewest, cheapest probes; deterministic guards decide what the platform does alone, what it does under signed standing orders, and what a person must approve. The work stops at the handoff.

On a quiet Tuesday it finds the research vehicle that went silent.

## Quickstart

```bash
make setup      # uv sync + pnpm install
make check      # ruff, mypy, pytest (MODEL_MODE=replay), web build, denylist
make dev        # API on :8000 serving the UI
make sim        # run the smoke scenario, deterministic
make demo       # replay the five-minute demo sequence offline
make eval       # catalog × {soundings, bang, human_queue} → artifacts/eval/
```

No model API key is needed to run anything above. Live model calls are opt-in and gated (`docs/WORKFLOW.md §7`).

## Read in this order
`CLAUDE.md` → `docs/BUILD-PLAN.md` → `docs/SPEC-LADDER.md` → `docs/SPEC-INVESTIGATION.md` → `docs/SPEC-SIM.md` → `docs/WORKFLOW.md` → `docs/STATE.md`

## Honesty
Detection is simulated for this demonstration (range falloff, noise, track gaps, biologic false alarms). Probabilities are stress-test estimates from a visible catalog, not operational forecasts. Physically stopping a vehicle is out of scope by the sponsor's brief (see CLAUDE.md §2.1); nothing here models it.
