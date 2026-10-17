"""The catalog verifier: every scenario's ``expected`` block against runner v3 (WO-043).

``grade(result)`` reads a runner v3 ``RunResultV3`` the way the catalog is judged (WO-022 and
WO-033 convention): the scenario's ground-truth contact is the lineage of observed ids that
track it (sim false alarms excluded; ground truth is read here only to pick what to grade).

* ``terminal_state``: the lineage end's ``handling_outcome``;
* ``passes_through``: each listed state appears, in order, among the lineage's ladder
  checkpoints (all of its observed ids, in tick order);
* ``min_threat_mass`` and ``top_hypothesis``: the lineage end's recommendation;
* ``max_human_interruptions`` and ``max_probes``: per observed id of the lineage (one
  definition, DECISIONS 23:05: approval request, backup page, handoff SITREP, eyes-on probe);
* ``sitrep_has_search_box`` and ``search_box_contains_truth``: the lineage end's SITREPs.

It lives in ``soundings.eval`` (not ``soundings.ladder``): grading reads ground truth and the
``expected`` block, which tests/test_boundary.py keeps out of the ladder package.
``check_expected(result_or_graded, scenario_or_expected) -> list[ExpectationFailure]``.
``python -m soundings.eval.catalog_verify --suite catalog [--json]`` (also
``python -m soundings.runner verify``) runs each catalog scenario at its own seed under the
signed standing orders into a tmp audit DB, verifies each audit chain, prints one row per
scenario and exits 0 only if every expectation and every chain holds. Nothing here imports
a model client; the verifier only reads results.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from soundings.canonical import canonical_json, round6
from soundings.schemas import LadderState
from soundings.schemas.config import STANDING_ORDERS_PATH
from soundings.schemas.scenario import Expected, Scenario

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTRA_SCENARIOS = ("BT-01.yaml",)
"""Scenario files outside the catalog glob the verifier also runs when present."""


@dataclass(frozen=True)
class ExpectationFailure:
    key: str
    expected: Any
    actual: Any
    detail: str = ""


@dataclass(frozen=True)
class Graded:
    """What the verifier reads from one run (tests build these directly)."""

    terminal_state: LadderState | None
    visited: tuple[LadderState, ...] = ()
    threat_mass: float | None = None
    top_hypothesis: str | None = None
    interruptions: tuple[int, ...] = ()
    probes: tuple[int, ...] = ()
    has_search_box: bool = False
    box_contains_truth: bool | None = None
    lineage: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)


def _gt_contacts(result: Any) -> list[str]:
    truth = result.sim_extended.contact_truth if result.sim_extended else result.sim.contact_truth
    cases = {c.case_id: c for c in result.cases}
    ids = []
    for cid, case_id in result.contact_cases.items():
        if cid in truth and not truth[cid].gt_id.startswith("fa-"):
            ids.append((cases[case_id].opened_tick, cid))
    return [cid for _, cid in sorted(ids)]


def grade(result: Any) -> Graded:
    """Grade a ``RunResultV3`` (see the module docstring)."""
    lineage = _gt_contacts(result)
    if not lineage:
        return Graded(terminal_state=None)
    end = lineage[-1]
    checkpoints = sorted(
        (cp.tick, cid, cp.seq, cp.to_state)
        for cid in lineage
        if cid in result.ladder
        for cp in result.ladder[cid].checkpoints
    )
    visited = tuple(s for *_, s in checkpoints)
    rec = result.recommendations.get(result.contact_cases.get(end, ""))
    probes = []
    for cid in lineage:
        r = result.recommendations.get(result.contact_cases.get(cid, ""))
        probes.append(0 if r is None else int(r.probes_used))
    sim = result.sim_extended or result.sim
    gt_id = sim.contact_truth[end].gt_id
    truth = {p.tick: p for p in sim.truth_paths.get(gt_id, [])}
    boxes = [p for p in result.sitrep_packets if p.contact_id == end and p.sitrep.search_box]
    contains: bool | None = None
    if boxes:
        contains = True
        for p in boxes:
            tick = int(round(p.t_s / float(sim.tick_s)))
            point = truth.get(tick)
            box = p.sitrep.search_box.box
            contains = contains and point is not None and box.contains(point.lat, point.lon)
    return Graded(
        terminal_state=result.ladder[end].handling_outcome if end in result.ladder else None,
        visited=visited,
        threat_mass=None if rec is None else round6(rec.threat_mass),
        top_hypothesis=None if rec is None else str(rec.top.value),
        interruptions=tuple(int(result.contact_interruptions.get(c, 0)) for c in lineage),
        probes=tuple(probes),
        has_search_box=bool(boxes),
        box_contains_truth=contains,
        lineage=tuple(lineage),
    )


def _in_order(visited: Sequence[LadderState], wanted: Sequence[LadderState]) -> bool:
    i = 0
    for state in visited:
        if i < len(wanted) and state is wanted[i]:
            i += 1
    return i == len(wanted)


def check_expected(result: Any, scenario: Scenario | Expected) -> list[ExpectationFailure]:
    """Every key of ``expected`` the run misses (empty: the run meets the block)."""
    exp = scenario.expected if isinstance(scenario, Scenario) else scenario
    g = result if isinstance(result, Graded) else grade(result)
    out: list[ExpectationFailure] = []
    if g.terminal_state is not exp.terminal_state:
        out.append(ExpectationFailure("terminal_state", exp.terminal_state, g.terminal_state))
    if exp.passes_through and not _in_order(g.visited, exp.passes_through):
        out.append(ExpectationFailure("passes_through", list(exp.passes_through), list(g.visited)))
    if exp.min_threat_mass is not None and (
        g.threat_mass is None or g.threat_mass < exp.min_threat_mass
    ):
        out.append(ExpectationFailure("min_threat_mass", exp.min_threat_mass, g.threat_mass))
    if exp.top_hypothesis is not None and g.top_hypothesis != exp.top_hypothesis.value:
        out.append(ExpectationFailure("top_hypothesis", exp.top_hypothesis.value, g.top_hypothesis))
    if exp.max_human_interruptions is not None and any(
        n > exp.max_human_interruptions for n in g.interruptions
    ):
        out.append(
            ExpectationFailure(
                "max_human_interruptions", exp.max_human_interruptions, list(g.interruptions)
            )
        )
    if exp.max_probes is not None and any(n > exp.max_probes for n in g.probes):
        out.append(ExpectationFailure("max_probes", exp.max_probes, list(g.probes)))
    if exp.sitrep_has_search_box is not None and g.has_search_box != exp.sitrep_has_search_box:
        out.append(
            ExpectationFailure("sitrep_has_search_box", exp.sitrep_has_search_box, g.has_search_box)
        )
    if (
        exp.search_box_contains_truth is not None
        and bool(g.box_contains_truth) != exp.search_box_contains_truth
    ):
        out.append(
            ExpectationFailure(
                "search_box_contains_truth", exp.search_box_contains_truth, g.box_contains_truth
            )
        )
    return out


# --- the suite --------------------------------------------------------------------------------


def suite_paths(root: Path = REPO_ROOT / "scenarios") -> list[Path]:
    from soundings.sim.scenario import catalog_paths

    paths = list(catalog_paths(root))
    paths += [root / name for name in EXTRA_SCENARIOS if (root / name).exists()]
    return paths


def verify_scenario(
    path: str | Path,
    *,
    orders_path: str | Path | None = STANDING_ORDERS_PATH,
    unsigned: bool = False,
    db_dir: Path | None = None,
) -> dict[str, Any]:
    """Run one scenario through runner v3 at its own seed and check its ``expected`` block."""
    from soundings.runner import ScenarioRunnerV3

    runner = ScenarioRunnerV3(str(path))
    sid = runner.scenario.id
    db = None if db_dir is None else db_dir / f"{sid}.db"
    result = runner.run(orders_path=orders_path, unsigned=unsigned, db_url=db)
    g = grade(result)
    failures = check_expected(g, runner.scenario)
    return {
        "scenario": sid,
        "seed": runner.seed,
        "ok": not failures and result.audit_ok,
        "audit_ok": result.audit_ok,
        "audit_head": result.audit_head,
        "ladder_log_hash": result.ladder_log_hash,
        "checkpoint_hashes": {
            cid: [cp.hash for cp in run.checkpoints] for cid, run in result.ladder.items()
        },
        "sitrep_digests": [
            f"{p.contact_id}:{p.status.value}:{p.t_s}:{p.audit_seq}" for p in result.sitrep_packets
        ],
        "terminal_state": None if g.terminal_state is None else g.terminal_state.value,
        "lineage": list(g.lineage),
        "visited": [s.value for s in g.visited],
        "threat_mass": g.threat_mass,
        "interruptions": list(g.interruptions),
        "probes": list(g.probes),
        "failures": [asdict(f) for f in failures],
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m soundings.eval.catalog_verify", description=__doc__
    )
    ap.add_argument("--suite", choices=("catalog",), default="catalog")
    ap.add_argument("--orders", type=Path, default=STANDING_ORDERS_PATH)
    ap.add_argument("--scenario", action="append", default=[], help="limit to these ids")
    ap.add_argument("--json", action="store_true", help="one canonical JSON document")
    args = ap.parse_args(argv)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for path in suite_paths():
            if args.scenario and not any(path.name.startswith(s) for s in args.scenario):
                continue
            rows.append(verify_scenario(path, orders_path=args.orders, db_dir=Path(tmp)))
    ok = all(r["ok"] for r in rows)
    if args.json:
        sys.stdout.write(canonical_json({"ok": ok, "runs": rows}) + "\n")
    else:
        for r in rows:
            mark = "ok  " if r["ok"] else "FAIL"
            sys.stdout.write(
                f"{mark} {r['scenario']} seed={r['seed']} terminal={r['terminal_state']} "
                f"interruptions={r['interruptions']} audit={'ok' if r['audit_ok'] else 'broken'}"
                f"{'' if r['ok'] else ' ' + json.dumps(r['failures'])}\n"
            )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
