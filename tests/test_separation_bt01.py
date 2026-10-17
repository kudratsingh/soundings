"""WO-083 integration: separation advisories over a full run, signed and unsigned orders.

BT-01 (WO-080) at seed 7 through ``ScenarioRunner(...).run()`` and ``advise_run``. If the
committed BT-01 yields no advisory in a basin the test needs, one in-memory vessel transit
is added over a job's track (the committed file is unchanged). The S01 cases run the same
pipeline on the catalog's filed survey with one in-memory vessel, so the integration is
exercised even before BT-01 exists. Determinism: two in-process runs and two subprocesses
under ``PYTHONHASHSEED`` 0 and 1 give identical canonical JSON, guard hashes and audit
chain heads. No wall clock: time is ``tick * tick_s``.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from soundings.advisories.cpa import cpa_tcpa
from soundings.advisories.separation import (
    LABEL_OUT_OF_SCOPE,
    JobFix,
    JobTracks,
    SeparationReport,
    advise_run,
    in_water_jobs,
    load_advisory_config,
    report_interval_s,
)
from soundings.canonical import canonical_json, stable_hash
from soundings.ladder.audit import AuditKind, AuditLog
from soundings.registry.models import PlanView
from soundings.registry.store import plan_views_from_scenario
from soundings.runner import RunResult, ScenarioRunner
from soundings.schemas import LadderState
from soundings.schemas.geo import from_local_m
from soundings.schemas.harbor import load_harbor
from soundings.sim.detection import load_detection_params
from soundings.sim.scenario import resolve_scenario
from soundings.sim.types import SimRun
from tests.support.separation_fixtures import guards_for, signed_status, unsigned_status

REPO = Path(__file__).resolve().parent.parent
BT01 = REPO / "scenarios" / "BT-01.yaml"
SEED = 7
CFG = load_advisory_config()
HARBOR = load_harbor()
PASS_OFFSET_M = 40.0
"""The in-memory vessel passes this far north of the job (well inside ``separation_m``)."""
VESSEL_SPEED = 5.0


def _has_ais_field() -> bool:
    return "ais_tracks" in {f.name for f in dataclasses.fields(SimRun)}


needs_bt01 = pytest.mark.skipif(
    not (BT01.is_file() and _has_ais_field()),
    reason="BT-01 and SimRun.ais_tracks come from WO-080 part A (not on this branch yet)",
)


# --- in-memory vessel over a job's track --------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Report:
    """Stand-in with WO-080 ``AisReport``'s fields, used only when it is not importable."""

    ais_id: str
    tick: int
    t_s: float
    lat: float
    lon: float
    sog_mps: float
    cog_deg: float


def _report(**kw: Any) -> Any:
    try:
        from soundings.sim.types import AisReport  # type: ignore[attr-defined]
    except ImportError:
        return _Report(**kw)
    return AisReport(**kw)


class _SimWithAis:
    """A read-only view of a ``SimRun`` plus ``ais_tracks`` (before WO-080 adds the field)."""

    def __init__(self, sim: SimRun, ais_tracks: dict[str, tuple[Any, ...]]) -> None:
        self._sim = sim
        self.ais_tracks = ais_tracks

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)


def with_vessel(result: RunResult, job: JobFix, vessel_id: str) -> RunResult:
    """``result`` with one more AIS vessel heading east, passing ``PASS_OFFSET_M`` north of
    ``job``'s position at ``job.tick``, reporting every ``report_interval_s`` for 300 s
    before and after."""
    sim = result.sim
    tick_s = float(sim.tick_s)
    every = max(1, round(report_interval_s() / tick_s))
    span = round(300.0 / tick_s)
    reports = []
    for tick in range(max(0, job.tick - span), min(sim.n_ticks, job.tick + span + 1), every):
        dt_s = (tick - job.tick) * tick_s
        lat, lon = from_local_m(job.x_m + VESSEL_SPEED * dt_s, job.y_m + PASS_OFFSET_M)
        reports.append(
            _report(
                ais_id=vessel_id,
                tick=tick,
                t_s=round(tick * tick_s, 6),
                lat=round(lat, 6),
                lon=round(lon, 6),
                sog_mps=VESSEL_SPEED,
                cog_deg=90.0,
            )
        )
    tracks = dict(getattr(sim, "ais_tracks", None) or {})
    tracks[vessel_id] = tuple(reports)
    new_sim: Any = (
        dataclasses.replace(sim, ais_tracks=tracks)  # type: ignore[call-arg]
        if _has_ais_field()
        else _SimWithAis(sim, tracks)
    )
    return dataclasses.replace(result, sim=new_sim)


def _jobs_at(result: RunResult, plans: list[PlanView], tick: int) -> list[JobFix]:
    timeout = load_detection_params().track_timeout_ticks
    return in_water_jobs(result, plans, HARBOR, tick, timeout)


def _first_job(result: RunResult, plans: list[PlanView], basin: str | None = None) -> JobFix:
    """The first in-water job (optionally in ``basin``), scanning ticks in order."""
    tracks = JobTracks(result)
    timeout = load_detection_params().track_timeout_ticks
    for tick in range(result.sim.n_ticks // 4, result.sim.n_ticks):
        for j in in_water_jobs(result, plans, HARBOR, tick, timeout, tracks=tracks):
            if basin is None or j.basin == basin:
                return j
    raise AssertionError(f"no in-water job in {basin}")


# --- runs ---------------------------------------------------------------------------------


def _runner_kwargs() -> dict[str, Any]:
    params = inspect.signature(ScenarioRunner).parameters
    return {"skip_idle_ticks": True} if "skip_idle_ticks" in params else {}


@cache
def s01_case() -> tuple[RunResult, tuple[PlanView, ...]]:
    scenario = resolve_scenario("S01", HARBOR)
    result = ScenarioRunner(scenario, seed=1).run()
    plans = plan_views_from_scenario(scenario)
    job = _first_job(result, plans, "inner_harbor_west")
    return with_vessel(result, job, "ais-91"), tuple(plans)


@cache
def bt01_case() -> tuple[RunResult, tuple[PlanView, ...], tuple[str, ...]]:
    """BT-01 at seed 7, plus in-memory vessels only where the committed file has none."""
    scenario = resolve_scenario("BT-01", HARBOR)
    result = ScenarioRunner(scenario, seed=SEED, **_runner_kwargs()).run()
    plans = list(plan_views_from_scenario(scenario))
    added: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        report = advise(result, plans, signed_status(Path(tmp)), None)
    basins = {d.advisory.basin for d in report.decisions}
    if "inner_harbor_west" not in basins:
        result = with_vessel(result, _first_job(result, plans, "inner_harbor_west"), "ais-91")
        added.append("ais-91")
    if not basins - {"inner_harbor_west"}:
        result = with_vessel(result, _first_job(result, plans, "main_channel"), "ais-92")
        added.append("ais-92")
    return result, tuple(plans), tuple(added)


def advise(
    result: RunResult, plans: list[PlanView] | tuple[PlanView, ...], status: Any, audit: Any
) -> SeparationReport:
    epoch = None  # orders checked at load time (scenario hour 07:00, inside the window)
    return advise_run(
        result, list(plans), HARBOR, guards_for(status, epoch=epoch), audit, "run-sep", CFG
    )


def _signed_report(result: RunResult, plans: Any, tmp: Path) -> tuple[SeparationReport, AuditLog]:
    audit = AuditLog(":memory:")
    return advise(result, plans, signed_status(tmp), audit), audit


def _check_report(
    report: SeparationReport, result: RunResult, plans: Any, vessels: set[str]
) -> None:
    assert report.decisions, "the integration must not be vacuous"
    assert report.issued >= 1
    issued_basins = {d.advisory.basin for d in report.decisions if d.status == "issued"}
    assert "inner_harbor_west" in issued_basins
    for d in report.decisions:
        a = d.advisory
        assert a.vessel_id in vessels, a.vessel_id
        in_water = {j.plan_id for j in _jobs_at(result, list(plans), a.tick)}
        assert a.plan_id in in_water, (a.plan_id, a.tick)
        f = a.inputs
        res = cpa_tcpa(
            (f.own_x_m, f.own_y_m),
            (f.own_vx_mps, f.own_vy_mps),
            (f.other_x_m, f.other_y_m),
            (f.other_vx_mps, f.other_vy_mps),
        )
        assert abs(res.cpa_m - a.cpa_m) <= 1e-6 and abs(res.tcpa_s - a.tcpa_s) <= 1e-6
        assert a.cpa_m < CFG.separation_m and 0.0 < a.tcpa_s < CFG.advisory_window_s
        if d.status == "issued":
            assert a.basin == "inner_harbor_west"  # the orders' one slow basin
            assert d.granted_scope is not None and d.granted_scope.action == "advisory_one_basin"
        elif a.basin != "inner_harbor_west":
            assert d.decision.reason == "basin_out_of_standing_scope"
            assert d.label == LABEL_OUT_OF_SCOPE


def _check_unsigned(result: RunResult, plans: Any, signed: SeparationReport, tmp: Path) -> None:
    audit = AuditLog(":memory:")
    report = advise(result, plans, unsigned_status(tmp), audit)
    assert [d.advisory for d in report.decisions] == [d.advisory for d in signed.decisions]
    assert report.issued == 0 and all(d.status == "blocked" for d in report.decisions)
    assert audit.rows(kind=AuditKind.ACTION) == []
    assert len(audit.rows(kind=AuditKind.GUARD_DECISION)) == len(report.decisions)


# --- S01: the pipeline before BT-01 -------------------------------------------------------


def test_s01_injected_vessel(tmp_path: Path) -> None:
    result, plans = s01_case()
    before = (result.interruptions, [c.state for c in result.cases])
    report, audit = _signed_report(result, plans, tmp_path)
    _check_report(report, result, plans, {"ais-91"})
    assert audit.verify().ok
    assert len(audit.rows(kind=AuditKind.ACTION)) == report.issued
    _check_unsigned(result, plans, report, tmp_path)
    # read-only over the run: no case, state or interruption changes
    assert (result.interruptions, [c.state for c in result.cases]) == before


def test_vessel_fixes_read_ais_only() -> None:
    src = (REPO / "soundings" / "advisories" / "separation.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert not {"truth_paths", "contact_truth"} & names
    assert "truth_paths" not in src and "contact_truth" not in src
    assert "ais_tracks" in src


# --- BT-01 --------------------------------------------------------------------------------


@needs_bt01
def test_bt01_advisories(tmp_path: Path) -> None:
    result, plans, added = bt01_case()
    vessels = {f"ais-0{i}" for i in (1, 2, 3)} | set(added)
    report, audit = _signed_report(result, plans, tmp_path)
    _check_report(report, result, plans, vessels)
    assert audit.verify().ok
    _check_unsigned(result, plans, report, tmp_path)


@needs_bt01
def test_bt01_terminal_states_unchanged() -> None:
    result, _, _ = bt01_case()
    filed = {}
    for c in result.cases:
        if c.outcome is not None and c.outcome.plan_id is not None:
            filed.setdefault(c.outcome.plan_id, []).append(c.state)
    cleared = [p for p, states in filed.items() if set(states) == {LadderState.CLEARED_AT_ASK}]
    assert len(cleared) == 39
    if hasattr(result, "recommendations"):  # runner v2 (WO-033): the rogue is handed off
        assert any(c.state is LadderState.HANDED_OFF for c in result.cases)


# --- determinism (CLAUDE.md 2.4) ----------------------------------------------------------


def fingerprint(which: str) -> str:
    """Canonical JSON of the report, every guard ``inputs_hash`` and the audit chain head."""
    with tempfile.TemporaryDirectory() as tmp:
        if which == "bt01":
            result, plans, _ = bt01_case()
        else:
            result, plans = s01_case()
        report, audit = _signed_report(result, plans, Path(tmp))
    hashes = [d.decision.inputs_hash for d in report.decisions]
    return canonical_json(
        {
            "report": report,
            "report_hash": stable_hash(report),
            "inputs_hashes": hashes,
            "audit_head": audit.head(),
        }
    )


def main() -> None:
    print(fingerprint(sys.argv[1] if len(sys.argv) > 1 else "s01"))


def _subprocess_runs(which: str) -> Iterator[str]:
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "MODEL_MODE": "replay"}
        proc = subprocess.run(
            [sys.executable, "-c", "from tests.test_separation_bt01 import main; main()", which],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        yield proc.stdout.strip()


def _assert_deterministic(which: str) -> None:
    first = fingerprint(which)
    assert fingerprint(which) == first
    for out in _subprocess_runs(which):
        assert out == first


def test_s01_deterministic() -> None:
    _assert_deterministic("s01")


@needs_bt01
def test_bt01_deterministic() -> None:
    _assert_deterministic("bt01")


__all__ = ["fingerprint", "main", "with_vessel"]
