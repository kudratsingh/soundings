"""WO-021 integration: the hail on real catalog runs (WO-011 ``run_scenario``, own seeds).

Exit tests: ``-k s02``, ``-k s04_s05``, ``-k read_only``, ``-k deterministic``.
False alarms are observed ``c-NNNN`` ids whose ``contact_truth`` ``gt_id`` is ``fa-*``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from functools import cache

from soundings.canonical import canonical_json, stable_hash
from soundings.challenge.hail import (
    HailConfig,
    HailResult,
    HailSimulator,
    hail_evidence_rows,
    load_known_operators,
)
from soundings.schemas import HailReply, Scenario
from soundings.schemas._base import REPO_ROOT
from soundings.sim.hashing import track_hash
from soundings.sim.run import run_scenario
from soundings.sim.scenario import resolve_scenario
from soundings.sim.types import SimRun

CATALOG = ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08")


@cache
def scenario_and_run(ref: str) -> tuple[Scenario, SimRun]:
    scenario = resolve_scenario(ref)
    return scenario, run_scenario(scenario)


def simulator(ref: str, *, seed: int | None = None, cfg: HailConfig | None = None) -> HailSimulator:
    scenario, run = scenario_and_run(ref)
    return HailSimulator(
        scenario,
        run,
        seed=scenario.seed if seed is None else seed,
        known=load_known_operators(scenario=scenario),
        cfg=cfg if cfg is not None else HailConfig(),
    )


def observed_ids(run: SimRun, gt_id: str) -> list[str]:
    return sorted(cid for cid, t in run.contact_truth.items() if t.gt_id == gt_id)


def false_alarm_ids(run: SimRun) -> list[str]:
    return sorted(cid for cid, t in run.contact_truth.items() if t.gt_id.startswith("fa-"))


def first_tick(run: SimRun, cid: str) -> int:
    return run.tracks[cid].points[0].tick


# --- s02 ---------------------------------------------------------------------------------


def test_s02_replies_before_silence_from_plan_modem() -> None:
    scenario, run = scenario_and_run("S02")
    ids = observed_ids(run, "gt-1")
    early = [cid for cid in ids if first_tick(run, cid) < 300]
    assert early, "S02 gt-1 must be observed before tick 300"
    assert scenario.contacts[0].registry.modem_id is None  # the modem comes from the plan
    assert scenario.plan("P-2026-1017-003").modem_id == "MOD-4471"
    for cid in early:
        result = simulator("S02").send(cid, first_tick(run, cid))
        assert result.hail_reply is HailReply.VALID
        assert result.replied is True
        assert result.modem_id == "MOD-4471"
        assert result.reply_tick == result.sent_tick + 10


def test_s02_silent_at_400_timeout_or_none() -> None:
    _, run = scenario_and_run("S02")
    for cid in observed_ids(run, "gt-1"):
        after = simulator("S02").send(cid, 400, previously_replied=True)
        assert after.hail_reply is HailReply.TIMEOUT
        assert after.replied is False and after.modem_id is None
        fresh = simulator("S02").send(cid, 400)
        assert fresh.hail_reply is HailReply.NONE
        assert fresh.replied is False and fresh.modem_id is None


def test_s02_rows_tell_the_gone_silent_story() -> None:
    _, run = scenario_and_run("S02")
    cid = observed_ids(run, "gt-1")[0]
    sim = simulator("S02")
    before = hail_evidence_rows(sim.send(cid, first_tick(run, cid)), tick_s=run.tick_s)
    after = hail_evidence_rows(sim.send(cid, 400, previously_replied=True), tick_s=run.tick_s)
    assert [r.value for r in before[1:3]] == ["valid", "MOD-4471"]
    assert [r.value for r in after[1:3]] == ["timeout", "unknown"]


# --- s04_s05 -----------------------------------------------------------------------------


def test_s04_s05_known_operator_replies_valid() -> None:
    _, run = scenario_and_run("S04")
    ids = observed_ids(run, "gt-1")
    assert ids
    for cid in ids:
        result = simulator("S04").send(cid, first_tick(run, cid))
        assert result.hail_reply is HailReply.VALID
        assert result.modem_id == "MOD-2290"
        assert result.operator_known is True
        assert result.operator == "Port Survey Co (demo)"


def test_s04_s05_unauthorized_contact_returns_none() -> None:
    _, run = scenario_and_run("S05")
    ids = observed_ids(run, "gt-1")
    assert ids
    for cid in ids:
        for tick in (first_tick(run, cid), run.tracks[cid].points[-1].tick):
            result = simulator("S05").send(cid, tick)
            assert result.hail_reply is HailReply.NONE
            assert result.operator_known is False


def test_s04_s05_s01_false_alarms_return_none() -> None:
    _, run = scenario_and_run("S01")
    fas = false_alarm_ids(run)
    assert fas, "S01 must contain false alarms"
    for cid in fas:
        result = simulator("S01").send(cid, first_tick(run, cid))
        assert result.hail_reply is HailReply.NONE
        assert result.modem_id is None


# --- read_only ---------------------------------------------------------------------------


def test_read_only_hundred_hails_leave_runs_unchanged() -> None:
    before = {ref: track_hash(scenario_and_run(ref)[1]) for ref in CATALOG}
    truth_before = {ref: canonical_json(scenario_and_run(ref)[1].contact_truth) for ref in CATALOG}
    sims = {ref: simulator(ref) for ref in CATALOG}
    targets = [
        (ref, cid) for ref in CATALOG for cid in sorted(scenario_and_run(ref)[1].contact_truth)
    ]
    sent = 0
    i = 0
    while sent < 100:
        ref, cid = targets[i % len(targets)]
        run = scenario_and_run(ref)[1]
        tick = (i * 37) % run.n_ticks
        sims[ref].send(cid, tick, previously_replied=bool(i % 2))
        sent += 1
        i += 1
    assert sent == 100
    after = {ref: track_hash(scenario_and_run(ref)[1]) for ref in CATALOG}
    truth_after = {ref: canonical_json(scenario_and_run(ref)[1].contact_truth) for ref in CATALOG}
    assert after == before
    assert truth_after == truth_before
    # and a fresh run of each scenario still hashes the same
    for ref in CATALOG:
        scenario = resolve_scenario(ref)
        assert track_hash(run_scenario(scenario)) == before[ref]


# --- deterministic -----------------------------------------------------------------------

LOSSY = HailConfig(reply_loss_prob=0.5)


def hail_sweep(sim: HailSimulator, run: SimRun) -> list[HailResult]:
    """Hail every observed contact every 10 ticks, in (tick, contact id) order."""
    return [sim.send(cid, tick) for tick in range(0, run.n_ticks, 10) for cid in sorted(run.tracks)]


def test_deterministic_two_simulators_identical() -> None:
    _, run = scenario_and_run("S02")
    a = hail_sweep(simulator("S02", cfg=LOSSY), run)
    b = hail_sweep(simulator("S02", cfg=LOSSY), run)
    assert canonical_json(a) == canonical_json(b)
    rows_a = [r.row_id for res in a for r in hail_evidence_rows(res, tick_s=run.tick_s)]
    rows_b = [r.row_id for res in b for r in hail_evidence_rows(res, tick_s=run.tick_s)]
    assert rows_a == rows_b
    buckets = {r.hail_reply for r in a}
    assert HailReply.VALID in buckets and HailReply.NONE in buckets  # loss really applies


def test_deterministic_seed_sensitive() -> None:
    _, run = scenario_and_run("S02")
    three = hail_sweep(simulator("S02", seed=3, cfg=LOSSY), run)
    four = hail_sweep(simulator("S02", seed=4, cfg=LOSSY), run)
    assert canonical_json(three) != canonical_json(four)
    # with no loss the seed has nothing to change
    lossless_three = hail_sweep(simulator("S02", seed=3), run)
    lossless_four = hail_sweep(simulator("S02", seed=4), run)
    assert canonical_json(lossless_three) == canonical_json(lossless_four)


SUBPROCESS_CODE = textwrap.dedent(
    """
    from soundings.canonical import stable_hash
    from soundings.challenge.hail import HailConfig, HailSimulator, hail_evidence_rows
    from soundings.challenge.hail import load_known_operators
    from soundings.sim.run import run_scenario
    from soundings.sim.scenario import resolve_scenario

    scenario = resolve_scenario("S02")
    run = run_scenario(scenario)
    sim = HailSimulator(
        scenario,
        run,
        seed=scenario.seed,
        known=load_known_operators(scenario=scenario),
        cfg=HailConfig(reply_loss_prob=0.5),
    )
    results = [
        sim.send(cid, tick) for tick in range(0, run.n_ticks, 10) for cid in sorted(run.tracks)
    ]
    rows = [r.row_id for res in results for r in hail_evidence_rows(res, tick_s=run.tick_s)]
    print(len(results), stable_hash(results), stable_hash(rows))
    """
)


def _run_with_hashseed(hashseed: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": hashseed, "MODEL_MODE": "replay"}
    proc = subprocess.run(
        [sys.executable, "-c", SUBPROCESS_CODE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return proc.stdout


def test_deterministic_across_pythonhashseed() -> None:
    out0 = _run_with_hashseed("0")
    out1 = _run_with_hashseed("1")
    assert out0 == out1
    _, run = scenario_and_run("S02")
    here = hail_sweep(simulator("S02", cfg=LOSSY), run)
    rows = [r.row_id for res in here for r in hail_evidence_rows(res, tick_s=run.tick_s)]
    assert out0.split() == [str(len(here)), stable_hash(here), stable_hash(rows)]
