"""WO-085 unit tests: priority, queue order, admission, the officer channel, config."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from soundings.eval.policies import InterruptionKind
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import NOT_CLOSING_S
from soundings.investigation.posterior import load_likelihood_tables, load_prior_config
from soundings.schemas.enums import EvidenceKind, FeatureName, Unit
from soundings.schemas.evidence import UNKNOWN, EvidenceRow
from soundings.triage import (
    Grant,
    OfficerArbiter,
    TriageConfig,
    TriageEntry,
    TriageInputs,
    TriageQueue,
    load_triage_config,
    priority,
    triage_inputs,
)

REPO = Path(__file__).resolve().parents[1]
CFG = load_triage_config()
FLOOR = CFG.ttc_floor_s


def _entry(cid: str, p: float, case_n: int = 1) -> TriageEntry:
    inputs = TriageInputs(
        contact_id=cid,
        case_id=f"{cid}#{case_n}",
        tick=0,
        threat_mass=0.5,
        zone_criticality=0.5,
        ttc_s=FLOOR,
        evidence_row_ids=("r",),
    )
    return TriageEntry(inputs=inputs, priority=p)


# --- priority ------------------------------------------------------------------------------

PRIORITY_CASES = [
    # (threat_mass, zone_criticality, ttc_s, expected)
    (0.8, 0.9, FLOOR, round(0.8 * 0.9 / FLOOR, 6)),
    (0.8, 0.9, FLOOR / 3, round(0.8 * 0.9 / FLOOR, 6)),  # below the floor = the floor
    (0.8, 0.9, 0.0, round(0.8 * 0.9 / FLOOR, 6)),
    (0.8, 0.9, -5.0, round(0.8 * 0.9 / FLOOR, 6)),
    (0.8, 0.9, 2 * FLOOR, round(0.8 * 0.9 / FLOOR / 2, 6)),  # twice as far: half
    (0.0, 0.9, FLOOR, 0.0),  # no threat mass: 0
    (1.0, 0.0, FLOOR, 0.0),
    (1.0, 1.0, FLOOR, round(1.0 / FLOOR, 6)),
]


@pytest.mark.parametrize(("tm", "zc", "ttc", "want"), PRIORITY_CASES)
def test_priority_table(tm: float, zc: float, ttc: float, want: float) -> None:
    assert priority(tm, zc, ttc, floor_s=FLOOR) == want


def test_priority_floor_equalities() -> None:
    p10 = priority(0.7, 0.9, FLOOR / 3, floor_s=FLOOR)
    p30 = priority(0.7, 0.9, FLOOR, floor_s=FLOOR)
    p60 = priority(0.7, 0.9, 2 * FLOOR, floor_s=FLOOR)
    assert p10 == p30
    assert p60 == pytest.approx(p30 / 2, abs=1e-6)


def test_priority_not_closing_is_negligible() -> None:
    assert priority(1.0, 1.0, NOT_CLOSING_S, floor_s=FLOOR) < 1e-8


@pytest.mark.parametrize(
    ("tm", "zc", "ttc"),
    [
        (math.nan, 0.5, 60.0),
        (0.5, math.nan, 60.0),
        (0.5, 0.5, math.nan),
        (0.5, 0.5, math.inf),
        (1.2, 0.5, 60.0),
        (-0.1, 0.5, 60.0),
        (0.5, 1.5, 60.0),
    ],
)
def test_priority_rejects_bad_inputs(tm: float, zc: float, ttc: float) -> None:
    with pytest.raises(ValueError):
        priority(tm, zc, ttc, floor_s=FLOOR)


def _row(cid: str, feature: FeatureName, value: object, unit: Unit, t: int = 5) -> EvidenceRow:
    return EvidenceRow(
        contact_id=cid,
        feature=feature,
        source="test",
        t=t,
        value=value,  # type: ignore[arg-type]
        unit=unit,
        confidence=1.0 if value != UNKNOWN else 0.0,
        kind=EvidenceKind.FACT,
        probe_id=None,
    )


def test_priority_unknown_zone_uses_config_and_cites_no_zone_row() -> None:
    table = EvidenceTable()
    z = _row("c-0001", FeatureName.ZONE_CRITICALITY, UNKNOWN, Unit.RATIO)
    ttc = _row("c-0001", FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET, 120.0, Unit.S)
    table.append(z)
    table.append(ttc)
    inp = triage_inputs(
        table, "c-0001", "c-0001#1", 5, load_likelihood_tables(), load_prior_config(), CFG
    )
    assert inp.zone_criticality == CFG.unknown_zone_criticality == 1.0
    assert z.row_id not in inp.evidence_row_ids
    assert ttc.row_id in inp.evidence_row_ids
    assert inp.ttc_s == 120.0


def test_priority_absent_ttc_uses_not_closing() -> None:
    table = EvidenceTable()
    table.append(_row("c-0002", FeatureName.ZONE_CRITICALITY, 0.9, Unit.RATIO))
    inp = triage_inputs(
        table, "c-0002", "c-0002#1", 5, load_likelihood_tables(), load_prior_config(), CFG
    )
    assert inp.ttc_s == NOT_CLOSING_S
    assert inp.zone_criticality == 0.9
    assert TriageEntry.of(inp, CFG).priority < 1e-8


# --- queue order ---------------------------------------------------------------------------


def test_tie_break_by_contact_then_case() -> None:
    q = TriageQueue([_entry("c-3", 0.1), _entry("c-0001", 0.1, 2), _entry("c-0001", 0.1, 1)])
    q.push(_entry("c-2", 0.5))
    assert [e.case_id for e in q.ordered()] == ["c-2#1", "c-0001#1", "c-0001#2", "c-3#1"]


ENTRIES = [_entry(f"c-{i % 17:04d}", float((i * 7) % 5) / 10, case_n=i) for i in range(50)]


@settings(derandomize=True, max_examples=50, deadline=None)
@given(st.permutations(ENTRIES))
def test_tie_break_insertion_order_irrelevant(perm: list[TriageEntry]) -> None:
    want = TriageQueue(ENTRIES).ordered()
    assert TriageQueue(perm).ordered() == want


def test_tie_break_rekey_moves_and_equals_remove_push() -> None:
    a = TriageQueue([_entry("c-0001", 0.1), _entry("c-2", 0.2), _entry("c-3", 0.3)])
    b = TriageQueue([_entry("c-0001", 0.1), _entry("c-2", 0.2), _entry("c-3", 0.3)])
    assert a.ordered()[-1].case_id == "c-0001#1"
    a.rekey(_entry("c-0001", 0.9))
    assert a.ordered()[0].case_id == "c-0001#1"
    b.remove("c-0001#1")
    b.push(_entry("c-0001", 0.9))
    assert a.ordered() == b.ordered()


def test_tie_break_push_duplicate_raises() -> None:
    q = TriageQueue([_entry("c-0001", 0.1)])
    with pytest.raises(ValueError):
        q.push(_entry("c-0001", 0.2))


# --- admission -----------------------------------------------------------------------------


def test_admit_top_slots_rest_in_order() -> None:
    q = TriageQueue([_entry(f"c-{i}", i / 10) for i in range(5)])
    got = q.admit(3)
    assert [e.case_id for e in got] == ["c-4#1", "c-3#1", "c-2#1"]
    assert [e.case_id for e in q.ordered()] == ["c-1#1", "c-0#1"]


def test_admit_zero_slots_admits_nothing() -> None:
    q = TriageQueue([_entry("c-0001", 0.1), _entry("c-2", 0.2)])
    assert q.admit(0) == ()
    assert q.admit(-1) == ()
    assert len(q) == 2


# --- the officer channel -------------------------------------------------------------------


def _cfg(**kw: float) -> TriageConfig:
    return CFG.model_copy(update=kw)


TICK_S = 2.0
REVIEW = math.ceil(CFG.officer_review_s / TICK_S)


def test_arbiter_serial_spacing_and_priority_order() -> None:
    arb = OfficerArbiter(CFG, TICK_S)
    arb.request("c-0001#1", InterruptionKind.HANDOFF, 0, 0.1)
    arb.request("c-2#1", InterruptionKind.HANDOFF, 0, 0.9)
    arb.request("c-3#1", InterruptionKind.APPROVAL_REQUEST, 0, 0.5)
    grants: list[Grant] = []
    for t in range(4 * REVIEW):
        grants += arb.grant_due(t)
    assert [g.case_id for g in grants] == ["c-2#1", "c-3#1", "c-0001#1"]
    assert [g.tick for g in grants] == [0, REVIEW, 2 * REVIEW]


def test_arbiter_mandatory_granted_when_optional_budget_spent() -> None:
    arb = OfficerArbiter(_cfg(optional_interruptions_per_hour=0), TICK_S)
    arb.request("c-0001#1", InterruptionKind.EYES_ON, 0, 0.9)
    arb.request("c-2#1", InterruptionKind.BACKUP_PAGE, 0, 0.1)
    assert not arb.eyes_on_allowed("c-0001#1", 0)
    got = arb.grant_due(0)
    assert [(g.case_id, g.kind) for g in got] == [("c-2#1", InterruptionKind.BACKUP_PAGE)]
    assert len(arb.pending) == 1  # the eyes-on waits; never discarded


def test_arbiter_third_eyes_on_deferred_until_window_slides() -> None:
    arb = OfficerArbiter(CFG, TICK_S)
    window = math.ceil(CFG.optional_window_s / TICK_S)
    cap = CFG.optional_interruptions_per_hour
    assert cap == 2
    for i in range(cap + 1):
        arb.request(f"c-{i}#1", InterruptionKind.EYES_ON, 0, 0.5)
    grants: list[Grant] = []
    for t in range(window + 2 * REVIEW):
        grants += arb.grant_due(t)
    assert [g.tick for g in grants[:cap]] == [0, REVIEW]
    third = grants[cap]
    assert third.tick == window  # the first grant (tick 0) left the trailing window
    assert third.case_id == f"c-{cap}#1"


def test_arbiter_nothing_discarded() -> None:
    arb = OfficerArbiter(_cfg(optional_interruptions_per_hour=1), TICK_S)
    kinds = [
        InterruptionKind.HANDOFF,
        InterruptionKind.EYES_ON,
        InterruptionKind.APPROVAL_REQUEST,
        InterruptionKind.EYES_ON,
        InterruptionKind.BACKUP_PAGE,
    ]
    for i, k in enumerate(kinds):
        arb.request(f"c-{i}#1", k, i, i / 10)
    window = math.ceil(CFG.optional_window_s / TICK_S)
    out: list[Grant] = []
    for t in range(3 * window):
        out += arb.grant_due(t)
    assert len(out) == len(kinds)
    assert sorted(g.case_id for g in out) == sorted(f"c-{i}#1" for i in range(len(kinds)))
    assert arb.pending == ()


def test_arbiter_eyes_on_allowed_counts_pending() -> None:
    arb = OfficerArbiter(CFG, TICK_S)
    assert arb.eyes_on_allowed("c-0001#1", 0)
    arb.request("c-0001#1", InterruptionKind.EYES_ON, 0, 0.5)
    arb.request("c-2#1", InterruptionKind.EYES_ON, 0, 0.5)
    assert not arb.eyes_on_allowed("c-3#1", 0)


# --- config --------------------------------------------------------------------------------


def test_config_values_from_yaml() -> None:
    assert CFG.ttc_floor_s == 30.0
    assert CFG.investigation_slots == 4
    assert CFG.optional_interruptions_per_hour == 2
    assert CFG.optional_window_s == 3600.0
    assert CFG.unknown_zone_criticality == 1.0
    assert CFG.officer_review_s == 120.0  # config/eval.yaml baselines.human_review_s


def test_config_tmp_floor_changes_floor_case(tmp_path: Path) -> None:
    text = (REPO / "config" / "triage.yaml").read_text(encoding="utf-8")
    p = tmp_path / "triage.yaml"
    p.write_text(re.sub(r"ttc_floor_s: \d+", "ttc_floor_s: 60", text), encoding="utf-8")
    cfg = load_triage_config(p)
    assert cfg.ttc_floor_s == 60.0
    a = priority(0.7, 0.9, 30.0, floor_s=cfg.ttc_floor_s)
    b = priority(0.7, 0.9, 60.0, floor_s=cfg.ttc_floor_s)
    assert a == b


def test_config_extra_key_rejected(tmp_path: Path) -> None:
    text = (REPO / "config" / "triage.yaml").read_text(encoding="utf-8")
    p = tmp_path / "triage.yaml"
    p.write_text(text + "surprise: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_triage_config(p)


def test_config_no_literals_in_triage_module() -> None:
    src = (REPO / "soundings" / "triage.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(30|120|3600)\b", src)
