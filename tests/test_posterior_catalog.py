"""WO-030 integration: score the real catalog end to end (sim -> evidence -> posterior).

Ground truth is read here only to pick which observed contact to grade; the scorer sees
the evidence table alone. Registry rows are still Phase 2 placeholders (``unknown``) and
are skipped. S05 adds the WO-021 acoustic hail (merged on main) on the closing prefix:
without a hail the closing vehicle is indistinguishable from a filed survey on kinematics
and acoustics at the WO-012 derived-row confidences (DECISIONS 2026-10-17, WO-030).
"""

from __future__ import annotations

import math
from functools import cache

import pytest

from soundings.challenge.hail import HailSimulator, hail_evidence_rows
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import build_evidence
from soundings.investigation.pol import load_pol_table
from soundings.investigation.posterior import (
    STRESS_TEST_LABEL,
    PosteriorReport,
    load_likelihood_tables,
    load_prior_config,
    score_contact,
)
from soundings.schemas.enums import Hypothesis
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.schemas.scenario import Scenario
from soundings.sim.current import load_current_field, tide_phase_for_hour
from soundings.sim.run import run_scenario, truncate_run
from soundings.sim.scenario import resolve_scenario
from soundings.sim.types import SimRun

CATALOG = ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08")
S05_CLOSING_TICKS = (240, 300)  # contact still closing on the fuel berth (parks later)

TABLES = load_likelihood_tables()
PRIORS = load_prior_config()


@cache
def _harbor() -> Harbor:
    return load_harbor()


@cache
def _scenario(sid: str) -> Scenario:
    return resolve_scenario(sid, _harbor())


@cache
def _run(sid: str, seed: int) -> SimRun:
    return run_scenario(_scenario(sid), seed=seed, harbor=_harbor())


def _evidence(sid: str, run: SimRun) -> EvidenceTable:
    field = load_current_field()
    return build_evidence(
        run,
        _harbor(),
        field,
        load_pol_table(),
        hour_of_day=_scenario(sid).hour_of_day,
        tide_phase_for_hour=lambda hour: tide_phase_for_hour(hour, field),
    )


def _truth_ids(run: SimRun, hypothesis: Hypothesis) -> list[str]:
    """Observed ids of the scenario contact with this ground truth, longest track first."""
    ids = [
        cid
        for cid, truth in run.contact_truth.items()
        if truth.hypothesis is hypothesis and not truth.gt_id.startswith("fa-")
    ]
    return sorted(ids, key=lambda cid: (-len(run.tracks[cid].points), cid))


def _check(report: PosteriorReport) -> None:
    assert abs(math.fsum(report.posterior.probs.values()) - 1.0) <= 1e-9
    assert report.label == STRESS_TEST_LABEL
    assert report.to_dict()["label"] == STRESS_TEST_LABEL
    # Registry is a Phase 2 placeholder in the WO-012 table: skipped, never imputed.
    assert report.skipped.get("registry_match") == "unknown"


@pytest.mark.parametrize("sid", CATALOG)
def test_catalog_reports_sum_to_one_with_label(sid: str) -> None:
    run = _run(sid, _scenario(sid).seed)
    table = _evidence(sid, run)
    assert table.contacts()
    for cid in table.contacts():
        _check(score_contact(table, cid, TABLES, PRIORS))


@pytest.mark.parametrize(
    ("sid", "seed", "expected"),
    [("S07", 17, Hypothesis.BIOLOGIC), ("S08", 19, Hypothesis.DEBRIS_OR_DRIFT)],
)
def test_catalog_benign_tops(sid: str, seed: int, expected: Hypothesis) -> None:
    run = _run(sid, seed)
    table = _evidence(sid, run)
    ids = _truth_ids(run, expected)
    assert ids, f"{sid}: no observed contact for {expected.value}"
    for cid in ids:
        report = score_contact(table, cid, TABLES, PRIORS)
        _check(report)
        assert report.posterior.top() is expected, (cid, report.to_dict())
        assert report.posterior.threat_mass() < 0.2


@pytest.mark.parametrize("ticks", S05_CLOSING_TICKS)
def test_catalog_s05_closing_contact_is_uuv(ticks: int) -> None:
    full = _run("S05", 11)
    run = truncate_run(full, ticks)
    table = _evidence("S05", run)
    cid = _truth_ids(run, Hypothesis.UNAUTHORIZED_UUV)[0]
    before = score_contact(table, cid, TABLES, PRIORS)
    _check(before)
    assert before.posterior.threat_mass() > before.prior.threat_mass()

    hail = HailSimulator(_scenario("S05"), full, seed=11)
    result = hail.send(cid, run.tracks[cid].points[-1].tick)
    assert result.hail_reply.value == "none"
    table.extend(hail_evidence_rows(result, tick_s=run.tick_s))

    report = score_contact(table, cid, TABLES, PRIORS)
    _check(report)
    assert report.buckets["hail_reply"] == "none"
    assert report.prior_adjustments == ("night_hours", "critical_zone")
    assert report.posterior.top() is Hypothesis.UNAUTHORIZED_UUV, report.to_dict()
    assert report.posterior.threat_mass() >= 0.6


def test_cli_dump_is_canonical_and_labeled(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from soundings.investigation.posterior import main

    assert main(["--scenario", "S08", "--seed", "19"]) == 0
    text = capsys.readouterr().out
    doc = json.loads(text)
    assert doc["label"] == STRESS_TEST_LABEL and doc["scenario"] == "S08" and doc["seed"] == 19
    ids = [c["contact_id"] for c in doc["contacts"]]
    assert ids == sorted(ids) and ids
    assert all(c["label"] == STRESS_TEST_LABEL for c in doc["contacts"])
