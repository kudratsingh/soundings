"""The evidence table: append-only invariants, carry-over, and tables built from
synthetic WO-010 tracks (completeness, fact/assumption disjointness, provenance).

The integration twin over real WO-011 runs is ``tests/test_evidence.py``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError
from synthetic_tracks import (
    FUEL_BERTHS_XY,
    MAIN_CHANNEL_XY,
    OUTSIDE_ZONES_XY,
    FakeRun,
    calm,
    line_track,
    make_track,
    orbit,
    straight,
    to_track,
)

from soundings.investigation import features as F
from soundings.investigation.evidence import EvidenceError, EvidenceTable, carry_over_reacquired
from soundings.physics import load_physics_config
from soundings.schemas.enums import EvidenceKind, FeatureName, TonalBand, Unit
from soundings.schemas.evidence import UNKNOWN, EvidenceRow, EvidenceValue
from soundings.schemas.harbor import load_harbor
from soundings.schemas.track import AcousticReading, Track

REPO = Path(__file__).resolve().parent.parent
HARBOR = load_harbor()
NINE_COLUMNS = ("contact_id", "t", "feature", "value", "unit", "source", "confidence", "kind")


def _fact(
    feature: FeatureName = FeatureName.DEPTH,
    *,
    contact_id: str = "c-0001",
    t: int = 5,
    value: EvidenceValue = 3.5,
    confidence: float = 0.8,
    source: str = "tracker",
) -> EvidenceRow:
    return EvidenceRow(
        contact_id=contact_id,
        t=t,
        feature=feature,
        value=value,
        unit=Unit.M,
        source=source,
        confidence=confidence,
        kind=EvidenceKind.FACT,
        probe_id=None,
    )


def _assumption(feature: FeatureName = FeatureName.CURRENT_FIELD, **kw: object) -> EvidenceRow:
    return EvidenceRow(
        contact_id=str(kw.get("contact_id", "c-0001")),
        t=5,
        feature=feature,
        value=0.1,
        unit=Unit.M_PER_S,
        source="config:current_field",
        confidence=1.0,
        kind=EvidenceKind.ASSUMPTION,
        probe_id=None,
        assumes="current field is the configured default; no live gauge",
    )


def _derived(sources: tuple[str, ...], confidence: float) -> EvidenceRow:
    return EvidenceRow(
        contact_id="c-0001",
        t=5,
        feature=FeatureName.SPEED_OVER_GROUND,
        value=1.0,
        unit=Unit.M_PER_S,
        source="extractor:speed_over_ground",
        confidence=confidence,
        kind=EvidenceKind.DERIVED,
        probe_id=None,
        derived_from=sources,
    )


# --- append and read ---------------------------------------------------------------


def test_append_returns_row_id_and_reads_back() -> None:
    table = EvidenceTable()
    a = _fact(t=5)
    b = _fact(t=9, value=4.0)
    c = _fact(contact_id="c-0002")
    assert table.append(a) == a.row_id
    assert table.extend([b, c]) == [b.row_id, c.row_id]
    assert table.rows() == (a, b, c)
    assert table.for_contact("c-0001") == table.rows_for("c-0001") == (a, b)
    assert table.contacts() == ("c-0001", "c-0002")
    assert table.latest("c-0001", "depth") == b
    assert table.latest("c-0001", FeatureName.TURN_RATE) is None
    assert table.get(a.row_id) is a and a.row_id in table and len(table) == 3
    assert table.facts("c-0001") == {FeatureName.DEPTH}
    assert table.assumptions("c-0001") == frozenset()


def test_append_rejects_unknown_derived_from() -> None:
    table = EvidenceTable()
    with pytest.raises(EvidenceError, match="not in the table"):
        table.append(_derived(("0123456789abcdef",), 0.5))
    assert len(table) == 0


def test_append_rejects_duplicate_row_id() -> None:
    table = EvidenceTable()
    table.append(_fact())
    with pytest.raises(EvidenceError, match="already in the table"):
        table.append(_fact())
    assert len(table) == 1


@pytest.mark.parametrize("fact_first", [True, False])
def test_append_rejects_fact_assumption_collision(fact_first: bool) -> None:
    table = EvidenceTable()
    fact = _fact(FeatureName.CURRENT_FIELD)
    assumption = _assumption(FeatureName.CURRENT_FIELD)
    first, second = (fact, assumption) if fact_first else (assumption, fact)
    table.append(first)
    with pytest.raises(EvidenceError, match="cannot also be"):
        table.append(second)
    assert table.rows() == (first,)
    # another contact may label the same feature the other way
    table.append(_fact(FeatureName.CURRENT_FIELD, contact_id="c-0009"))


def test_append_rejects_confidence_over_the_ceiling() -> None:
    table = EvidenceTable()
    low = _fact(confidence=0.4)
    high = _fact(FeatureName.DEPTH_VARIANCE, confidence=0.9)
    table.extend([low, high])
    with pytest.raises(EvidenceError, match="exceeds"):
        table.append(_derived((low.row_id, high.row_id), 0.41))
    # within the 1e-9 tolerance, and at or below the minimum, is fine
    table.append(_derived((low.row_id, high.row_id), 0.4))
    table.append(_derived((high.row_id,), 0.9))


def test_no_mutation_api() -> None:
    table = EvidenceTable()
    row = _fact()
    table.append(row)
    for name in ("update", "delete", "remove", "pop", "clear", "insert", "replace", "set"):
        assert not hasattr(table, name), name
    assert not hasattr(table, "__setitem__") and not hasattr(table, "__delitem__")
    assert isinstance(table.rows(), tuple)
    with pytest.raises(TypeError):
        table.index()[row.row_id] = row  # type: ignore[index]
    with pytest.raises(ValidationError):
        row.value = 9.0  # type: ignore[misc]
    assert table.rows() == (row,)


def test_rejects_non_rows() -> None:
    with pytest.raises(EvidenceError):
        EvidenceTable().append({"contact_id": "c-0001"})  # type: ignore[arg-type]


def test_json_round_trip_and_hash() -> None:
    table = build({"c-0001": line_track(40), "c-0002": line_track(3, contact_id="c-0002")})
    text = table.to_json()
    again = EvidenceTable.from_json(text)
    assert again.rows() == table.rows()
    assert again.table_hash() == table.table_hash()
    assert again.to_json() == text
    assert '"probe_id":null' in text and '"row_id"' in text


# --- carry over (SPEC-INV 9.9 groundwork) ------------------------------------------


def _s02_style_table() -> tuple[EvidenceTable, Track, Track]:
    """A contact seen for 200 s, lost for 60 s (> 15 ticks), seen again under a new id."""
    first = to_track(
        straight(speed=0.8, heading_deg=45.0, duration_s=200.0), origin_xy=MAIN_CHANNEL_XY
    )
    t = np.arange(130, 230, dtype=np.float64) * 2.0
    second = to_track(
        make_track(t, 0.57 * t, 0.57 * t, confidence=0.6),
        contact_id="c-0002",
        origin_xy=MAIN_CHANNEL_XY,
    )
    return build({"c-0001": first, "c-0002": second}), first, second


def test_carry_over_reacquired_rekeys_every_row_as_derived() -> None:
    table, _, _ = _s02_style_table()
    before = table.rows()
    old_rows = table.for_contact("c-0001")
    new_before = table.for_contact("c-0002")
    ids = carry_over_reacquired(
        table, "c-0001", "c-0002", t=130, match_confidence=0.55, reason="position and drift box"
    )
    # the old rows (and everything before) are unchanged
    assert table.rows()[: len(before)] == before
    assert table.for_contact("c-0001") == old_rows
    assert len(ids) == 1 + len(old_rows)
    assumption = table.get(ids[0])
    assert assumption.kind is EvidenceKind.ASSUMPTION
    assert assumption.feature is FeatureName.REACQUIRED_FROM
    assert assumption.value == "c-0001" and assumption.contact_id == "c-0002"
    assert assumption.assumes == "position and drift box"
    assert assumption.confidence == 0.55
    for original, new_id in zip(old_rows, ids[1:], strict=True):
        carried = table.get(new_id)
        assert carried.contact_id == "c-0002"
        assert carried.kind is EvidenceKind.DERIVED
        assert carried.derived_from == (original.row_id, assumption.row_id)
        assert (carried.feature, carried.value, carried.unit, carried.source, carried.t) == (
            original.feature,
            original.value,
            original.unit,
            original.source,
            original.t,
        )
        assert isinstance(original.confidence, float)
        assert carried.confidence == min(original.confidence, 0.55)
        assert carried.assumes == ""
    assert table.for_contact("c-0002")[: len(new_before)] == new_before


def test_carry_over_reacquired_rejects_bad_calls() -> None:
    table, _, _ = _s02_style_table()
    with pytest.raises(EvidenceError):
        carry_over_reacquired(table, "c-0001", "c-0001", t=1, match_confidence=0.5, reason="x")
    with pytest.raises(EvidenceError):
        carry_over_reacquired(table, "c-0404", "c-0002", t=1, match_confidence=0.5, reason="x")
    with pytest.raises(EvidenceError):
        carry_over_reacquired(table, "c-0001", "c-0002", t=1, match_confidence=1.5, reason="x")
    carry_over_reacquired(table, "c-0001", "c-0002", t=1, match_confidence=0.5, reason="x")
    with pytest.raises(EvidenceError):  # same carry twice: duplicate assumption row
        carry_over_reacquired(table, "c-0001", "c-0002", t=1, match_confidence=0.5, reason="x")


# --- tables built from synthetic tracks --------------------------------------------


def build(
    tracks: Mapping[str, Track], pol: F.PolBaseline | None = None, hour: int = 10
) -> EvidenceTable:
    return F.build_evidence(FakeRun(tracks=tracks), HARBOR, calm, pol, hour_of_day=hour)


def _acoustic(n: int) -> list[AcousticReading]:
    return [
        AcousticReading(
            propeller_tonal_present=i % 2 == 0,
            tonal_band=TonalBand.MID,
            cavitation_index=0.25,
            breathing_or_click_present=i == 3,
        )
        for i in range(n)
    ]


def _synthetic_fleet() -> dict[str, Track]:
    n_orbit = len(orbit(radius=40.0, turns=1.5))
    return {
        "c-0001": to_track(straight(duration_s=600.0, sigma_m=0.0), origin_xy=(100.0, -2500.0)),
        "c-0002": to_track(
            orbit(radius=40.0, turns=1.5, sigma_m=8.0, seed=2),
            contact_id="c-0002",
            origin_xy=(138.7, 278.0),
            sigma_m=8.0,
            sensor_id="hyd_1",
            acoustic=_acoustic(n_orbit),
        ),
        "c-0003": line_track(1, contact_id="c-0003"),
        "c-0004": line_track(12, contact_id="c-0004", origin_xy=OUTSIDE_ZONES_XY),
        "c-0005": line_track(80, contact_id="c-0005", origin_xy=FUEL_BERTHS_XY, speed=0.3),
    }


class _FakePol:
    seed = 42

    def anomaly(
        self, zone_id: str, hour: int, weekday: str, observed: Mapping[str, EvidenceValue]
    ) -> float | None:
        if zone_id == "fuel_berths":
            return None  # an empty cell
        return 0.25 if hour < 12 else 0.9


@pytest.mark.parametrize("with_pol", [False, True])
def test_completeness_every_catalog_feature_every_column(with_pol: bool) -> None:
    table = build(_synthetic_fleet(), _FakePol() if with_pol else None)
    assert table.contacts() == tuple(sorted(_synthetic_fleet()))
    for contact_id in table.contacts():
        rows = table.for_contact(contact_id)
        features = {r.feature for r in rows}
        missing = [f.value for f in F.FEATURE_CATALOG if f not in features]
        assert not missing, (contact_id, missing)
        for row in rows:
            dumped = row.model_dump()
            for column in NINE_COLUMNS:
                assert dumped[column] is not None, (contact_id, row.feature, column)
            assert dumped["probe_id"] is None
            if row.value == UNKNOWN:
                assert row.confidence == 0.0
                assert row.source in F.UNKNOWN_SOURCES
            assert row.unit != Unit.UNKNOWN


def test_disjoint_facts_and_assumptions() -> None:
    table = build(_synthetic_fleet(), _FakePol())
    for contact_id in table.contacts():
        assert not (table.facts(contact_id) & table.assumptions(contact_id)), contact_id
        rows = table.for_contact(contact_id)
        facts = {r.feature for r in rows if r.kind is EvidenceKind.FACT}
        assumptions = {r.feature for r in rows if r.kind is EvidenceKind.ASSUMPTION}
        assert facts == table.facts(contact_id) and not facts & assumptions
        assert assumptions == {
            FeatureName.CURRENT_FIELD,
            FeatureName.WEEKDAY,
            FeatureName.POL_BASELINE,
            FeatureName.VISIBILITY_FOR_CAMERA,
        }
        for row in rows:
            if row.kind is EvidenceKind.ASSUMPTION:
                assert row.assumes.strip()
    row = table.latest("c-0001", FeatureName.POL_BASELINE)
    assert row is not None and row.assumes == F.pol_assumes(42)
    assert "seed 42" in row.assumes


def test_provenance_sources_come_first_and_cap_confidence() -> None:
    table = build(_synthetic_fleet(), _FakePol())
    seen: dict[str, EvidenceRow] = {}
    for row in table.rows():
        if row.kind is EvidenceKind.DERIVED:
            assert row.derived_from
            sources = [seen[s] for s in row.derived_from]  # KeyError = not earlier
            ceiling = min(s.confidence for s in sources)  # type: ignore[type-var]
            assert row.confidence <= ceiling + 1e-9  # type: ignore[operator]
        seen[row.row_id] = row
    for contact_id in table.contacts():
        current = table.latest(contact_id, FeatureName.CURRENT_FIELD)
        assert current is not None
        assert current.assumes == "current field is the configured default; no live gauge"
        drift = table.latest(contact_id, FeatureName.DRIFT_CONSISTENCY)
        assert drift is not None and current.row_id in drift.derived_from
        for feature in F.KINEMATIC_FEATURES:
            row = table.latest(contact_id, feature)
            assert row is not None
            assert table.get(row.derived_from[0]).feature is FeatureName.TRACK_SEGMENT


def test_provenance_pattern_of_life_rows() -> None:
    with_pol = build(_synthetic_fleet(), _FakePol())
    without = build(_synthetic_fleet(), None)
    for contact_id in with_pol.contacts():
        row = without.latest(contact_id, FeatureName.PATTERN_OF_LIFE_ANOMALY)
        assert row is not None and row.value == UNKNOWN and row.source == F.NO_POL_TABLE
        assert without.latest(contact_id, FeatureName.POL_BASELINE) is None
    # c-0001: main_channel, a populated cell
    row = with_pol.latest("c-0001", FeatureName.PATTERN_OF_LIFE_ANOMALY)
    pol_row = with_pol.latest("c-0001", FeatureName.POL_BASELINE)
    assert row is not None and pol_row is not None
    assert row.value == 0.25 and row.kind is EvidenceKind.DERIVED
    assert pol_row.row_id in row.derived_from
    cited = {with_pol.get(i).feature for i in row.derived_from}
    assert {FeatureName.ZONE_ID, FeatureName.HOUR_OF_DAY, FeatureName.WEEKDAY} <= cited
    assert set(F.POL_INPUT_FEATURES) <= cited
    # fuel_berths cell is empty; c-0004 is in no zone
    for contact_id in ("c-0005", "c-0004"):
        row = with_pol.latest(contact_id, FeatureName.PATTERN_OF_LIFE_ANOMALY)
        assert row is not None and row.value == UNKNOWN and row.source == F.POL_CELL_EMPTY
    night = build(_synthetic_fleet(), _FakePol(), hour=13)
    row = night.latest("c-0001", FeatureName.PATTERN_OF_LIFE_ANOMALY)
    assert row is not None and row.value == 0.9


def test_acoustic_passthrough_per_reading() -> None:
    table = build(_synthetic_fleet())
    track = _synthetic_fleet()["c-0002"]
    clicks = table.feature_rows("c-0002", FeatureName.BREATHING_OR_CLICK_PRESENT)
    assert len(clicks) == len(track.points)
    assert [r.t for r in clicks] == [p.tick for p in track.points]
    assert [r.value for r in clicks].count(True) == 1
    assert all(
        r.source == "acoustic_detector:hyd_1" and r.kind is EvidenceKind.FACT for r in clicks
    )
    assert [r.confidence for r in clicks] == [p.confidence for p in track.points]
    band = table.latest("c-0002", FeatureName.TONAL_BAND)
    assert band is not None and band.value == "mid" and type(band.value) is str
    # broadband_level is never reported: one unknown row
    (level,) = table.feature_rows("c-0002", FeatureName.BROADBAND_LEVEL)
    assert level.value == UNKNOWN and level.source == F.NOT_REPORTED_BY_DETECTOR
    # a contact with no acoustic reports: every acoustic feature is one unknown row
    for feature in F.ACOUSTIC_FEATURES:
        (row,) = table.feature_rows("c-0001", feature)
        assert row.value == UNKNOWN and row.source == F.NOT_REPORTED_BY_DETECTOR


def test_context_rows() -> None:
    fleet = _synthetic_fleet()
    table = build(fleet, hour=22)
    for contact_id in table.contacts():
        ais = table.latest(contact_id, FeatureName.AIS_SURFACE_CORRELATION)
        assert ais is not None and ais.value == UNKNOWN and ais.source == F.NO_AIS_FEED
        for feature in F.REGISTRY_FEATURES:
            (row,) = table.feature_rows(contact_id, feature)
            assert row.value == UNKNOWN and row.source == F.PLACEHOLDER_PHASE2
            assert row.kind is EvidenceKind.FACT
        visibility = table.latest(contact_id, FeatureName.VISIBILITY_FOR_CAMERA)
        assert visibility is not None and visibility.kind is EvidenceKind.ASSUMPTION
        # WO-089: the dark default times exp(-k depth) exp(-turbidity) at the last depth
        cam = load_physics_config().camera
        depth = max(fleet[contact_id].points[-1].depth_m, 0.0)
        attenuation = math.exp(-cam.k_depth_per_m.value * depth)
        attenuation *= math.exp(-cam.turbidity_default.value)
        assert visibility.value == round(F.VISIBILITY_DARK * attenuation, 6)
        assert isinstance(visibility.value, float) and 0.0 < visibility.value <= F.VISIBILITY_DARK
        weekday = table.latest(contact_id, FeatureName.WEEKDAY)
        assert weekday is not None and weekday.value == F.DEFAULT_WEEKDAY in F.WEEKDAYS
    # c-0002 orbits fuel_berth_1, reported by hyd_1 (also in range)
    health = table.feature_rows("c-0002", FeatureName.SENSOR_HEALTH)
    assert [r.source for r in health] == ["sensor:hyd_1"]
    assert all(r.value == 1.0 and r.kind is EvidenceKind.FACT for r in health)
    # hour_of_day = scenario hour + elapsed time at the last tick
    long = line_track(1900, contact_id="c-0007", speed=0.1)  # last tick 1899 -> 3798 s
    hour = build({"c-0007": long}, hour=23).latest("c-0007", FeatureName.HOUR_OF_DAY)
    assert hour is not None and hour.value == 0 and hour.unit == Unit.HOUR
    assert F.hour_at(23, 3598.0) == 23 and F.hour_at(23, 3600.0) == 0


def test_current_at_from_field_reads_lat_lon_and_tide_phase() -> None:
    calls: list[tuple[float, float, str]] = []

    def vector_at(lat: float, lon: float, phase: str) -> tuple[float, float]:
        calls.append((lat, lon, phase))
        return (0.1, 0.0) if phase == "flood" else (0.0, -0.2)

    def phase_for_hour(hour: int) -> str:
        return "flood" if hour < 12 else "ebb"

    current_at = F.current_at_from_field(vector_at, phase_for_hour, hour_of_day=11)
    assert current_at(0.0, 0.0, 0.0) == (0.1, 0.0)
    assert current_at(0.0, 0.0, 3600.0) == (0.0, -0.2)
    assert calls[0][:2] == pytest.approx((33.74, -118.265))

    class Field:
        def vector_at(self, lat: float, lon: float, phase: str) -> tuple[float, float]:
            return vector_at(lat, lon, phase)

    tracks = {"c-0001": line_track(40)}
    via_field = F.build_evidence(
        FakeRun(tracks=tracks),
        HARBOR,
        Field(),
        None,
        hour_of_day=11,
        tide_phase_for_hour=phase_for_hour,
    )
    row = via_field.latest("c-0001", FeatureName.CURRENT_FIELD)
    assert row is not None and row.value == 0.1
    with pytest.raises(TypeError):
        F.build_evidence(FakeRun(tracks=tracks), HARBOR, Field(), None, hour_of_day=11)


def test_build_rejects_mismatched_runs() -> None:
    with pytest.raises(ValueError):
        build({"c-0002": line_track(5)})  # key and contact_id differ
    with pytest.raises(ValueError):
        F.build_evidence(
            FakeRun(tracks={"c-0001": line_track(5)}, tick_s=1.0), HARBOR, calm, hour_of_day=1
        )


def test_determinism_same_inputs_same_table() -> None:
    fleet = _synthetic_fleet()
    a = build(fleet, _FakePol())
    b = build(dict(reversed(list(fleet.items()))), _FakePol())
    assert a.to_json() == b.to_json()
    assert a.table_hash() == b.table_hash() == build(_synthetic_fleet(), _FakePol()).table_hash()


# --- guards: observed tracks only, no model path, clean vocabulary ------------------

INVESTIGATION = REPO / "soundings" / "investigation"
GROUND_TRUTH = re.compile(
    r"contact_truth|truth_paths|gt_id|is_false_alarm|ground_truth|\.detections"
)


def test_ground_truth_guard_grep() -> None:
    files = [INVESTIGATION / "evidence.py", INVESTIGATION / "features.py"]
    main = INVESTIGATION / "__main__.py"
    if main.exists():
        files.append(main)
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not GROUND_TRUTH.search(text), path.name


def test_no_model_path_feeds_the_table() -> None:
    for path in sorted(INVESTIGATION.glob("*.py")):
        assert "soundings.llm" not in path.read_text(encoding="utf-8"), path.name


def test_denylist_clean() -> None:
    patterns = [
        re.compile(line.strip(), re.IGNORECASE)
        for line in (REPO / "config" / "denylist.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    paths = sorted(INVESTIGATION.glob("*.py"))
    paths += [
        p
        for p in sorted((REPO / "tests").glob("*.py"))
        if p.name.startswith(("test_evidence", "test_features", "test_transit", "test_pol"))
        or p.name == "synthetic_tracks.py"
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            assert not pattern.search(text), (path.name, pattern.pattern)
