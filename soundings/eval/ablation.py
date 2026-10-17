"""Planner ablation (WO-051 addendum item 3; entry point built by WO-093).

``python -m soundings.eval.ablation --planners llm,deterministic,random
--scenarios S04-S08,BT-01 --mode replay`` runs each scenario through runner v3 (signed
standing orders, the scenario's own seed, SITREPs from the template) once per planner arm
and prints probes used, median seconds seen to handled, missed threats and interruptions,
then the sentence for the report's "Where the LLM layer added nothing" section.

Arms (every one goes through the same guarded investigation; nothing here is a control):

* ``deterministic``: the WO-031 planner (``llm_roles=False``).
* ``llm``: ``llm_roles=True`` over the recorded fixtures, replay only. With no fixtures
  every call is a replay miss and falls back to the deterministic choice, so the arm
  equals ``deterministic``; the sentence says so plainly.
* ``random``: ``llm_roles=True`` over a seeded ``RandomPlannerStore`` that answers each
  ``probe_planner`` request with a uniformly chosen probe the guard would accept
  (allowlisted, permitted at the current rung, not yet run, within the remaining probe,
  paid and interruption budget). The guard and the post-guard re-check still apply, and
  after ``llm_calls_per_contact`` picks the planner reverts to the deterministic choice.

Metrics (method line printed with the table): probes = investigation probes executed
(cached repeats excluded); seconds seen to handled = median over closed cases of
``(closed_tick - opened_tick) * tick_s``; missed threats = WO-050's rule (a ground-truth
threat none of whose observed ids entered HOLDING or HANDED_OFF, undetected included);
interruptions = runner v3's count (CLAUDE.md section 8 A5). Deterministic: seeded runs,
canonical output, no clock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import statistics
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from soundings.canonical import round6
from soundings.eval.policies import THREAT_HYPOTHESES
from soundings.llm.client import FixtureStore, ModelMode, ModelOutput, ReplayMiss
from soundings.llm.fixtures import BT01_ID, CapturingStore, bt01_path, quiet_replay_misses
from soundings.llm.roles import ROLE_PROBE_PLANNER
from soundings.schemas import LadderState, Rung

__all__ = [
    "ARMS",
    "AblationReport",
    "AblationRow",
    "RandomPlannerStore",
    "ablation_sentence",
    "ablation_table",
    "expand_scenarios",
    "main",
    "run_ablation",
]

ARMS: Final = ("llm", "deterministic", "random")
RANDOM_SEED: Final = 93
SCENARIO_ROOT: Final = Path(__file__).resolve().parents[2] / "scenarios"
CAUGHT: Final = frozenset({LadderState.HOLDING, LadderState.HANDED_OFF})
METHOD: Final = (
    "method: runner v3, signed standing orders, each scenario at its own seed, template"
    " SITREPs; probes = investigation probes executed (cached repeats excluded); s seen->"
    "handled = median over closed cases of (closed_tick - opened_tick) * tick_s; missed"
    " threats = WO-050 rule (never HOLDING or HANDED_OFF, undetected included);"
    " interruptions = runner v3 (CLAUDE.md 8 A5); random arm seed 93"
)
EXIT_REFUSED: Final = 3


# --- the random arm ----------------------------------------------------------------------


class RandomPlannerStore(FixtureStore):
    """Answers ``probe_planner`` with a seeded random permitted probe; nothing else.

    The choice depends only on ``seed`` and the request's canonical inputs (through the
    fixture key), so a run is reproducible and independent of call order.
    """

    def __init__(self, seed: int = RANDOM_SEED, root: Path | None = None) -> None:
        super().__init__(root)
        self.seed = int(seed)
        self.answered = 0

    def load(self, role: str, inputs: Any) -> ModelOutput | None:
        if role != ROLE_PROBE_PLANNER or not isinstance(inputs, Mapping):
            return None
        options = permitted_probes(inputs)
        if not options:
            return None
        from soundings.llm.client import fixture_key

        digest = hashlib.sha256(f"{self.seed}:{fixture_key(role, inputs)}".encode()).digest()
        pick = random.Random(int.from_bytes(digest[:8], "big")).choice(options)
        probs = inputs.get("posterior", {}).get("posterior", {})
        top = sorted(probs, key=lambda h: (-float(probs[h]), h))[:2]
        self.answered += 1
        return {
            "next_probe": pick,
            "why": "Random probe order (ablation arm).",
            "hypotheses_to_discriminate": top or ["unknown"],
            "question_for_human": None,
        }


def permitted_probes(inputs: Mapping[str, Any]) -> list[str]:
    """Probes in the request's allowlist the guard would accept, in allowlist order."""
    order = [r.value for r in Rung]
    rung = str(inputs.get("current_rung", ""))
    if rung not in order:
        return []
    left = inputs.get("remaining_budget", {})
    if int(left.get("probes", 0)) <= 0:
        return []
    ran = set(inputs.get("already_run", ()))
    out: list[str] = []
    for a in inputs.get("allowlist", ()):
        need = str(a.get("rung_required", ""))
        if a.get("probe_id") in ran or need not in order:
            continue
        if order.index(need) > order.index(rung):
            continue
        if float(a.get("cost_usd", 0)) > 0 and int(left.get("paid_probes", 0)) <= 0:
            continue
        if int(a.get("interruption", 0)) > 0 and int(left.get("interruptions", 0)) <= 0:
            continue
        out.append(str(a["probe_id"]))
    return out


# --- running -----------------------------------------------------------------------------


@dataclass(frozen=True)
class AblationRow:
    planner: str
    scenario: str
    probes: int
    s_seen_to_handled: float | None
    missed_threats: int
    interruptions: int
    llm_choices: int


@dataclass(frozen=True)
class AblationReport:
    planners: tuple[str, ...]
    scenarios: tuple[str, ...]
    pending: tuple[str, ...]
    rows: tuple[AblationRow, ...]
    llm_fixture_hits: int

    def row(self, planner: str, scenario: str) -> AblationRow:
        for r in self.rows:
            if r.planner == planner and r.scenario == scenario:
                return r
        raise KeyError((planner, scenario))

    def to_json(self) -> dict[str, Any]:
        return {
            "planners": list(self.planners),
            "scenarios": list(self.scenarios),
            "pending": list(self.pending),
            "llm_fixture_hits": self.llm_fixture_hits,
            "rows": [asdict(r) for r in self.rows],
            "sentence": ablation_sentence(self),
            "method": METHOD,
        }


def expand_scenarios(
    spec: str, *, root: Path = SCENARIO_ROOT
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``S04-S08,BT-01`` -> (runnable ids in order, pending ids). BT-01 is pending when
    its file is not on this checkout."""
    out: list[str] = []
    pending: list[str] = []
    for part in (p.strip() for p in spec.split(",")):
        if not part:
            continue
        m = re.fullmatch(r"S(\d{2})-S(\d{2})", part)
        ids = [f"S{i:02d}" for i in range(int(m[1]), int(m[2]) + 1)] if m else [part]
        for sid in ids:
            if sid == BT01_ID and bt01_path(root) is None:
                pending.append(sid)
            elif sid not in out:
                out.append(sid)
    return tuple(out), tuple(pending)


def _source(sid: str, root: Path) -> str:
    if sid == BT01_ID:
        path = bt01_path(root)
        return str(path) if path is not None else sid
    return sid


def _metrics(planner: str, sid: str, result: Any, scenario: Any) -> AblationRow:
    tick_s = float(result.sim.tick_s)
    spans = [
        (c.closed_tick - c.opened_tick) * tick_s for c in result.cases if c.closed_tick is not None
    ]
    sim = result.sim_extended or result.sim
    truths = sim.contact_truth
    threat_gt: dict[str, bool] = {}
    for cid in sorted(truths):
        ct = truths[cid]
        if ct.hypothesis in THREAT_HYPOTHESES and not ct.gt_id.startswith("fa-"):
            caught = cid in result.ladder and any(
                cp.to_state in CAUGHT for cp in result.ladder[cid].checkpoints
            )
            threat_gt[ct.gt_id] = threat_gt.get(ct.gt_id, False) or caught
    own = [c.id for c in scenario.contacts] if scenario.ground_truth in THREAT_HYPOTHESES else []
    for gt in own:
        threat_gt.setdefault(gt, False)  # a threat with no observed id was never detected
    return AblationRow(
        planner=planner,
        scenario=sid,
        probes=sum(1 for c in result.probe_calls if not c.cached),
        s_seen_to_handled=round6(statistics.median(spans)) if spans else None,
        missed_threats=sum(1 for caught in threat_gt.values() if not caught),
        interruptions=int(result.interruptions),
        llm_choices=sum(1 for c in result.planner_choices if c.source == "llm"),
    )


def run_ablation(
    planners: Sequence[str] = ARMS,
    scenarios: Iterable[str] = ("S04", "S05", "S06", "S07", "S08"),
    *,
    mode: str = "replay",
    fixture_root: Path | None = None,
    pending: Sequence[str] = (),
    scenario_root: Path = SCENARIO_ROOT,
    seed: int = RANDOM_SEED,
) -> AblationReport:
    """Run every (planner, scenario) pair; ``mode`` must be ``replay``."""
    if ModelMode(mode) is not ModelMode.REPLAY:
        raise ValueError("the ablation runs in replay mode only (CLAUDE.md 2.3)")
    unknown = [p for p in planners if p not in ARMS]
    if unknown:
        raise ValueError(f"unknown planner arm(s): {', '.join(unknown)}")
    from soundings.runner import ScenarioRunnerV3  # lazy: the runner reaches the sim

    ids = tuple(scenarios)
    rows: list[AblationRow] = []
    hits = 0
    with tempfile.TemporaryDirectory() as tmp:
        violations = Path(tmp) / "VIOLATIONS.md"
        for planner in planners:
            for sid in ids:
                store: FixtureStore | None = None
                if planner == "llm":
                    store = CapturingStore(fixture_root)
                elif planner == "random":
                    store = RandomPlannerStore(seed)
                runner = ScenarioRunnerV3(_source(sid, scenario_root))
                result = runner.run(
                    llm_roles=planner != "deterministic",
                    fixture_store=store,
                    violations_path=violations,
                )
                if isinstance(store, CapturingStore):
                    hits += sum(1 for c in store.captured if c.hit and c.role == ROLE_PROBE_PLANNER)
                rows.append(_metrics(planner, sid, result, runner.scenario))
    return AblationReport(tuple(planners), ids, tuple(pending), tuple(rows), hits)


# --- output ------------------------------------------------------------------------------

_FIELDS: Final = ("probes", "s_seen_to_handled", "missed_threats", "interruptions")


def _same(a: AblationRow, b: AblationRow) -> bool:
    return all(getattr(a, f) == getattr(b, f) for f in _FIELDS)


def ablation_sentence(report: AblationReport) -> str:
    """The "Where the LLM layer added nothing" sentence, computed from the rows."""
    if "llm" not in report.planners or "deterministic" not in report.planners:
        return "The ablation needs both the llm and the deterministic arm to compare."
    scope = ", ".join(report.scenarios) + (
        f" ({', '.join(report.pending)} pending)" if report.pending else ""
    )
    same = [
        s for s in report.scenarios if _same(report.row("llm", s), report.row("deterministic", s))
    ]
    differ = [s for s in report.scenarios if s not in same]
    llm_picks = sum(report.row("llm", s).llm_choices for s in report.scenarios)
    if report.llm_fixture_hits == 0 or llm_picks == 0:
        return (
            f"No recorded planner reply was used on {scope}: every LLM planner call fell back"
            " to the deterministic choice, so the LLM planner equals the deterministic"
            " planner on every metric. The LLM layer added nothing here."
        )
    if not differ:
        return (
            f"With {llm_picks} recorded planner picks accepted by the guard on {scope}, the"
            " LLM planner matched the deterministic planner on probes, time to handled,"
            " missed threats and interruptions. The LLM layer added nothing measurable here."
        )
    parts = []
    for s in differ:
        a, b = report.row("deterministic", s), report.row("llm", s)
        changes = [
            f"{f.replace('_', ' ')} {getattr(a, f)} -> {getattr(b, f)}"
            for f in _FIELDS
            if getattr(a, f) != getattr(b, f)
        ]
        parts.append(f"{s}: " + ", ".join(changes))
    nothing = ", ".join(same) if same else "no scenario"
    return (
        f"The LLM planner (deterministic -> llm) changed {'; '.join(parts)}."
        f" It added nothing on {nothing}."
    )


def ablation_table(report: AblationReport) -> str:
    head = (
        f"{'planner':<14} {'scenario':<8} {'probes':>6} {'s seen->handled':>15}"
        f" {'missed threats':>14} {'interruptions':>13} {'llm picks':>9}"
    )
    lines = [head, "-" * len(head)]
    for r in report.rows:
        seen = "-" if r.s_seen_to_handled is None else f"{r.s_seen_to_handled:.1f}"
        lines.append(
            f"{r.planner:<14} {r.scenario:<8} {r.probes:>6} {seen:>15}"
            f" {r.missed_threats:>14} {r.interruptions:>13} {r.llm_choices:>9}"
        )
    if report.pending:
        lines.append(f"pending (not on this checkout): {', '.join(report.pending)}")
    lines.append(METHOD)
    lines.append("")
    lines.append("Where the LLM layer added nothing: " + ablation_sentence(report))
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m soundings.eval.ablation",
        description="Planner ablation: llm vs deterministic vs random probe order (replay).",
    )
    parser.add_argument("--planners", default=",".join(ARMS))
    parser.add_argument("--scenarios", default="S04-S08,BT-01")
    parser.add_argument("--mode", default="replay")
    parser.add_argument("--fixture-root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None, help="also write the JSON here")
    args = parser.parse_args(argv)
    if args.mode != ModelMode.REPLAY.value:
        print("the ablation runs in replay mode only (CLAUDE.md 2.3)", file=sys.stderr)
        return EXIT_REFUSED
    planners = [p.strip() for p in args.planners.split(",") if p.strip()]
    scenarios, pending = expand_scenarios(args.scenarios)
    try:
        with quiet_replay_misses():
            report = run_ablation(
                planners, scenarios, mode=args.mode, fixture_root=args.fixture_root, pending=pending
            )
    except ReplayMiss as exc:  # under SOUNDINGS_CI a replay miss raises (WORKFLOW 8)
        print(f"ablation needs SOUNDINGS_CI unset for missing fixtures ({exc})", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.write(ablation_table(report))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report.to_json(), sort_keys=True, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
