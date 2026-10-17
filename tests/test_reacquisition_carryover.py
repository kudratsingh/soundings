"""WO-094: re-acquisition carry-over (SPEC-INVESTIGATION 9.9; DECISIONS WO-094 row).

When the matcher links a re-issued observed id to a lineage whose last case ended benign
through the investigation's stop rule, the new case re-decides that outcome from the
carried rows: same stop rule, same clause, no new interruption. Anything that is not the
same vehicle doing the same thing is investigated as usual.
"""

from __future__ import annotations

import dataclasses
from functools import cache

import pytest

from soundings.eval.catalog_verify import grade
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.machine import (
    InvestigationEvent,
    StopOutcome,
    StopRuleId,
    load_stopping_config,
)
from soundings.ladder.types import ActionKind
from soundings.matcher import load_match_config
from soundings.runner import (
    CARRYOVER_REASON,
    CARRYOVER_SOURCE_PREFIX,
    CarryOver,
    RunResultV2,
    RunResultV3,
    ScenarioRunnerV2,
    ScenarioRunnerV3,
    carried_hail_stale,
    carryover_consistency,
    case_interruptions,
    lineage_of,
)
from soundings.schemas import LadderState
from soundings.schemas.config import STANDING_ORDERS_PATH
from soundings.schemas.enums import EvidenceKind, FeatureName, Unit
from soundings.schemas.evidence import EvidenceRow

S = LadderState
STOPPING = load_stopping_config()
SPEED_TOL = load_match_config().reacquire.speed_tol_mps
REHAIL_S = load_match_config().plan_watch.rehail_interval_s


@cache
def v2(sid: str) -> RunResultV2:
    return ScenarioRunnerV2(sid).run()


@cache
def v3(sid: str) -> RunResultV3:
    return ScenarioRunnerV3(sid).run(orders_path=STANDING_ORDERS_PATH)


def _carry(result: RunResultV2, case_id: str) -> CarryOver:
    (record,) = [co for co in result.carryovers if co.case_id == case_id]
    return record


# --- (b) benign carry-over ---------------------------------------------------------------------


def test_s04_benign_carryover_resolves_by_the_same_clause_without_interruption() -> None:
    result = v2("S04")
    record = _carry(result, "c-0009#1")
    assert (record.old_contact_id, record.old_case_id) == ("c-0001", "c-0001#1")
    assert record.prior_outcome is StopOutcome.RESOLVED_BENIGN
    assert record.prior_rule is StopRuleId.C_KNOWN_OPERATOR
    assert record.eligible and record.identity_confirmed
    prior = result.recommendations["c-0001#1"]
    rec = result.recommendations["c-0009#1"]
    assert result.case("c-0009#1").state is S.RESOLVED_BENIGN
    assert (rec.outcome, rec.rule_id) == (prior.outcome, prior.rule_id)
    assert not rec.uncertain
    assert rec.probes_used == 0
    assert case_interruptions(result, "c-0009#1") == 0
    # The decision is the investigation machine's stop rule, not a runner shortcut.
    cps = result.investigation_checkpoints["c-0009#1"]
    assert cps[-1].event is InvestigationEvent.STOP_RULE_FIRED
    assert cps[-1].recommendation == rec
    assert not any(cp.event is InvestigationEvent.PROBE_SELECTED for cp in cps)
    closing = [t for t in result.transitions if t.case_id == "c-0009#1"][-1]
    assert closing.reason_code == "RESOLVED_BENIGN:C_KNOWN_OPERATOR"
    assert closing.from_state is S.INVESTIGATING


def test_s04_carried_rows_are_derived_with_reacquired_from_provenance() -> None:
    result = v2("S04")
    record = _carry(result, "c-0009#1")
    table = result.evidence
    rows = [table.get(i) for i in record.row_ids]
    assert {r.feature for r in rows} == {FeatureName.REACQUIRED_FROM, FeatureName.REGISTRY_MATCH}
    for row in rows:
        assert row.contact_id == "c-0009"
        assert row.kind is EvidenceKind.DERIVED
        assert row.source == f"{CARRYOVER_SOURCE_PREFIX}c-0001"
        sources = [table.get(i) for i in row.derived_from]
        assert isinstance(row.confidence, float)
        assert row.confidence <= min(float(s.confidence) for s in sources)  # type: ignore[arg-type]
    (ident,) = [r for r in rows if r.feature is FeatureName.REACQUIRED_FROM]
    modems = {table.get(i).value for i in ident.derived_from}
    assert len(modems) == 1  # the same modem answered on both tracks
    (registry,) = [r for r in rows if r.feature is FeatureName.REGISTRY_MATCH]
    assert registry.row_id in result.recommendations["c-0009#1"].evidence_row_ids


def test_s04_lineage_view_and_per_id_view_agree() -> None:
    result = v3("S04")
    g = grade(result)
    assert g.terminal_state is S.RESOLVED_BENIGN
    assert g.interruptions == (0, 0)
    assert g.lineage_interruptions == 0
    assert result.lineage_interruptions[lineage_of(result, g.lineage[-1])[0]] == 0


# --- not short-circuited ------------------------------------------------------------------------


def _row(cid: str, t: int, feature: FeatureName, value: float, unit: Unit) -> EvidenceRow:
    return EvidenceRow(
        contact_id=cid,
        t=t,
        feature=feature,
        value=value,
        unit=unit,
        source="extractor:test",
        confidence=0.9,
        kind=EvidenceKind.FACT,
        probe_id=None,
    )


def _pair_table(*, new_ttc: float, new_speed: float = 0.5, new_zone: float = 0.5) -> EvidenceTable:
    table = EvidenceTable()
    for cid, t0, speed, zone, ttc in (
        ("c-0001", 10, 0.5, 0.5, 5000.0),
        ("c-0009", 100, new_speed, new_zone, new_ttc),
    ):
        table.append(_row(cid, t0, FeatureName.SPEED_OVER_GROUND, speed, Unit.M_PER_S))
        table.append(_row(cid, t0, FeatureName.DEPTH, 6.0, Unit.M))
        table.append(_row(cid, t0, FeatureName.ZONE_CRITICALITY, zone, Unit.RATIO))
        table.append(_row(cid, t0, FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET, ttc, Unit.S))
    return table


def _consistency(table: EvidenceTable) -> str | None:
    return carryover_consistency(
        table,
        "c-0001",
        "c-0009",
        old_first_tick=10,
        first_tick=100,
        stopping=STOPPING,
        speed_tol_mps=SPEED_TOL,
    )


def test_consistency_rule_units() -> None:
    assert _consistency(_pair_table(new_ttc=5000.0)) is None
    closing = _consistency(_pair_table(new_ttc=STOPPING.benign_time_guard_s - 1))
    assert closing is not None and "closing on a critical asset" in closing
    assert _consistency(_pair_table(new_ttc=STOPPING.benign_time_guard_s)) is None
    fast = _consistency(_pair_table(new_ttc=5000.0, new_speed=2.5))
    assert fast is not None and "speed_over_ground" in fast
    # A neighbouring bucket within the matcher's speed tolerance is the same vehicle.
    assert _consistency(_pair_table(new_ttc=5000.0, new_speed=0.25)) is None
    zone = _consistency(_pair_table(new_ttc=5000.0, new_zone=0.9))
    assert zone is not None and "more critical zone" in zone


def test_closing_on_a_critical_asset_is_investigated_not_short_circuited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import soundings.runner as runner_mod

    def closing(*_a: object, **_k: object) -> str:
        return "closing on a critical asset: time to it 120 s < 300 s"

    monkeypatch.setattr(runner_mod, "carryover_consistency", closing)
    result = ScenarioRunnerV2("S04").run()
    record = _carry(result, "c-0009#1")
    assert not record.eligible and not record.rehailed and record.row_ids == ()
    rec = result.recommendations["c-0009#1"]
    # The ordinary investigation ran (planner probes), as before WO-094.
    assert rec.probes_used > 0
    assert rec.rule_id is not StopRuleId.C_KNOWN_OPERATOR
    assert not any(
        r.source.startswith(CARRYOVER_SOURCE_PREFIX) for r in result.evidence.for_contact("c-0009")
    )


def test_eligible_but_different_evidence_still_goes_through_the_investigation() -> None:
    # S08: the debris track first resolved as BIOLOGIC; the re-acquired track is eligible and
    # re-hailed, but no modem answers, so nothing beyond SPEC 9.9's rows is carried and the
    # stop rule decides HAZARD_NOTICE on the new evidence (not the prior's outcome).
    result = v2("S08")
    record = _carry(result, "c-0004#1")
    assert record.eligible and record.rehailed and not record.identity_confirmed
    rec = result.recommendations["c-0004#1"]
    assert rec.outcome is StopOutcome.HAZARD_NOTICE
    assert rec.outcome is not record.prior_outcome


# --- (c) HANDED_OFF lineages --------------------------------------------------------------------


@pytest.mark.parametrize("sid", ["S05", "S06"])
def test_handed_off_lineage_never_hands_off_twice(sid: str) -> None:
    result = v3(sid)
    assert result.carryovers == ()  # a handed-off prior is never re-decided as benign
    assert result.repeat_handoffs
    lineages: dict[str, list[str]] = {}
    for cid in result.contact_interruptions:
        lineages.setdefault(lineage_of(result, cid)[0], []).append(cid)
    for head, ids in lineages.items():
        counted = [
            cid
            for cid in ids
            if cid in result.ladder
            and result.ladder[cid].handling_outcome is S.HANDED_OFF
            and not any(a.kind is ActionKind.REQUEST_APPROVAL for a in result.ladder[cid].actions)
            and cid not in result.repeat_handoffs
        ]
        assert len(counted) <= 1, (head, counted)
        total = sum(result.contact_interruptions[c] for c in ids)
        repeats = sum(1 for c in ids if c in result.repeat_handoffs)
        assert result.lineage_interruptions[head] == total - repeats
    assert grade(result).terminal_state is S.HANDED_OFF  # the outcome is kept


def test_lineage_of_follows_matcher_links() -> None:
    result = v2("S02")
    assert lineage_of(result, "c-0008") == ("c-0001", "c-0003", "c-0006", "c-0008", "c-0011")
    assert lineage_of(result, "c-0002") == ("c-0002",)


# --- staleness ----------------------------------------------------------------------------------


def test_staleness_rule_units() -> None:
    assert carried_hail_stale(None, REHAIL_S)
    assert carried_hail_stale(REHAIL_S + 1, REHAIL_S)
    assert not carried_hail_stale(REHAIL_S, REHAIL_S)


def test_stale_carried_hail_triggers_a_rehail_first() -> None:
    result = v2("S04")
    record = _carry(result, "c-0009#1")
    assert record.hail_age_s is not None and record.hail_age_s > REHAIL_S
    assert record.rehailed
    hails = [h for h in result.hails if h.case_id == "c-0009#1"]
    assert [h.sent_tick for h in hails] == [record.tick]
    cps = result.investigation_checkpoints["c-0009#1"]
    assert cps[0].tick >= hails[0].reply_tick  # decided after the reply landed


def test_fresh_carried_hail_decides_on_the_carried_rows_without_a_hail() -> None:
    cfg = load_match_config()
    cfg = cfg.model_copy(
        update={"plan_watch": cfg.plan_watch.model_copy(update={"rehail_interval_s": 1e6})}
    )
    result = ScenarioRunnerV2("S04", match_cfg=cfg).run()
    record = _carry(result, "c-0009#1")
    assert record.eligible and not record.rehailed and not record.identity_confirmed
    assert [h for h in result.hails if h.case_id == "c-0009#1"] == []
    entry = [t for t in result.transitions if t.case_id == "c-0009#1"][2]
    assert (entry.from_state, entry.to_state, entry.reason_code) == (
        S.ASKING,
        S.INVESTIGATING,
        CARRYOVER_REASON,
    )
    # The carried rows alone (capped at the position match confidence) are decided by the
    # stop rule like any other table: here they do not reach benign_mass_resolve, so the
    # investigation continues instead of copying the prior outcome.
    cps = result.investigation_checkpoints["c-0009#1"]
    assert cps[-1].event is InvestigationEvent.STOP_RULE_FIRED
    assert any(cp.event is InvestigationEvent.PROBE_SELECTED for cp in cps)


# --- determinism --------------------------------------------------------------------------------


@pytest.mark.parametrize(("sid", "seed"), [("S04", 1), ("S04", 7), ("S05", 11)])
def test_carryover_is_deterministic(sid: str, seed: int) -> None:
    a = ScenarioRunnerV2(sid, seed=seed).run()
    b = ScenarioRunnerV2(sid, seed=seed).run()
    assert a.nested_log_hash == b.nested_log_hash
    assert [dataclasses.asdict(x) for x in a.carryovers] == [
        dataclasses.asdict(x) for x in b.carryovers
    ]
