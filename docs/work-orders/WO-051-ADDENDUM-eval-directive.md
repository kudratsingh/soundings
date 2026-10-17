# WO-051 addendum — eval and model-run directive (owner, 2026-10-17 06:35 PDT; binding, verbatim)

Extends `WO-051-eval-harness.md` (Part B). Deterministic, no model calls except where stated.

1. FIXTURE RUN moves to ~12:30, after BT-01, WO-033 and WO-042 merge. One run: probe_planner + hypothesis_writer over S04–S08 and the BT-01 rogue, sitrep_writer over every handoff scenario. Print the real count (expect ~60) and dollar estimate via scripts/paid-run-gate.py, wait for my approval phrase, record once, return to replay. If I'm not there within 15 min, keep building and re-raise it.

2. WO-051 additions (deterministic, no model calls; fold in now):
   a. Mis-specified sim: sim samples from likelihood tables perturbed ±20% (seeded), scorer keeps the originals. Report missed threats (must stay 0) and the change in false shutdowns and rung histogram.
   b. Toggle sweep: every twist toggle × catalog. Terminal states vs expected; interruptions/hour per toggle.
   c. Threshold sensitivity: sweep stopping.threat_mass_handoff 0.25→0.45 in 0.05 steps; false shutdowns vs missed threats; one PNG curve for the deck.
   d. Scale: BT-01 at 40 / 100 / 200 jobs; wall time, interruptions/hour, % cleared at ask; one PNG.
   e. Seeds: catalog over seeds 1–20; report mean and min–max for every metric, not a single seed.
   Every table in artifacts/eval/report.md carries the seed(s) and a one-line method. report.json alongside. `make eval` regenerates everything in < 5 min.

3. Planner ablation (after the fixture run): LLM planner vs deterministic vs random probe order on S04–S08 and BT-01: probes used, seconds seen→handled, missed threats, interruptions. Auto-populate the "Where the LLM layer added nothing" section from the diff. If it added nothing, the report says so plainly.

## Coordinator notes (binding)
- Item 2c sweeps a threshold for the REPORT only: it passes an overridden stopping config into the investigation machine; `config/ladder.yaml` itself is never edited by the eval.
- Items 2a–2e are Part B exit tests in addition to the WO-051 originals; PNGs use the approved matplotlib dev extra (lazy import) and land in `artifacts/eval/`.
- Item 3 needs recorded fixtures (the 12:30 run); until then the section states plainly that both planner paths are the fallback.
- `make eval` < 5 min: use `ticks`/tick skipping from WO-080 and run scenarios in-process; report wall time in the method line.
