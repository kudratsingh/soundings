"""WO-033 part A: the stopping rule (SPEC-INVESTIGATION section 7 plus C_KNOWN_OPERATOR).

Table-driven: every clause at, just above and just below its threshold, the documented
precedence, and proof that the thresholds are read from ``config/ladder.yaml``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import NOT_CLOSING_S
from soundings.investigation.machine import (
    STOP_PRECEDENCE,
    StopDecision,
    StopInputs,
    StopOutcome,
    StoppingConfig,
    StopRuleId,
    evaluate_stop,
    evaluate_stop_at,
    load_stopping_config,
    stop_inputs_from,
)
from soundings.investigation.posterior import (
    load_likelihood_tables,
    load_prior_config,
    score_contact,
)
from soundings.schemas._base import CONFIG_DIR
from soundings.schemas.enums import (
    EvidenceKind,
    FeatureName,
    HailReply,
    Hypothesis,
    ProbeId,
    RegistryMatch,
    Unit,
)
from soundings.schemas.evidence import EvidenceRow

MACHINE_PY = Path(__file__).resolve().parent.parent / "soundings/investigation/machine.py"
CFG = load_stopping_config()

R = StopOutcome
C = StopRuleId
H = Hypothesis

# A neutral base: no clause holds.
BASE: dict[str, Any] = {"benign_mass": 0.5, "threat_mass": 0.1, "top": H.BIOLOGIC}
KNOWN_OP: dict[str, Any] = {
    "benign_mass": 0.1,
    "threat_mass": 0.05,
    "top": H.UNFILED_KNOWN_OPERATOR,
    "p_known_operator": 0.85,
    "hail_reply": HailReply.VALID,
    "operator_known": True,
}
LOST: dict[str, Any] = {
    "benign_mass": 0.2,
    "threat_mass": 0.1,
    "top": H.FILED_OFF_PLAN,
    "hail_reply": HailReply.NONE,
}


def _inputs(base: dict[str, Any], **over: Any) -> StopInputs:
    return StopInputs(**{**base, **over})


CLAUSE_CASES: list[tuple[str, StopInputs, tuple[StopOutcome, StopRuleId, bool] | None]] = [
    ("neutral", _inputs(BASE), None),
    # C_THREAT, threat mass
    ("threat_0.3499", _inputs(BASE, threat_mass=0.3499), None),
    ("threat_0.35", _inputs(BASE, threat_mass=0.35), (R.HANDOFF, C.C_THREAT, False)),
    ("threat_0.3501", _inputs(BASE, threat_mass=0.3501), (R.HANDOFF, C.C_THREAT, False)),
    # C_THREAT, imminent asset with low benign mass
    (
        "ttc_179_benign_0.59",
        _inputs(BASE, ttc_s=179, benign_mass=0.59),
        (R.HANDOFF, C.C_THREAT, False),
    ),
    ("ttc_179_benign_0.60", _inputs(BASE, ttc_s=179, benign_mass=0.60), None),
    ("ttc_180_benign_0.59", _inputs(BASE, ttc_s=180, benign_mass=0.59), None),
    ("ttc_181_benign_0.59", _inputs(BASE, ttc_s=181, benign_mass=0.59), None),
    ("ttc_none_benign_0.1", _inputs(BASE, benign_mass=0.1), None),
    # C_BENIGN and the benign time guard
    ("benign_0.85_ttc_299", _inputs(BASE, benign_mass=0.85, ttc_s=299), None),
    (
        "benign_0.85_ttc_300",
        _inputs(BASE, benign_mass=0.85, ttc_s=300),
        (R.RESOLVED_BENIGN, C.C_BENIGN, False),
    ),
    (
        "benign_0.85_no_asset",
        _inputs(BASE, benign_mass=0.85),
        (R.RESOLVED_BENIGN, C.C_BENIGN, False),
    ),
    ("benign_0.8499", _inputs(BASE, benign_mass=0.8499), None),
    ("benign_0.8501", _inputs(BASE, benign_mass=0.8501), (R.RESOLVED_BENIGN, C.C_BENIGN, False)),
    (
        "hazard_only_debris",
        _inputs(BASE, benign_mass=0.9, top=H.DEBRIS_OR_DRIFT),
        (R.HAZARD_NOTICE, C.C_BENIGN, False),
    ),
    (
        "biologic_not_hazard",
        _inputs(BASE, benign_mass=0.9, top=H.BIOLOGIC),
        (R.RESOLVED_BENIGN, C.C_BENIGN, False),
    ),
    (
        "filed_not_hazard",
        _inputs(BASE, benign_mass=0.9, top=H.FILED_ON_PLAN),
        (R.RESOLVED_BENIGN, C.C_BENIGN, False),
    ),
    ("debris_guarded", _inputs(BASE, benign_mass=0.9, top=H.DEBRIS_OR_DRIFT, ttc_s=299), None),
    # C_CLEARED
    (
        "cleared",
        _inputs(BASE, registry_match=RegistryMatch.EXACT, hail_reply=HailReply.VALID),
        (R.CLEARED_AT_ASK, C.C_CLEARED, False),
    ),
    (
        "exact_no_reply",
        _inputs(BASE, registry_match=RegistryMatch.EXACT, hail_reply=HailReply.NONE),
        None,
    ),
    (
        "area_mismatch_valid",
        _inputs(BASE, registry_match=RegistryMatch.AREA_MISMATCH, hail_reply=HailReply.VALID),
        None,
    ),
    # C_LOST: silence strictly above lost_vehicle_silence_min * 60
    ("lost_601", _inputs(LOST, silence_s=601), (R.LOST_VEHICLE, C.C_LOST, False)),
    ("lost_600", _inputs(LOST, silence_s=600), None),
    ("lost_599", _inputs(LOST, silence_s=599), None),
    (
        "lost_timeout_601",
        _inputs(LOST, hail_reply=HailReply.TIMEOUT, silence_s=601),
        (R.LOST_VEHICLE, C.C_LOST, False),
    ),
    ("lost_valid_reply", _inputs(LOST, hail_reply=HailReply.VALID, silence_s=601), None),
    ("lost_invalid_reply", _inputs(LOST, hail_reply=HailReply.INVALID, silence_s=601), None),
    ("lost_not_off_plan", _inputs(LOST, top=H.FILED_ON_PLAN, silence_s=601), None),
    ("lost_no_hail", _inputs(LOST, hail_reply=None, silence_s=601), None),
    ("lost_never_heard", _inputs(LOST, silence_s=None), None),
    # C_BUDGET
    ("budget", _inputs(BASE, budget_exhausted=True), (R.HANDOFF, C.C_BUDGET, True)),
    # C_KNOWN_OPERATOR
    ("known_op_0.85", _inputs(KNOWN_OP), (R.RESOLVED_BENIGN, C.C_KNOWN_OPERATOR, False)),
    ("known_op_0.8499", _inputs(KNOWN_OP, p_known_operator=0.8499), None),
    ("known_op_not_known", _inputs(KNOWN_OP, operator_known=False), None),
    ("known_op_unknown_operator", _inputs(KNOWN_OP, operator_known=None), None),
    ("known_op_ttc_299", _inputs(KNOWN_OP, ttc_s=299), None),
    (
        "known_op_ttc_300",
        _inputs(KNOWN_OP, ttc_s=300),
        (R.RESOLVED_BENIGN, C.C_KNOWN_OPERATOR, False),
    ),
    ("known_op_no_reply", _inputs(KNOWN_OP, hail_reply=HailReply.NONE), None),
    ("known_op_not_top", _inputs(KNOWN_OP, top=H.FILED_ON_PLAN), None),
]


@pytest.mark.parametrize(
    ("case", "inputs", "expected"), CLAUSE_CASES, ids=[c[0] for c in CLAUSE_CASES]
)
def test_clauses(
    case: str, inputs: StopInputs, expected: tuple[StopOutcome, StopRuleId, bool] | None
) -> None:
    decision = evaluate_stop(inputs, CFG)
    if expected is None:
        assert decision is None, case
    else:
        outcome, rule, uncertain = expected
        assert decision == StopDecision(outcome=outcome, rule_id=rule, uncertain=uncertain)


def test_clauses_case_count_and_every_rule_covered() -> None:
    assert len(CLAUSE_CASES) >= 14
    fired = {e[1] for _, _, e in CLAUSE_CASES if e is not None}
    assert fired == set(StopRuleId)
    assert {e[0] for _, _, e in CLAUSE_CASES if e is not None} == set(StopOutcome)


def test_precedence_order_is_documented() -> None:
    assert STOP_PRECEDENCE == (
        C.C_CLEARED,
        C.C_LOST,
        C.C_THREAT,
        C.C_KNOWN_OPERATOR,
        C.C_BENIGN,
        C.C_BUDGET,
    )


def test_precedence_cleared_beats_threat() -> None:
    inputs = _inputs(
        BASE, threat_mass=0.9, registry_match=RegistryMatch.EXACT, hail_reply=HailReply.VALID
    )
    decision = evaluate_stop(inputs, CFG)
    assert decision is not None and decision.outcome is R.CLEARED_AT_ASK


def test_precedence_threat_beats_budget_and_is_certain() -> None:
    decision = evaluate_stop(_inputs(BASE, threat_mass=0.5, budget_exhausted=True), CFG)
    assert decision == StopDecision(outcome=R.HANDOFF, rule_id=C.C_THREAT, uncertain=False)


def test_precedence_threat_beats_known_operator() -> None:
    decision = evaluate_stop(_inputs(KNOWN_OP, threat_mass=0.35), CFG)
    assert decision is not None
    assert (decision.outcome, decision.rule_id) == (R.HANDOFF, C.C_THREAT)
    imminent = evaluate_stop(_inputs(KNOWN_OP, ttc_s=100, benign_mass=0.1), CFG)
    assert imminent is not None and imminent.rule_id is C.C_THREAT


def test_precedence_lost_beats_threat() -> None:
    decision = evaluate_stop(_inputs(LOST, silence_s=900, threat_mass=0.5), CFG)
    assert decision is not None and decision.rule_id is C.C_LOST


def test_precedence_known_operator_beats_benign_and_budget() -> None:
    decision = evaluate_stop(_inputs(KNOWN_OP, benign_mass=0.9, budget_exhausted=True), CFG)
    assert decision is not None and decision.rule_id is C.C_KNOWN_OPERATOR


def test_precedence_benign_beats_budget() -> None:
    decision = evaluate_stop(_inputs(BASE, benign_mass=0.9, budget_exhausted=True), CFG)
    assert decision is not None
    assert (decision.rule_id, decision.uncertain) == (C.C_BENIGN, False)


def test_thresholds_from_config_values() -> None:
    raw = yaml.safe_load((CONFIG_DIR / "ladder.yaml").read_text(encoding="utf-8"))["stopping"]
    assert CFG.model_dump() == {k: float(v) for k, v in raw.items()}
    assert CFG.lost_vehicle_silence_s == raw["lost_vehicle_silence_min"] * 60


def test_thresholds_from_config_flip(tmp_path: Path) -> None:
    doc = yaml.safe_load((CONFIG_DIR / "ladder.yaml").read_text(encoding="utf-8"))
    doc["stopping"]["threat_mass_handoff"] = 0.5
    path = tmp_path / "ladder.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    tmp_cfg = load_stopping_config(path)
    case = _inputs(BASE, threat_mass=0.4)
    assert evaluate_stop(case, CFG) == StopDecision(outcome=R.HANDOFF, rule_id=C.C_THREAT)
    assert evaluate_stop(case, tmp_cfg) is None


def test_thresholds_from_config_missing_key_raises(tmp_path: Path) -> None:
    doc = yaml.safe_load((CONFIG_DIR / "ladder.yaml").read_text(encoding="utf-8"))
    del doc["stopping"]["imminent_time_s"]
    path = tmp_path / "ladder.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ValueError):
        load_stopping_config(path)


def test_thresholds_from_config_no_literal_in_machine() -> None:
    text = MACHINE_PY.read_text(encoding="utf-8")
    hits = [
        (i, ln)
        for i, ln in enumerate(text.splitlines(), 1)
        if re.search(r"0\.(35|85|6)\b|\b(180|300)\b", ln)
    ]
    assert hits == []


def test_stopping_config_is_frozen() -> None:
    with pytest.raises(ValueError):
        CFG.threat_mass_handoff = 0.9  # type: ignore[misc]
    assert isinstance(CFG, StoppingConfig)


# --- inputs read from the evidence table ------------------------------------------------

CID = "c-0001"


def _row(
    feature: FeatureName, value: Any, unit: Unit, t: int, source: str, **kw: Any
) -> EvidenceRow:
    return EvidenceRow(
        contact_id=CID,
        t=t,
        feature=feature,
        value=value,
        unit=unit,
        source=source,
        confidence=kw.pop("confidence", 0.9),
        kind=EvidenceKind.FACT,
        probe_id=kw.pop("probe_id", None),
    )


def test_stop_inputs_read_latest_rows() -> None:
    table = EvidenceTable()
    table.extend(
        [
            _row(
                FeatureName.PROPELLER_TONAL_PRESENT, True, Unit.BOOL, 10, "acoustic_detector:hyd_1"
            ),
            _row(FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET, 250.0, Unit.S, 10, "extractor:kin"),
            _row(
                FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET,
                NOT_CLOSING_S,
                Unit.S,
                20,
                "extractor:kin",
            ),
            _row(FeatureName.REGISTRY_MATCH, "none", Unit.CATEGORY, 20, "registry"),
            _row(
                FeatureName.HAIL_REPLY,
                "timeout",
                Unit.CATEGORY,
                30,
                "probe:acoustic_hail",
                probe_id=ProbeId.ACOUSTIC_HAIL,
            ),
            _row(
                FeatureName.OPERATOR_KNOWN,
                False,
                Unit.BOOL,
                30,
                "probe:acoustic_hail",
                probe_id=ProbeId.ACOUSTIC_HAIL,
            ),
        ]
    )
    report = score_contact(table, CID, load_likelihood_tables(), load_prior_config())
    inputs = stop_inputs_from(report, table, tick=40, tick_s=2.0)
    assert inputs.registry_match is RegistryMatch.NONE
    assert inputs.hail_reply is HailReply.TIMEOUT
    assert inputs.operator_known is False
    assert inputs.ttc_s is None  # NOT_CLOSING_S: no critical asset approached
    assert inputs.silence_s == 60.0  # (40 - 10) ticks x 2 s since the detector row
    assert inputs.top is report.posterior.top()
    assert all(rid in table for rid in inputs.row_ids)
    same, decision = evaluate_stop_at(report, table, CFG, tick=40, tick_s=2.0)
    assert same == inputs
    assert decision == evaluate_stop(inputs, CFG)


def test_stop_inputs_unknown_values_never_satisfy() -> None:
    table = EvidenceTable()
    table.extend(
        [
            _row(
                FeatureName.HAIL_REPLY,
                "unknown",
                Unit.CATEGORY,
                5,
                "probe:acoustic_hail",
                confidence="unknown",
            ),
            _row(
                FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET,
                "unknown",
                Unit.S,
                5,
                "no_track",
                confidence="unknown",
            ),
        ]
    )
    report = score_contact(table, CID, load_likelihood_tables(), load_prior_config())
    inputs = stop_inputs_from(report, table, tick=5, tick_s=2.0)
    assert inputs.hail_reply is None and inputs.ttc_s is None and inputs.silence_s is None


def test_silence_counts_from_valid_reply() -> None:
    table = EvidenceTable()
    table.extend(
        [
            _row(FeatureName.BROADBAND_LEVEL, 110.0, Unit.DB, 10, "acoustic_detector:hyd_2"),
            _row(
                FeatureName.HAIL_REPLY,
                "valid",
                Unit.CATEGORY,
                50,
                "probe:acoustic_hail",
                probe_id=ProbeId.ACOUSTIC_HAIL,
            ),
        ]
    )
    report = score_contact(table, CID, load_likelihood_tables(), load_prior_config())
    assert stop_inputs_from(report, table, tick=60, tick_s=2.0).silence_s == 20.0
