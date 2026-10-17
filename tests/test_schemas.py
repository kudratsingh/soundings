"""Unit tests for soundings.schemas and soundings.canonical (WO-010).

Hard rules exercised: CLAUDE.md 2.1 and 2.5 (closed vocabulary, denylist on free text),
2.2 (only enum members occupy rung and state fields), 2.4 (canonical JSON, derandomized
property tests), 2.6 (fact, assumption and derived rows are distinct and never null).
"""

from __future__ import annotations

import inspect
import math
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import AfterValidator, BaseModel, ValidationError

import soundings.canonical as canonical_mod
import soundings.llm.client as llm_client
from soundings.canonical import FLOAT_DECIMALS, canonical_json, round6, stable_hash
from soundings.schemas import (
    DENYLIST,
    HANDLING_TERMINAL_STATES,
    TERMINAL_STATES,
    UNKNOWN,
    AcousticReading,
    BehaviorType,
    Contact,
    EvidenceKind,
    EvidenceRow,
    Expected,
    FeatureName,
    Harbor,
    Hypothesis,
    LadderState,
    ProbeId,
    Rung,
    Toggle,
    ToggleName,
    TonalBand,
    Track,
    TrackGap,
    TrackPoint,
    Unit,
    _base,
    assert_clean,
    config,
    denylist,
    distance_m,
    enums,
    evidence,
    find_denylisted,
    from_local_m,
    harbor,
    load_harbor,
    load_ladder_config,
    load_likelihood_spec,
    load_priors,
    load_scenarios,
    load_standing_orders,
    point_in_polygon,
    scenario,
    to_local_m,
    track,
)
from soundings.schemas.config import DegradedMode
from soundings.schemas.denylist import _compile
from soundings.schemas.scenario import scenario_from_data

REPO_ROOT = Path(__file__).resolve().parent.parent
DENYLIST_FILE = REPO_ROOT / "config" / "denylist.txt"
SCHEMA_MODULES = (enums, evidence, track, harbor, scenario, config, denylist, _base)
PROPS = settings(derandomize=True, max_examples=200, database=None, deadline=None)


def _all_models() -> list[type[BaseModel]]:
    out: dict[str, type[BaseModel]] = {}
    for mod in SCHEMA_MODULES:
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, BaseModel) and obj.__module__ == mod.__name__:
                out[f"{mod.__name__}.{name}"] = obj
    return [out[k] for k in sorted(out)]


def _all_enums() -> list[type[enums.StrEnum]]:
    return [
        obj
        for _, obj in inspect.getmembers(enums, inspect.isclass)
        if issubclass(obj, enums.StrEnum)
        and obj.__module__ == enums.__name__
        and not obj.__name__.startswith("_")
    ]


# ---------------------------------------------------------------- enums


def test_enum_members_exact() -> None:
    assert [h.value for h in Hypothesis] == [
        "FILED_ON_PLAN",
        "FILED_OFF_PLAN",
        "UNFILED_KNOWN_OPERATOR",
        "BIOLOGIC",
        "DEBRIS_OR_DRIFT",
        "SURFACE_VESSEL_AIS_OFF",
        "UNAUTHORIZED_UUV",
        "UNAUTHORIZED_DIVER",
    ]
    assert [r.value for r in Rung] == ["watch", "ask", "slow", "hold", "hand_to_person"]
    assert [r.value for r in Rung] == yaml.safe_load(
        (REPO_ROOT / "config" / "ladder.yaml").read_text(encoding="utf-8")
    )["rungs"]
    assert [s.value for s in LadderState] == [
        "DETECTED",
        "WATCHING",
        "ASKING",
        "INVESTIGATING",
        "ADVISING_SLOW",
        "HOLDING",
        "AWAITING_APPROVAL",
        "HANDED_OFF",
        "WATCH_AFTER_HANDOFF",
        "RESOLVED_BENIGN",
        "CLEARED_AT_ASK",
        "HAZARD_NOTICE",
        "LOST_VEHICLE",
        "RESOLVED_DEPARTED",
        "FAILED",
    ]
    assert len(Hypothesis) == 8 and len(Rung) == 5 and len(LadderState) == 15
    S = LadderState
    assert (
        frozenset(
            {
                S.RESOLVED_BENIGN,
                S.CLEARED_AT_ASK,
                S.HAZARD_NOTICE,
                S.LOST_VEHICLE,
                S.RESOLVED_DEPARTED,
                S.FAILED,
            }
        )
        == TERMINAL_STATES
    )
    assert HANDLING_TERMINAL_STATES == TERMINAL_STATES | {S.HANDED_OFF}
    assert isinstance(TERMINAL_STATES, frozenset)
    assert isinstance(HANDLING_TERMINAL_STATES, frozenset)
    assert S.HANDED_OFF not in TERMINAL_STATES
    assert S.WATCH_AFTER_HANDOFF not in HANDLING_TERMINAL_STATES


def test_other_enum_members_exact() -> None:
    assert [e.value for e in EvidenceKind] == ["fact", "assumption", "derived"]
    # WO-088 appends three responder outputs after the five support names.
    assert len(FeatureName) == 40
    assert [f.value for f in FeatureName][-3:] == [
        "visual_class",
        "search_result",
        "search_position",
    ]
    assert [f.value for f in FeatureName][-8:-3] == [
        "track_segment",
        "current_field",
        "pol_baseline",
        "weekday",
        "reacquired_from",
    ]
    assert [f.value for f in FeatureName][:3] == ["speed_over_ground", "speed_variance", "depth"]
    assert len(ProbeId) == 13 and len(ToggleName) == 9  # WO-088: 11 section 5 + 2 responder
    assert [p.value for p in ProbeId][-2:] == ["responder_look", "responder_search_box"]
    assert len(BehaviorType) == 7
    assert [t.value for t in TonalBand] == ["none", "low", "mid", "high"]
    assert [u.value for u in Unit] == [
        "m", "m/s", "m/s2", "deg/s", "s", "ratio", "bool", "category", "id", "hour", "dB",
        "unknown",
    ]  # fmt: skip
    likelihood_features = set(load_likelihood_spec().features)
    assert likelihood_features <= set(FeatureName)


# ---------------------------------------------------------------- rung / state fields

BAD_VOCAB: list[Any] = ["climb", "HOLD", "hand to person", "", None, True, 3]


@pytest.mark.parametrize("bad", BAD_VOCAB)
def test_invalid_rung_and_state_rejected(bad: Any) -> None:
    with pytest.raises(ValidationError):
        DegradedMode.model_validate({"when": (), "allowed_rungs": (bad,)})
    with pytest.raises(ValidationError):
        Expected.model_validate({"terminal_state": bad})
    with pytest.raises(ValidationError):
        Expected.model_validate({"terminal_state": "HANDED_OFF", "passes_through": (bad,)})
    # The valid vocabulary is accepted, as members or as their string values.
    ok = DegradedMode.model_validate({"when": (), "allowed_rungs": ("hold", Rung.HAND_TO_PERSON)})
    assert ok.allowed_rungs == (Rung.HOLD, Rung.HAND_TO_PERSON)
    assert Expected.model_validate({"terminal_state": "HOLDING"}).terminal_state is (
        LadderState.HOLDING
    )


# ---------------------------------------------------------------- denylist


def _sample_from_pattern(pattern: str) -> str:
    """Build a matching word from a denylist pattern without storing any literal."""
    s = pattern.replace(r"\b", "")
    s = re.sub(r"\([^()]*\)\?", "", s)  # drop optional groups
    s = re.sub(r"\[([^\]])[^\]]*\]", r"\1", s)  # first character of each class
    s = re.sub(r"\(([^()|]*)(?:\|[^()]*)?\)", r"\1", s)  # first alternative
    s = re.sub(r".\?", "", s)  # drop optional single characters
    return s


def _denylist_samples() -> list[str]:
    return [_sample_from_pattern(p.pattern) for p in DENYLIST]


def test_denylist_compiled_from_file() -> None:
    lines = DENYLIST_FILE.read_text(encoding="utf-8").splitlines()
    active = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    assert len(DENYLIST) == len(active) > 0
    assert [p.pattern for p in DENYLIST] == active
    for pattern, sample in zip(DENYLIST, _denylist_samples(), strict=True):
        assert pattern.flags & re.IGNORECASE
        assert pattern.search(sample), (pattern.pattern, sample)
        assert pattern.search(sample.upper())
        assert find_denylisted(f"the {sample} here")
        with pytest.raises(ValueError):
            assert_clean(sample)
    assert assert_clean("watch the contact") == "watch the contact"


def test_denylist_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _compile(tmp_path / "absent.txt")


def _free_text_fields(model: type[BaseModel]) -> list[str]:
    names = []
    for name, info in model.model_fields.items():
        if any(isinstance(m, AfterValidator) and m.func is assert_clean for m in info.metadata):
            names.append(name)
    return names


def _valid_bases() -> dict[type[BaseModel], dict[str, Any]]:
    h = load_harbor()
    scenarios = {s.id: s for s in load_scenarios(REPO_ROOT / "scenarios")}
    s02 = scenarios["S02"]
    so = load_standing_orders(h)
    row = _row(kind="assumption", assumes="current field is the configured default")
    return {
        harbor.Harbor: h.model_dump(),
        harbor.Zone: h.zones[0].model_dump(),
        harbor.Asset: h.assets[0].model_dump(),
        harbor.SensorNode: h.sensor_nodes[0].model_dump(),
        scenario.Scenario: s02.model_dump(),
        scenario.RegistryPlan: s02.registry_plans[0].model_dump(),
        scenario.KnownOperator: {"operator": "Harbor Survey Co (demo)", "modem_id": "MOD-1"},
        evidence.EvidenceRow: row.model_dump(exclude={"row_id"}),
        config.Approver: so.orders.approvers["primary"].model_dump(),
        config.Orders: so.orders.model_dump(),
    }


def test_free_text_fields_reject_denylist() -> None:
    bases = _valid_bases()
    with_free_text = {m: _free_text_fields(m) for m in _all_models() if _free_text_fields(m)}
    assert set(with_free_text) == set(bases), "every model with FreeText needs a valid base"
    assert sum(len(v) for v in with_free_text.values()) >= 13
    samples = _denylist_samples()
    for model, fields in with_free_text.items():
        base = bases[model]
        model.model_validate(base)
        for field in fields:
            for sample in samples:
                for text in (sample, f"Note: {sample.upper()} near berth"):
                    with pytest.raises(ValidationError):
                        model.model_validate({**base, field: text})
            for near_miss in ("program", "ramp", "network", "netting"):
                model.model_validate({**base, field: f"{near_miss} check"})


def test_no_identifier_matches_denylist() -> None:
    def variants(name: str) -> list[str]:
        spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
        return [name, name.replace("_", " "), spaced, spaced.replace("_", " ")]

    identifiers: list[str] = []
    for e in _all_enums():
        identifiers.append(e.__name__)
        identifiers += [m.value for m in e] + [m.name for m in e]
    for model in _all_models():
        identifiers.append(model.__name__)
        identifiers += list(model.model_fields)
    h = load_harbor()
    identifiers += [z.zone_id for z in h.zones] + [a.asset_id for a in h.assets]
    identifiers += [n.node_id for n in h.sensor_nodes] + h.basins()
    identifiers += [z.name for z in h.zones] + [a.name for a in h.assets] + [h.name]
    assert len(identifiers) > 200
    for ident in identifiers:
        for v in variants(ident):
            assert not find_denylisted(v), ident

    # Nothing after the handoff can name an action: the only states outside the
    # pre-handoff path and its outcomes are watching and departure (CLAUDE.md 2.1).
    S = LadderState
    pre_handoff = {
        S.DETECTED,
        S.WATCHING,
        S.ASKING,
        S.INVESTIGATING,
        S.ADVISING_SLOW,
        S.HOLDING,
        S.AWAITING_APPROVAL,
        S.HANDED_OFF,
        S.RESOLVED_BENIGN,
        S.CLEARED_AT_ASK,
        S.HAZARD_NOTICE,
        S.LOST_VEHICLE,
        S.FAILED,
    }
    assert set(LadderState) - pre_handoff == {S.WATCH_AFTER_HANDOFF, S.RESOLVED_DEPARTED}


# ---------------------------------------------------------------- evidence rows


def _row(**kw: Any) -> EvidenceRow:
    base: dict[str, Any] = {
        "contact_id": "c-0001",
        "t": 12,
        "feature": FeatureName.SPEED_OVER_GROUND,
        "value": 1.25,
        "unit": Unit.M_PER_S,
        "source": "features.kinematics",
        "confidence": 0.8,
        "kind": EvidenceKind.FACT,
        "probe_id": None,
    }
    base.update(kw)
    return EvidenceRow.model_validate(base)


def test_evidence_row_rules() -> None:
    # None is rejected everywhere except probe_id.
    for col in ("value", "unit", "confidence", "kind", "feature", "source", "t"):
        with pytest.raises(ValidationError):
            _row(**{col: None})
    assert _row(probe_id=None).probe_id is None
    assert _row(probe_id="acoustic_hail").probe_id is ProbeId.ACOUSTIC_HAIL
    # The explicit sentinel is accepted for value and confidence.
    r = _row(value="unknown", confidence=UNKNOWN, unit="unknown")
    assert r.value == UNKNOWN and r.confidence == UNKNOWN
    # True stays a bool (never 1), and bool never fills an int or float column.
    assert _row(value=True).value is True
    assert type(_row(value=1).value) is int
    assert type(_row(value=1.0).value) is float
    for col in ("t", "confidence"):
        with pytest.raises(ValidationError):
            _row(**{col: True})
    # NaN and infinity are rejected.
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValidationError):
            _row(value=bad)
        with pytest.raises(ValidationError):
            _row(confidence=bad)
    with pytest.raises(ValidationError):
        _row(confidence=1.01)
    with pytest.raises(ValidationError):
        _row(t=-1)
    with pytest.raises(ValidationError):
        _row(contact_id="gt-1")
    with pytest.raises(ValidationError):
        _row(source="Bad Source")
    # Kind rules: derived names sources, assumption names what it assumes, fact neither.
    parent = _row()
    with pytest.raises(ValidationError):
        _row(kind="derived")
    with pytest.raises(ValidationError):
        _row(kind="assumption")
    with pytest.raises(ValidationError):
        _row(kind="assumption", assumes="   ")
    with pytest.raises(ValidationError):
        _row(kind="fact", derived_from=(parent.row_id,))
    with pytest.raises(ValidationError):
        _row(kind="fact", assumes="something")
    with pytest.raises(ValidationError):
        _row(kind="derived", derived_from=(parent.row_id,), assumes="x")
    with pytest.raises(ValidationError):
        _row(kind="derived", derived_from=("not-a-row-id",))
    d = _row(kind="derived", derived_from=(parent.row_id,))
    a = _row(kind="assumption", assumes="current field is the configured default")
    assert d.kind is EvidenceKind.DERIVED and a.kind is EvidenceKind.ASSUMPTION
    # row_id is a stable 16-hex content hash.
    assert _row().row_id == _row().row_id == parent.row_id
    assert re.fullmatch(r"[0-9a-f]{16}", parent.row_id)
    assert _row(t=13).row_id != parent.row_id
    assert d.row_id != _row(kind="derived", derived_from=(d.row_id,)).row_id
    # A serialized row_id must match the content.
    dumped = parent.model_dump(mode="json")
    assert EvidenceRow.model_validate_json(canonical_json(dumped)) == parent
    dumped["row_id"] = "0" * 16
    with pytest.raises(ValidationError):
        EvidenceRow.model_validate_json(canonical_json(dumped))
    # Rows are immutable.
    with pytest.raises(ValidationError):
        parent.t = 5  # type: ignore[misc]


# ---------------------------------------------------------------- scenarios


def _scenario_data(name: str) -> dict[str, Any]:
    path = next((REPO_ROOT / "scenarios").glob(f"{name}-*.yaml"))
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def test_scenario_validators() -> None:
    s05 = _scenario_data("S05")
    s02 = _scenario_data("S02")
    assert scenario_from_data(s05).id == "S05"
    assert scenario_from_data(s02).id == "S02"

    def with_behavior(data: dict[str, Any], **changes: Any) -> dict[str, Any]:
        c = dict(data["contacts"][0])
        b = {k: v for k, v in c["behavior"].items() if changes.get(k, 0) is not None}
        b.update({k: v for k, v in changes.items() if v is not None})
        c["behavior"] = b
        return {**data, "contacts": [c]}

    # transit: neither or both of target_asset / waypoint
    with pytest.raises(ValidationError, match="exactly one"):
        scenario_from_data(with_behavior(s05, target_asset=None))
    with pytest.raises(ValidationError, match="exactly one"):
        scenario_from_data(with_behavior(s05, waypoint={"lat": 33.74, "lon": -118.26}))
    wp = scenario_from_data(
        with_behavior(s05, target_asset=None, waypoint={"lat": 33.74, "lon": -118.26})
    )
    assert wp.contacts[0].behavior.waypoint is not None
    with pytest.raises(ValidationError, match="speed_mps"):
        scenario_from_data(with_behavior(s05, speed_mps=None))
    # lawnmower_then_drift: fault_tick must fall inside the run
    with pytest.raises(ValidationError, match="fault_tick"):
        scenario_from_data(with_behavior(s02, fault_tick=s02["duration_ticks"]))
    # orbit and lawnmower requirements
    with pytest.raises(ValidationError, match="orbit"):
        scenario_from_data(with_behavior(s05, type="orbit", speed_mps=None))
    with pytest.raises(ValidationError, match="lawnmower needs area"):
        scenario_from_data(with_behavior(s05, type="lawnmower", target_asset=None))
    # plan references and windows
    c = dict(s02["contacts"][0])
    c["registry"] = {**c["registry"], "plan_id": "P-NOT-FILED"}
    with pytest.raises(ValidationError, match="not in registry_plans"):
        scenario_from_data({**s02, "contacts": [c]})
    plan = {**s02["registry_plans"][0], "window": {"start_tick": 0, "end_tick": 901}}
    with pytest.raises(ValidationError, match="window"):
        scenario_from_data({**s02, "registry_plans": [plan]})
    c = dict(s02["contacts"][0])
    c["registry"] = {**c["registry"], "silent_after_tick": 900}
    with pytest.raises(ValidationError, match="silent_after_tick"):
        scenario_from_data({**s02, "contacts": [c]})
    # expected rejects unknown keys
    with pytest.raises(ValidationError):
        scenario_from_data({**s05, "expected": {**s05["expected"], "surprise_key": 1}})
    with pytest.raises(ValidationError):
        scenario_from_data({**s05, "unknown_top_level": 1})
    # toggles: bare strings coerce, off-list names fail
    sc = scenario_from_data({**s05, "toggles": ["network_down", {"name": "contact_surge", "n": 5}]})
    assert sc.toggles[0] == Toggle(name=ToggleName.NETWORK_DOWN)
    assert sc.toggles[1].n == 5
    with pytest.raises(ValidationError):
        scenario_from_data({**s05, "toggles": ["not_a_toggle"]})
    with pytest.raises(ValidationError):
        quiet = {"name": "contact_goes_quiet", "contact_id": "gt-9"}
        scenario_from_data({**s05, "toggles": [quiet]})
    # ids, duplicates, strict ints
    for bad_id in ("S5", "smoke2", "L-2026"):
        with pytest.raises(ValidationError):
            scenario_from_data({**s05, "id": bad_id})
    assert scenario_from_data({**s05, "id": "smoke"}).id == "smoke"
    assert scenario_from_data({**s05, "id": "L-20261017T020000"}).id == "L-20261017T020000"
    with pytest.raises(ValidationError, match="unique"):
        scenario_from_data({**s05, "contacts": s05["contacts"] * 2})
    with pytest.raises(ValidationError):
        scenario_from_data({**s05, "seed": True})
    with pytest.raises(ValidationError):
        scenario_from_data({**s05, "hour_of_day": 24})
    # acoustic fields default to None (sampled by the sim)
    c = {k: v for k, v in s05["contacts"][0].items() if k != "acoustic"}
    assert scenario_from_data({**s05, "contacts": [c]}).contacts[0].acoustic.tonal_band is None


def test_load_scenario_resolves_harbor_ids(tmp_path: Path) -> None:
    s05 = _scenario_data("S05")
    s05["contacts"][0]["behavior"]["target_asset"] = "no_such_asset"
    p = tmp_path / "S99-bad.yaml"
    p.write_text(yaml.safe_dump(s05), encoding="utf-8")
    with pytest.raises(ValueError, match="unresolved harbor ids"):
        scenario.load_scenario(p)


# ---------------------------------------------------------------- tracks


def _pt(tick: int, lat: float = 33.74, lon: float = -118.26, **kw: Any) -> TrackPoint:
    return TrackPoint(
        tick=tick,
        lat=lat,
        lon=lon,
        depth_m=kw.get("depth_m", 5.0),
        sigma_m=kw.get("sigma_m", 12.0),
        sensor_id="hyd_1",
        confidence=0.9,
    )


def test_track_validators() -> None:
    t = Track(contact_id="c-0001", tick_s=2.0, points=(_pt(0), _pt(1), _pt(5)), gaps=(
        TrackGap(start_tick=2, end_tick=4),
    ))  # fmt: skip
    assert t.gap_ratio == round6(3 / 6)
    with pytest.raises(ValidationError, match="rise strictly"):
        Track(contact_id="c-0001", tick_s=2.0, points=(_pt(0), _pt(0)))
    with pytest.raises(ValidationError, match="rise strictly"):
        Track(contact_id="c-0001", tick_s=2.0, points=(_pt(3), _pt(1)))
    with pytest.raises(ValidationError, match="disjoint"):
        Track(
            contact_id="c-0001",
            tick_s=2.0,
            points=(_pt(0), _pt(10)),
            gaps=(TrackGap(start_tick=1, end_tick=5), TrackGap(start_tick=5, end_tick=6)),
        )
    with pytest.raises(ValidationError, match="disjoint"):
        Track(
            contact_id="c-0001",
            tick_s=2.0,
            points=(_pt(0), _pt(10)),
            gaps=(TrackGap(start_tick=6, end_tick=7), TrackGap(start_tick=1, end_tick=2)),
        )
    with pytest.raises(ValidationError, match="covers tick"):
        Track(
            contact_id="c-0001",
            tick_s=2.0,
            points=(_pt(0), _pt(3), _pt(10)),
            gaps=(TrackGap(start_tick=1, end_tick=5),),
        )
    with pytest.raises(ValidationError):
        TrackGap(start_tick=5, end_tick=4)
    for lat, lon in ((33.69, -118.26), (33.79, -118.26), (33.74, -118.31), (33.74, -118.21)):
        with pytest.raises(ValidationError):
            _pt(0, lat=lat, lon=lon)
    with pytest.raises(ValidationError):
        _pt(0, depth_m=-0.1)
    with pytest.raises(ValidationError):
        _pt(0, sigma_m=0.0)
    with pytest.raises(ValidationError):
        Track(contact_id="c-0001", tick_s=2.0, points=())
    with pytest.raises(ValidationError):
        Track(contact_id="c-0001", tick_s=0.0, points=(_pt(0),))
    with pytest.raises(ValidationError):
        Contact(contact_id="c-0002", track=t)
    with pytest.raises(ValidationError):
        Contact(contact_id="c-0001", track=t, reacquired_from="c-0001")
    assert Contact(contact_id="c-0001", track=t, reacquired_from="c-0002").reacquired_from
    assert AcousticReading().tonal_band == UNKNOWN
    assert AcousticReading(tonal_band="mid").tonal_band is TonalBand.MID


# ---------------------------------------------------------------- property round trips

_clean_text = st.text(alphabet="abcdefghiklopsuvwxyz _-.", min_size=1, max_size=40).filter(
    lambda s: s.strip() != "" and not find_denylisted(s)
)
_floats = st.floats(allow_nan=False, allow_infinity=False, width=64)
_values = st.one_of(
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    _floats,
    st.text(max_size=20),
    st.just(UNKNOWN),
)
_row_ids = st.text(alphabet="0123456789abcdef", min_size=16, max_size=16)
_contact_ids = st.integers(min_value=0, max_value=999_999).map(lambda n: f"c-{n:04d}")


@st.composite
def _rows(draw: st.DrawFn) -> EvidenceRow:
    kind = draw(st.sampled_from(list(EvidenceKind)))
    return EvidenceRow(
        contact_id=draw(_contact_ids),
        t=draw(st.integers(min_value=0, max_value=10**6)),
        feature=draw(st.sampled_from(list(FeatureName))),
        value=draw(_values),
        unit=draw(st.sampled_from(list(Unit))),
        source=draw(st.from_regex(r"[a-z0-9_.:-]{1,24}", fullmatch=True)),
        confidence=draw(st.one_of(st.floats(min_value=0.0, max_value=1.0), st.just(UNKNOWN))),
        kind=kind,
        probe_id=draw(st.one_of(st.none(), st.sampled_from(list(ProbeId)))),
        derived_from=tuple(draw(st.lists(_row_ids, min_size=1, max_size=3)))
        if kind is EvidenceKind.DERIVED
        else (),
        assumes=draw(_clean_text) if kind is EvidenceKind.ASSUMPTION else "",
    )


@PROPS
@given(_rows())
def test_roundtrip_evidence_row_property(row: EvidenceRow) -> None:
    back = EvidenceRow.model_validate_json(row.model_dump_json())
    assert back == row
    assert back.row_id == row.row_id
    assert canonical_json(back) == canonical_json(row)
    assert EvidenceRow.model_validate(row.model_dump(exclude={"row_id"})) == row


_acoustic = st.builds(
    AcousticReading,
    propeller_tonal_present=st.one_of(st.booleans(), st.just(UNKNOWN)),
    tonal_band=st.one_of(st.sampled_from(list(TonalBand)), st.just(UNKNOWN)),
    cavitation_index=st.one_of(st.floats(min_value=0.0, max_value=5.0), st.just(UNKNOWN)),
    breathing_or_click_present=st.one_of(st.booleans(), st.just(UNKNOWN)),
    broadband_level=st.one_of(st.floats(min_value=-200.0, max_value=200.0), st.just(UNKNOWN)),
)


@st.composite
def _contacts(draw: st.DrawFn) -> Contact:
    ticks = sorted(draw(st.sets(st.integers(min_value=0, max_value=500), min_size=1, max_size=12)))
    cid = draw(_contact_ids)
    points = tuple(
        TrackPoint(
            tick=tick,
            lat=draw(st.floats(min_value=33.70, max_value=33.78)),
            lon=draw(st.floats(min_value=-118.30, max_value=-118.22)),
            depth_m=draw(st.floats(min_value=0.0, max_value=40.0)),
            sigma_m=draw(st.floats(min_value=0.5, max_value=50.0)),
            sensor_id=draw(st.sampled_from(["hyd_1", "hyd_2", "hyd_3", "hyd_4"])),
            confidence=draw(st.floats(min_value=0.0, max_value=1.0)),
            acoustic=draw(_acoustic),
        )
        for tick in ticks
    )
    gaps = []
    for a, b in zip(ticks, ticks[1:], strict=False):
        if b - a >= 2 and draw(st.booleans()):
            lo = draw(st.integers(min_value=a + 1, max_value=b - 1))
            hi = draw(st.integers(min_value=lo, max_value=b - 1))
            gaps.append(TrackGap(start_tick=lo, end_tick=hi))
    t = Track(
        contact_id=cid,
        tick_s=draw(st.floats(min_value=0.1, max_value=10.0)),
        points=points,
        gaps=tuple(gaps),
    )
    other = draw(st.one_of(st.none(), _contact_ids.filter(lambda c: c != cid)))
    return Contact(contact_id=cid, track=t, reacquired_from=other)


@PROPS
@given(_contacts())
def test_roundtrip_track_property(contact: Contact) -> None:
    t = contact.track
    assert Track.model_validate_json(t.model_dump_json()) == t
    back = Contact.model_validate_json(contact.model_dump_json())
    assert back == contact
    assert canonical_json(back) == canonical_json(contact)
    assert 0.0 <= t.gap_ratio <= 1.0


def test_roundtrip_all_loaded_schemas() -> None:
    h = load_harbor()
    objs: list[BaseModel] = [
        h,
        load_likelihood_spec(),
        load_priors(),
        load_ladder_config(),
        load_standing_orders(h),
        *load_scenarios(REPO_ROOT / "scenarios"),
    ]
    for obj in objs:
        back = type(obj).model_validate_json(obj.model_dump_json())
        assert back == obj, type(obj).__name__
        assert stable_hash(back) == stable_hash(obj)


# ---------------------------------------------------------------- canonical JSON


def test_canonical_json_6dp() -> None:
    assert FLOAT_DECIMALS == 6
    # Binary floats round half-even on their exact value: both literals sit just below .5
    assert round6(0.1234565) == 0.123456
    assert round6(0.1234564) == 0.123456
    assert round6(0.1234566) == 0.123457
    assert canonical_json({"x": 0.1234565}) == '{"x":0.123456}'
    assert canonical_json({"x": 0.1234566}) == '{"x":0.123457}'
    assert canonical_json(-0.0) == "0.0"
    assert canonical_json({"z": -0.0000001}) == '{"z":0.0}'
    assert round6(-0.0) == 0.0 and math.copysign(1.0, round6(-0.0)) == 1.0
    assert canonical_json({"b": 1, "a": {"d": 2, "c": 3}}) == '{"a":{"c":3,"d":2},"b":1}'
    assert canonical_json(Rung.HOLD) == '"hold"'
    assert canonical_json({Rung.ASK: 1}) == '{"ask":1}'
    row = _row()
    assert canonical_json(row) == canonical_json(row.model_dump(mode="json"))
    assert '"row_id"' in canonical_json(row)
    for bad in (math.nan, math.inf, {"x": [math.nan]}):
        with pytest.raises(ValueError):
            canonical_json(bad)
    with pytest.raises(ValueError):
        round6(math.nan)
    with pytest.raises(TypeError):
        canonical_json({1: "x"})
    with pytest.raises(TypeError):
        canonical_json({"s": {1, 2}})
    assert stable_hash({"a": 1.0}) == stable_hash({"a": 1.0000001}) != stable_hash({"a": 2})
    assert len(stable_hash([])) == 64
    assert llm_client.canonical_json is canonical_mod.canonical_json
    assert llm_client.FLOAT_DECIMALS == canonical_mod.FLOAT_DECIMALS


# ---------------------------------------------------------------- config loaders


def test_likelihood_bucket_names() -> None:
    raw = yaml.safe_load((REPO_ROOT / "config" / "likelihoods.yaml").read_text(encoding="utf-8"))
    spec = load_likelihood_spec()
    assert list(spec.features) == list(raw["features"])
    for name, block in raw["features"].items():
        fb = spec.features[FeatureName(name)]
        expected = tuple(("true" if b else "false") if isinstance(b, bool) else b
                         for b in block["buckets"])  # fmt: skip
        assert fb.buckets == expected
        assert all(isinstance(b, str) for b in fb.buckets)
        if "thresholds" in block:
            assert fb.thresholds == tuple(block["thresholds"])
            assert fb.thresholds is not None and len(fb.thresholds) == len(fb.buckets) - 1
        else:
            assert fb.thresholds is None
        assert set(fb.rows) == set(Hypothesis)
        assert all(len(r) == len(fb.buckets) for r in fb.rows.values())
    assert spec.features[FeatureName.PROPELLER_TONAL_PRESENT].buckets == ("false", "true")
    assert spec.features[FeatureName.BREATHING_OR_CLICK_PRESENT].buckets == ("false", "true")


def test_config_loaders_reject_bad_shapes(tmp_path: Path) -> None:
    raw = yaml.safe_load((REPO_ROOT / "config" / "likelihoods.yaml").read_text(encoding="utf-8"))
    block = raw["features"]["depth"]
    bad_cases = [
        {**block, "thresholds": [1.0, 4.0]},
        {**block, "BIOLOGIC": [0.5, 0.5]},
        {k: v for k, v in block.items() if k != "BIOLOGIC"},
        {**block, "NOT_A_HYPOTHESIS": [0.25, 0.25, 0.25, 0.25]},
    ]
    for i, bad in enumerate(bad_cases):
        p = tmp_path / f"lk{i}.yaml"
        p.write_text(yaml.safe_dump({"features": {"depth": bad}}), encoding="utf-8")
        with pytest.raises(ValidationError):
            load_likelihood_spec(p)
    p = tmp_path / "lk_badname.yaml"
    p.write_text(yaml.safe_dump({"features": {"not_a_feature": block}}), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_likelihood_spec(p)

    pri = yaml.safe_load((REPO_ROOT / "config" / "priors.yaml").read_text(encoding="utf-8"))
    del pri["base"]["BIOLOGIC"]
    p = tmp_path / "priors.yaml"
    p.write_text(yaml.safe_dump(pri), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_priors(p)

    lad = yaml.safe_load((REPO_ROOT / "config" / "ladder.yaml").read_text(encoding="utf-8"))
    p = tmp_path / "ladder.yaml"
    swapped = ["watch", "ask", "hold", "slow", "hand_to_person"]
    p.write_text(yaml.safe_dump({**lad, "rungs": swapped}))
    with pytest.raises(ValidationError):
        load_ladder_config(p)
    p.write_text(yaml.safe_dump({**lad, "lives_on": ["ask", "climb"]}))
    with pytest.raises(ValidationError):
        load_ladder_config(p)

    so = yaml.safe_load((REPO_ROOT / "config" / "standing_orders.yaml").read_text(encoding="utf-8"))
    so["orders"]["scope"]["slow"]["basins"] = ["atlantis"]
    p = tmp_path / "so.yaml"
    p.write_text(yaml.safe_dump(so), encoding="utf-8")
    with pytest.raises(ValueError, match="basins"):
        load_standing_orders(load_harbor(), path=p)
    so["orders"]["scope"]["slow"]["basins"] = ["inner_harbor_west"]
    so["orders"]["degraded_mode"]["allowed_rungs"] = ["watch", "climb"]
    p.write_text(yaml.safe_dump(so), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_standing_orders(load_harbor(), path=p)


# ---------------------------------------------------------------- geo and harbor


def test_geo_helpers() -> None:
    assert to_local_m(33.74, -118.265) == (0.0, 0.0)
    for lat, lon in ((33.7350, -118.2620), (33.71, -118.29), (33.775, -118.225)):
        x, y = to_local_m(lat, lon)
        lat2, lon2 = from_local_m(x, y)
        assert abs(lat2 - lat) < 1e-12 and abs(lon2 - lon) < 1e-12
    # One thousandth of a degree of latitude is about 111 m.
    assert 110.0 < distance_m(33.740, -118.26, 33.741, -118.26) < 112.0
    assert distance_m(33.74, -118.26, 33.75, -118.25) == distance_m(33.75, -118.25, 33.74, -118.26)
    square = ((33.0, -118.0), (34.0, -118.0), (34.0, -117.0), (33.0, -117.0))
    assert point_in_polygon(33.5, -117.5, square)
    assert not point_in_polygon(34.5, -117.5, square)
    assert point_in_polygon(33.5, -117.5, (*square, square[0]))  # closed ring too
    assert not point_in_polygon(33.5, -117.5, square[:2])
    concave = ((0.0, 0.0), (0.0, 4.0), (4.0, 4.0), (4.0, 3.0), (1.0, 3.0), (1.0, 0.0))
    assert point_in_polygon(0.5, 0.5, concave)
    assert not point_in_polygon(2.0, 1.0, concave)


def _tiny_harbor(**over: Any) -> Harbor:
    sq = ((33.73, -118.27), (33.75, -118.27), (33.75, -118.25), (33.73, -118.25))
    data: dict[str, Any] = {
        "name": "Test harbor",
        "zones": (
            {"zone_id": "b_zone", "name": "B", "criticality": 0.5, "basin": "west", "polygon": sq},
            {"zone_id": "a_zone", "name": "A", "criticality": 0.5, "basin": "east", "polygon": sq},
            {"zone_id": "c_zone", "name": "C", "criticality": 0.2, "basin": "west", "polygon": sq},
        ),
        "assets": (
            {"asset_id": "b_berth", "name": "B", "criticality": 0.9, "asset_kind": "berth",
             "lat": 33.741, "lon": -118.26},
            {"asset_id": "a_berth", "name": "A", "criticality": 0.9, "asset_kind": "berth",
             "lat": 33.739, "lon": -118.26},
            {"asset_id": "gate_1", "name": "G", "criticality": 0.3, "asset_kind": "gate",
             "lat": 33.7401, "lon": -118.26},
        ),
        "sensor_nodes": (
            {"node_id": "hyd_1", "name": "H", "lat": 33.74, "lon": -118.26, "range_m": 400.0,
             "health": 1.0},
        ),
    }  # fmt: skip
    data.update(over)
    return Harbor.model_validate(data)


def test_harbor_methods() -> None:
    h = _tiny_harbor()
    z = h.zone_at(33.74, -118.26)
    assert z is not None and z.zone_id == "a_zone"  # tie on criticality: lowest id
    assert h.zone_at(33.76, -118.26) is None
    near = h.nearest_asset(33.74, -118.26)
    assert near is not None and near.asset_id == "gate_1"
    crit = h.nearest_asset(33.74, -118.26, min_criticality=0.5)
    assert crit is not None and crit.asset_id == "a_berth"  # equidistant: lowest id
    assert h.nearest_asset(33.74, -118.26, min_criticality=0.99) is None
    assert h.basins() == ["east", "west"]
    assert h.zone("c_zone").criticality == 0.2 and h.asset("gate_1").asset_kind == "gate"
    assert h.node("hyd_1").range_m == 400.0
    for lookup in (h.zone, h.asset, h.node):
        with pytest.raises(KeyError):
            lookup("nope")
    dup_node = {"node_id": "a_zone", "name": "H", "lat": 33.74, "lon": -118.26,
                "range_m": 400.0, "health": 1.0}  # fmt: skip
    with pytest.raises(ValidationError, match="unique"):
        _tiny_harbor(sensor_nodes=(dup_node,))
    two_vertices = {"zone_id": "z", "name": "Z", "criticality": 0.5, "basin": "west",
                    "polygon": ((33.73, -118.27), (33.75, -118.27))}  # fmt: skip
    with pytest.raises(ValidationError):
        _tiny_harbor(zones=(two_vertices,))


def test_harbor_geojson_shape() -> None:
    import json

    doc = json.loads((REPO_ROOT / "config" / "harbor.geojson").read_text(encoding="utf-8"))
    assert doc["type"] == "FeatureCollection"
    assert doc["name"] == "Port of Los Angeles (simplified for demonstration)"
    kinds = sorted({f["properties"]["kind"] for f in doc["features"]})
    assert kinds == ["asset", "sensor_node", "zone"]
    bad = {**doc, "features": [*doc["features"], {**doc["features"][0], "properties": {
        **doc["features"][0]["properties"], "kind": "mystery"}}]}  # fmt: skip
    with pytest.raises(ValueError):
        harbor.harbor_from_geojson(bad)
