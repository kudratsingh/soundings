"""The ask-rung matcher: registry and hail evidence to a reason-coded outcome (WO-022).

Everything here is pure (CLAUDE.md 2.2 and 2.4): no I/O, no clock, no globals, no model
output. ``load_match_config`` is the one function that reads files, and nothing else in
this module calls it. Inputs are frozen models; outputs are frozen models with floats
rounded to 6 dp.

* ``evidence_view`` snapshots what the matcher may read from the evidence table for one
  contact at one tick (the latest hail, the lineage's plan, when it was last heard).
* ``registry_lookup`` buckets a position (and, after a hail, a modem id) against the
  registry plans with the ``config/likelihoods.yaml`` ``registry_match`` names;
  ``registry_lookup_rows`` turns the result into evidence rows.
* ``match`` applies the five rules of the WO-022 decision table in order. Every outcome
  carries a ``MatchCode``, a one-line reason naming the numbers it used, and the sorted
  row ids it read (CLAUDE.md 2.6).
* ``drift_search_box`` advects a last-known fix through the configured current field for
  a LOST_VEHICLE notice; ``match_reacquisition`` links a returning track to a lost one
  (SPEC-INVESTIGATION 9.9).

Plans are WO-020 ``PlanView`` objects. A plan's window is its closed tick window
``[start_tick, end_tick]``; a plan filed without ticks (live-only) is never in window
here, as in ``RegistryStore.overlapping_ticks``. Only ``active`` plans count.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Protocol

import yaml
from pydantic import Field, StrictBool, StrictStr, model_validator

from soundings.canonical import round6
from soundings.investigation.evidence import EvidenceTable
from soundings.registry.models import PlanStatus, PlanView
from soundings.schemas._base import (
    CONFIG_DIR,
    NonNegFloat,
    NonNegInt,
    PositiveFloat,
    StrictModel,
    from_file_data,
)
from soundings.schemas.config import LADDER_PATH, load_ladder_config
from soundings.schemas.denylist import FreeText
from soundings.schemas.enums import (
    EvidenceKind,
    FeatureName,
    HailReply,
    ProbeId,
    RegistryMatch,
    Unit,
)
from soundings.schemas.evidence import UNKNOWN, EvidenceRow, RowId
from soundings.schemas.geo import distance_m, from_local_m, point_in_polygon, to_local_m
from soundings.schemas.harbor import Harbor
from soundings.schemas.track import Track

MATCHER_CONFIG_PATH: Path = CONFIG_DIR / "matcher.yaml"

REGISTRY_SOURCE = "probe:registry_lookup"
PLAN_SOURCE_PREFIX = "registry:"
"""``plan_id`` rows named by a modem identity (a hail reply). Lineage reads only these."""
PLAN_POSITION_SOURCE_PREFIX = "registry.position:"
"""``plan_id`` rows from a position-only lookup (watch rung, before any identity)."""
PLAN_ROW_CONFIDENCE = 1.0
TRACKER_SOURCE = "tracker"
HAIL_SOURCE = "probe:acoustic_hail"


# --- config ------------------------------------------------------------------------------


class AskConfig(StrictModel):
    max_hails_per_case: Annotated[int, Field(ge=1)] = 2


class PlanWatchConfig(StrictModel):
    rehail_interval_s: PositiveFloat = 300.0


class ReacquireConfig(StrictModel):
    max_gap_s: PositiveFloat = 600.0
    gate_base_m: PositiveFloat = 75.0
    gate_growth_mps: NonNegFloat = 0.15
    speed_tol_mps: PositiveFloat = 0.5


class SearchBoxConfig(StrictModel):
    base_radius_m: NonNegFloat = 50.0
    growth_mps: NonNegFloat = 0.10
    step_s: PositiveFloat = 30.0


class MatchConfig(StrictModel):
    """``config/matcher.yaml`` plus ``lost_vehicle_silence_min`` from ``config/ladder.yaml``."""

    area_tolerance_m: NonNegFloat = 150.0
    evidence_every_ticks: Annotated[int, Field(ge=1)] = 15
    watch_min_points: Annotated[int, Field(ge=1)] = 10
    ask: AskConfig = Field(default_factory=AskConfig)
    plan_watch: PlanWatchConfig = Field(default_factory=PlanWatchConfig)
    reacquire: ReacquireConfig = Field(default_factory=ReacquireConfig)
    search_box: SearchBoxConfig = Field(default_factory=SearchBoxConfig)
    lost_vehicle_silence_min: PositiveFloat = 10.0

    @property
    def lost_vehicle_silence_s(self) -> float:
        return self.lost_vehicle_silence_min * 60.0


def _number(value: Any) -> Any:
    """YAML ints become floats where a float field expects them (strict models)."""
    return float(value) if isinstance(value, int) and not isinstance(value, bool) else value


_FLOAT_KEYS = frozenset(
    {
        "area_tolerance_m",
        "rehail_interval_s",
        "max_gap_s",
        "gate_base_m",
        "gate_growth_mps",
        "speed_tol_mps",
        "base_radius_m",
        "growth_mps",
        "step_s",
        "lost_vehicle_silence_min",
    }
)


def _floats(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: (_number(v) if k in _FLOAT_KEYS else _floats(v)) for k, v in obj.items()}
    return obj


def load_match_config(
    path: str | Path = MATCHER_CONFIG_PATH, ladder_path: str | Path = LADDER_PATH
) -> MatchConfig:
    """Load ``config/matcher.yaml``; the silence threshold comes from ``config/ladder.yaml``."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    if "lost_vehicle_silence_min" in doc:
        raise ValueError("lost_vehicle_silence_min belongs to config/ladder.yaml, not here")
    ladder = load_ladder_config(ladder_path)
    data = dict(doc)
    data["lost_vehicle_silence_min"] = ladder.stopping["lost_vehicle_silence_min"]
    return MatchConfig.model_validate(from_file_data(_floats(data)))


# --- outcome ------------------------------------------------------------------------------


class MatchCode(StrEnum):
    """Matcher reason codes. ``PENDING_SILENCE`` is the one non-terminal code."""

    CLEARED_AT_ASK = "CLEARED_AT_ASK"
    MISMATCH_AREA = "MISMATCH_AREA"
    MISMATCH_WINDOW = "MISMATCH_WINDOW"
    LOST_VEHICLE = "LOST_VEHICLE"
    ESCALATE = "ESCALATE"
    UNFILED_KNOWN_OPERATOR = "UNFILED_KNOWN_OPERATOR"
    PENDING_SILENCE = "PENDING_SILENCE"


class MatchOutcome(StrictModel):
    """A reason-coded outcome. ``evidence_row_ids`` are sorted, unique and never empty."""

    code: MatchCode
    reason: FreeText
    evidence_row_ids: Annotated[tuple[RowId, ...], Field(min_length=1)]
    plan_id: StrictStr | None = None
    silence_s: NonNegFloat | None = None

    @model_validator(mode="after")
    def _shape(self) -> MatchOutcome:
        if not self.reason.strip():
            raise ValueError("an outcome needs a reason")
        ids = self.evidence_row_ids
        if list(ids) != sorted(set(ids)):
            raise ValueError("evidence_row_ids must be sorted and unique")
        return self


def _outcome(
    code: MatchCode,
    reason: str,
    row_ids: Sequence[str | None],
    *,
    plan_id: str | None = None,
    silence_s: float | None = None,
) -> MatchOutcome:
    ids = tuple(sorted({r for r in row_ids if r is not None}))
    return MatchOutcome(
        code=code,
        reason=reason,
        evidence_row_ids=ids,
        plan_id=plan_id,
        silence_s=None if silence_s is None else round6(silence_s),
    )


# --- the evidence view --------------------------------------------------------------------


class PositionFix(StrictModel):
    """One position: a reading (or a fitted position) with its noise and source row."""

    lat: float
    lon: float
    tick: NonNegInt
    sigma_m: NonNegFloat
    row_id: RowId | None = None


class EvidenceView(StrictModel):
    """What the matcher reads for one contact at one tick, each value with its row id."""

    contact_id: StrictStr
    tick: NonNegInt
    tick_s: PositiveFloat
    position: PositionFix
    hail_reply: HailReply | None = None
    hail_row_id: RowId | None = None
    modem_id: StrictStr | None = None
    modem_row_id: RowId | None = None
    operator_known: StrictBool | None = None
    operator_row_id: RowId | None = None
    lineage_plan_id: StrictStr | None = None
    lineage_plan_row_id: RowId | None = None
    lineage_replied: StrictBool = False
    lineage_replied_row_id: RowId | None = None
    last_heard_tick: NonNegInt | None = None
    last_heard_row_id: RowId | None = None
    current_field_row_id: RowId | None = None
    reacquired_from: StrictStr | None = None
    reacquired_row_id: RowId | None = None

    def row_ids(self) -> frozenset[str]:
        """Every row id the view holds."""
        ids = (
            self.position.row_id,
            self.hail_row_id,
            self.modem_row_id,
            self.operator_row_id,
            self.lineage_plan_row_id,
            self.lineage_replied_row_id,
            self.last_heard_row_id,
            self.current_field_row_id,
            self.reacquired_row_id,
        )
        return frozenset(i for i in ids if i is not None)


def _is_unknown(value: object) -> bool:
    return isinstance(value, str) and value == UNKNOWN


def _latest(rows: Sequence[tuple[int, EvidenceRow]]) -> tuple[int, EvidenceRow] | None:
    """Highest tick, ties to the later append (rows come as (append index, row))."""
    best: tuple[int, EvidenceRow] | None = None
    for idx, row in rows:
        if best is None or (row.t, idx) >= (best[1].t, best[0]):
            best = (idx, row)
    return best


def _segment_start(row: EvidenceRow) -> int:
    text = str(row.value)
    try:
        return int(text.split(";")[0].split("=")[1].split("..")[0])
    except (IndexError, ValueError):
        return -1


def evidence_view(
    table: EvidenceTable, contact_id: str, track: Track, tick: int, tick_s: float
) -> EvidenceView:
    """Snapshot one contact's evidence at ``tick`` (rows and readings at ``t <= tick``).

    ``position`` is the last reading at or before ``tick``; its ``row_id`` is the
    contact's own ``track_segment`` fact row ending at that reading (the shortest window
    when several do). Raises ``ValueError`` when no such row exists: extract first.
    Hail, plan and last-heard values read the whole lineage, so rows carried over from a
    re-acquired contact count.
    """
    if track.contact_id != contact_id:
        raise ValueError(f"track is {track.contact_id}, view is for {contact_id}")
    points = [p for p in track.points if p.tick <= tick]
    if not points:
        raise ValueError(f"{contact_id} has no reading at or before tick {tick}")
    last = points[-1]
    indexed = [(i, r) for i, r in enumerate(table.for_contact(contact_id)) if r.t <= tick]

    segments = [
        (i, r)
        for i, r in indexed
        if r.feature is FeatureName.TRACK_SEGMENT
        and r.kind is EvidenceKind.FACT
        and r.source == TRACKER_SOURCE
        and r.t == last.tick
    ]
    if not segments:
        raise ValueError(f"{contact_id}: no track_segment row ends at tick {last.tick}")
    seg = max(segments, key=lambda ir: (_segment_start(ir[1]), -ir[0]))[1]
    position = PositionFix(
        lat=round6(last.lat),
        lon=round6(last.lon),
        tick=last.tick,
        sigma_m=round6(last.sigma_m),
        row_id=seg.row_id,
    )

    def of(feature: FeatureName) -> list[tuple[int, EvidenceRow]]:
        return [(i, r) for i, r in indexed if r.feature is feature]

    fields: dict[str, Any] = {}
    hails = [(i, r) for i, r in of(FeatureName.HAIL_REPLY) if not _is_unknown(r.value)]
    latest_hail = _latest(hails)
    if latest_hail is not None:
        hail = latest_hail[1]
        fields["hail_reply"] = HailReply(str(hail.value))
        fields["hail_row_id"] = hail.row_id

        def sibling(feature: FeatureName) -> EvidenceRow | None:
            same = [
                (i, r)
                for i, r in of(feature)
                if r.t == hail.t and r.source == hail.source and r.kind is hail.kind
            ]
            found = _latest(same)
            return None if found is None else found[1]

        modem = sibling(FeatureName.MODEM_ID)
        if modem is not None:
            fields["modem_row_id"] = modem.row_id
            fields["modem_id"] = None if _is_unknown(modem.value) else str(modem.value)
        known = sibling(FeatureName.OPERATOR_KNOWN)
        if known is not None and isinstance(known.value, bool):
            fields["operator_known"] = known.value
            fields["operator_row_id"] = known.row_id

    plans = [
        (i, r)
        for i, r in of(FeatureName.PLAN_ID)
        if r.source.startswith(PLAN_SOURCE_PREFIX) and not _is_unknown(r.value)
    ]
    latest_plan = _latest(plans)
    if latest_plan is not None:
        fields["lineage_plan_id"] = str(latest_plan[1].value)
        fields["lineage_plan_row_id"] = latest_plan[1].row_id

    replies = [(i, r) for i, r in hails if r.value in (HailReply.VALID, HailReply.INVALID)]
    latest_reply = _latest(replies)
    if latest_reply is not None:
        fields["lineage_replied"] = True
        fields["lineage_replied_row_id"] = latest_reply[1].row_id

    heard = [(i, r) for i, r in hails if r.value == HailReply.VALID] + [
        (i, r) for i, r in of(FeatureName.PROPELLER_TONAL_PRESENT) if r.value is True
    ]
    latest_heard = _latest(heard)
    if latest_heard is not None:
        fields["last_heard_tick"] = latest_heard[1].t
        fields["last_heard_row_id"] = latest_heard[1].row_id

    current = _latest(of(FeatureName.CURRENT_FIELD))
    if current is not None:
        fields["current_field_row_id"] = current[1].row_id

    reacq = _latest(of(FeatureName.REACQUIRED_FROM))
    if reacq is not None and not _is_unknown(reacq[1].value):
        fields["reacquired_from"] = str(reacq[1].value)
        fields["reacquired_row_id"] = reacq[1].row_id

    return EvidenceView(
        contact_id=contact_id, tick=tick, tick_s=float(tick_s), position=position, **fields
    )


# --- registry lookup ----------------------------------------------------------------------


def distance_to_zone_m(harbor: Harbor, zone_id: str, lat: float, lon: float) -> float:
    """0 inside the zone polygon, else the distance (m) to its nearest edge, 6 dp.

    Computed in WO-010 local metres. Raises ``KeyError`` for an unknown zone.
    """
    zone = harbor.zone(zone_id)
    if point_in_polygon(lat, lon, zone.polygon):
        return 0.0
    px, py = to_local_m(lat, lon)
    verts = [to_local_m(a, b) for a, b in zone.polygon]
    best = math.inf
    for (ax, ay), (bx, by) in zip(verts, verts[1:] + verts[:1], strict=True):
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        u = 0.0 if seg2 == 0.0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        best = min(best, math.hypot(px - (ax + u * dx), py - (ay + u * dy)))
    return round6(best)


def _area_distance(harbor: Harbor, plan: PlanView, lat: float, lon: float) -> float:
    try:
        return distance_to_zone_m(harbor, plan.area, lat, lon)
    except KeyError:
        return math.inf


def in_window(plan: PlanView, tick: int) -> bool:
    """``start_tick <= tick <= end_tick``; a plan without ticks is never in window."""
    if plan.start_tick is None or plan.end_tick is None:
        return False
    return plan.start_tick <= tick <= plan.end_tick


def _active(plans: Sequence[PlanView]) -> list[PlanView]:
    return sorted(
        (p for p in plans if p.status is PlanStatus.ACTIVE),
        key=lambda p: p.plan_id,
    )


def _best_plan(
    cands: Sequence[PlanView], harbor: Harbor, lat: float, lon: float, tick: int, tol: float
) -> tuple[PlanView, float] | None:
    """In window first, then in area, then nearest zone, then lowest plan_id."""
    scored = [(p, _area_distance(harbor, p, lat, lon)) for p in cands]
    if not scored:
        return None
    return min(
        scored, key=lambda pd: (not in_window(pd[0], tick), pd[1] > tol, pd[1], pd[0].plan_id)
    )


class RegistryLookup(StrictModel):
    """One ``registry_lookup`` result. ``distance_m`` is to the chosen plan's zone."""

    match: RegistryMatch
    plan_id: StrictStr | None = None
    distance_m: NonNegFloat | None = None
    modem_id: StrictStr | None = None
    tick: NonNegInt = 0


def _finite(d: float) -> float | None:
    return None if math.isinf(d) else round6(d)


def registry_lookup(
    plans: Sequence[PlanView],
    harbor: Harbor,
    lat: float,
    lon: float,
    tick: int,
    cfg: MatchConfig,
    modem_id: str | None = None,
) -> RegistryLookup:
    """Bucket a contact against the active plans (``config/likelihoods.yaml`` names).

    Position only (``modem_id=None``, the watch rung): a plan whose zone lies within
    ``area_tolerance_m`` is ``exact`` when its window holds ``tick``, else
    ``window_mismatch``; no such plan is ``none``. Never ``area_mismatch``: that needs an
    identity. With a modem id: that modem's plans only; in window and in area is
    ``exact``, in window but out of area ``area_mismatch``, out of window
    ``window_mismatch``, no plan for the modem ``none``.
    """
    tol = cfg.area_tolerance_m
    active = _active(plans)
    if modem_id is not None:
        best = _best_plan(
            [p for p in active if p.modem_id == modem_id], harbor, lat, lon, tick, tol
        )
        if best is None:
            return RegistryLookup(match=RegistryMatch.NONE, modem_id=modem_id, tick=tick)
        plan, dist = best
        if not in_window(plan, tick):
            bucket = RegistryMatch.WINDOW_MISMATCH
        elif dist > tol:
            bucket = RegistryMatch.AREA_MISMATCH
        else:
            bucket = RegistryMatch.EXACT
        return RegistryLookup(
            match=bucket,
            plan_id=plan.plan_id,
            distance_m=_finite(dist),
            modem_id=modem_id,
            tick=tick,
        )
    near = [p for p in active if _area_distance(harbor, p, lat, lon) <= tol]
    best = _best_plan(near, harbor, lat, lon, tick, tol)
    if best is None:
        return RegistryLookup(match=RegistryMatch.NONE, tick=tick)
    plan, dist = best
    bucket = RegistryMatch.EXACT if in_window(plan, tick) else RegistryMatch.WINDOW_MISMATCH
    return RegistryLookup(match=bucket, plan_id=plan.plan_id, distance_m=_finite(dist), tick=tick)


def _confidence(row: EvidenceRow) -> float:
    return row.confidence if isinstance(row.confidence, float) else 0.0


def registry_lookup_rows(
    lookup: RegistryLookup,
    *,
    contact_id: str,
    track_segment: EvidenceRow,
    hail_rows: Sequence[EvidenceRow] = (),
) -> list[EvidenceRow]:
    """The rows a lookup writes at ``lookup.tick``: ``registry_match``, then ``plan_id``.

    ``registry_match`` is derived from the ``track_segment`` row (the position) and any
    hail rows (the identity), at their minimum confidence. ``plan_id`` is a fact written
    only when a plan was found: ``source="registry:<plan_id>"`` when a modem named it,
    ``"registry.position:<plan_id>"`` for a position-only lookup (sources are lower
    case by the WO-010 pattern; the value keeps the plan id as filed).
    """
    sources = [track_segment, *hail_rows]
    rows = [
        EvidenceRow(
            contact_id=contact_id,
            t=lookup.tick,
            feature=FeatureName.REGISTRY_MATCH,
            value=lookup.match.value,
            unit=Unit.CATEGORY,
            source=REGISTRY_SOURCE,
            confidence=min(_confidence(r) for r in sources),
            kind=EvidenceKind.DERIVED,
            probe_id=ProbeId.REGISTRY_LOOKUP,
            derived_from=tuple(dict.fromkeys(r.row_id for r in sources)),
        )
    ]
    if lookup.plan_id is not None:
        prefix = PLAN_SOURCE_PREFIX if lookup.modem_id is not None else PLAN_POSITION_SOURCE_PREFIX
        rows.append(
            EvidenceRow(
                contact_id=contact_id,
                t=lookup.tick,
                feature=FeatureName.PLAN_ID,
                value=lookup.plan_id,
                unit=Unit.ID,
                source=f"{prefix}{lookup.plan_id.lower()}",
                confidence=PLAN_ROW_CONFIDENCE,
                kind=EvidenceKind.FACT,
                probe_id=ProbeId.REGISTRY_LOOKUP,
            )
        )
    return rows


# --- match --------------------------------------------------------------------------------


def _m(x: float) -> str:
    return "inf" if math.isinf(x) else f"{x:.1f}"


def match(
    view: EvidenceView,
    plans: Sequence[PlanView],
    tick: int,
    *,
    harbor: Harbor,
    cfg: MatchConfig,
) -> MatchOutcome:
    """The WO-022 decision table; the first rule that holds wins.

    1. ``valid`` reply and the modem has a plan P: outside P's window is
       ``MISMATCH_WINDOW``; else zone distance ``> area_tolerance_m`` is
       ``MISMATCH_AREA``; else ``CLEARED_AT_ASK``.
    2. ``valid``, no plan for the modem, operator known: ``UNFILED_KNOWN_OPERATOR``.
    3. ``valid`` with an unknown modem, or ``invalid``: ``ESCALATE``.
    4. ``none``/``timeout`` and a filed lineage: silence ``> lost_vehicle_silence_min``
       is ``LOST_VEHICLE``, otherwise ``PENDING_SILENCE``.
    5. Anything else: ``ESCALATE``.
    """
    pos = view.position
    hail_id = view.hail_row_id
    reply = view.hail_reply
    tol = cfg.area_tolerance_m

    if reply is HailReply.VALID and view.modem_id is not None:
        modem = view.modem_id
        cands = [p for p in _active(plans) if p.modem_id == modem]
        best = _best_plan(cands, harbor, pos.lat, pos.lon, tick, tol)
        cited = (hail_id, view.modem_row_id, pos.row_id)
        if best is not None:
            plan, dist = best
            window = f"{plan.start_tick}..{plan.end_tick}"
            if not in_window(plan, tick):
                return _outcome(
                    MatchCode.MISMATCH_WINDOW,
                    f"hail valid from {modem}; plan {plan.plan_id} window {window} does not "
                    f"hold tick {tick}",
                    cited,
                    plan_id=plan.plan_id,
                )
            if dist > tol:
                return _outcome(
                    MatchCode.MISMATCH_AREA,
                    f"hail valid from {modem}; plan {plan.plan_id} in window {window} but "
                    f"{_m(dist)} m from {plan.area} > {_m(tol)} m",
                    cited,
                    plan_id=plan.plan_id,
                )
            return _outcome(
                MatchCode.CLEARED_AT_ASK,
                f"hail valid from {modem}; plan {plan.plan_id} window {window} holds tick "
                f"{tick}; {_m(dist)} m from {plan.area} <= {_m(tol)} m",
                cited,
                plan_id=plan.plan_id,
            )
        if view.operator_known:
            return _outcome(
                MatchCode.UNFILED_KNOWN_OPERATOR,
                f"hail valid from {modem}; no active plan names it; modem is on the "
                "known-operator table",
                (hail_id, view.modem_row_id, view.operator_row_id),
            )
        return _outcome(
            MatchCode.ESCALATE,
            f"hail valid from {modem}; no active plan names it and the operator is unknown",
            (hail_id, view.modem_row_id, view.operator_row_id),
        )

    if reply is HailReply.VALID or reply is HailReply.INVALID:
        return _outcome(
            MatchCode.ESCALATE,
            f"hail reply {reply.value}: no usable modem identity",
            (hail_id, view.modem_row_id),
        )

    if reply in (HailReply.NONE, HailReply.TIMEOUT) and view.lineage_plan_id is not None:
        plan_id = view.lineage_plan_id
        if view.last_heard_tick is None or view.last_heard_row_id is None:
            return _outcome(
                MatchCode.ESCALATE,
                f"hail {reply.value}; lineage filed on {plan_id} but nothing shows when it "
                "was last heard",
                (hail_id, view.lineage_plan_row_id, pos.row_id),
                plan_id=plan_id,
            )
        silence = round6(max(tick - view.last_heard_tick, 0) * view.tick_s)
        limit = cfg.lost_vehicle_silence_s
        cited = (hail_id, view.lineage_plan_row_id, view.last_heard_row_id)
        if silence > limit:
            return _outcome(
                MatchCode.LOST_VEHICLE,
                f"hail {reply.value}; filed on {plan_id}; silence {silence:.0f} s > "
                f"{limit:.0f} s since row {view.last_heard_row_id}",
                cited,
                plan_id=plan_id,
                silence_s=silence,
            )
        return _outcome(
            MatchCode.PENDING_SILENCE,
            f"hail {reply.value}; filed on {plan_id}; silence {silence:.0f} s <= "
            f"{limit:.0f} s since row {view.last_heard_row_id}",
            cited,
            plan_id=plan_id,
            silence_s=silence,
        )

    if reply is None:
        return _outcome(
            MatchCode.ESCALATE, "no hail reply on record and no filed lineage", (pos.row_id,)
        )
    return _outcome(
        MatchCode.ESCALATE,
        f"hail {reply.value} and no filed lineage",
        (hail_id, pos.row_id),
    )


# --- drift search box ---------------------------------------------------------------------


class VectorField(Protocol):
    """Anything with ``CurrentField.vector_at(lat, lon, tide_phase) -> (u, v)`` in m/s."""

    def vector_at(self, lat: float, lon: float, tide_phase: Any) -> tuple[float, float]: ...


def advect(
    lat: float, lon: float, elapsed_s: float, field: VectorField, tide_phase: Any, step_s: float
) -> tuple[float, float]:
    """Euler steps of ``step_s`` (the last one shorter) through ``field``; local metres."""
    x, y = to_local_m(lat, lon)
    if elapsed_s <= 0:
        return lat, lon
    n_full = int(math.floor(round(elapsed_s / step_s, 9)))
    steps = [step_s] * n_full
    rest = elapsed_s - n_full * step_s
    if rest > 1e-9:
        steps.append(rest)
    for dt in steps:
        u, v = field.vector_at(*from_local_m(x, y), tide_phase)
        x += float(u) * dt
        y += float(v) * dt
    return from_local_m(x, y)


class SearchBox(StrictModel):
    """A circle (``center``, ``radius_m``) with its bounding box, for a LOST_VEHICLE notice."""

    center_lat: float
    center_lon: float
    radius_m: NonNegFloat
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float
    anchor: PositionFix
    to_tick: NonNegInt
    assumption_row_id: RowId

    def contains(self, lat: float, lon: float) -> bool:
        """True when ``(lat, lon)`` lies within ``radius_m`` of the centre."""
        return distance_m(self.center_lat, self.center_lon, lat, lon) <= self.radius_m + 1e-9


def drift_search_box(
    anchor: PositionFix,
    to_tick: int,
    tick_s: float,
    current_field: VectorField,
    tide_phase: Any,
    cfg: MatchConfig,
    *,
    assumption_row_id: str,
) -> SearchBox:
    """Advect ``anchor`` to ``to_tick``; radius ``base + sigma + growth * elapsed``.

    ``assumption_row_id`` is the ``current_field`` assumption row the drift rests on.
    """
    box = cfg.search_box
    elapsed = max(to_tick - anchor.tick, 0) * float(tick_s)
    clat, clon = advect(anchor.lat, anchor.lon, elapsed, current_field, tide_phase, box.step_s)
    radius = box.base_radius_m + anchor.sigma_m + box.growth_mps * elapsed
    cx, cy = to_local_m(clat, clon)
    lo_lat, lo_lon = from_local_m(cx - radius, cy - radius)
    hi_lat, hi_lon = from_local_m(cx + radius, cy + radius)
    return SearchBox(
        center_lat=round6(clat),
        center_lon=round6(clon),
        radius_m=round6(radius),
        min_lat=round6(lo_lat),
        max_lat=round6(hi_lat),
        min_lon=round6(lo_lon),
        max_lon=round6(hi_lon),
        anchor=anchor,
        to_tick=to_tick,
        assumption_row_id=assumption_row_id,
    )


# --- re-acquisition (SPEC-INVESTIGATION 9.9) ----------------------------------------------


class LostTrack(StrictModel):
    """A contact whose track dropped: last position, fitted velocity (east, north)."""

    contact_id: StrictStr
    last_fix: PositionFix
    velocity_mps: tuple[float, float]
    last_tick: NonNegInt


class ReacquireMatch(StrictModel):
    old_id: StrictStr
    new_id: StrictStr
    distance_m: NonNegFloat
    gate_m: PositiveFloat
    match_confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    reason: FreeText


def match_reacquisition(
    first_fix: PositionFix,
    speed_mps: float | None,
    lost: Sequence[LostTrack],
    tick: int,
    tick_s: float,
    current_field: VectorField,
    tide_phase: Any,
    cfg: MatchConfig,
    *,
    new_id: str,
) -> ReacquireMatch | None:
    """Match a new track's first fix to the lost tracks, or None.

    Each lost track (last reading before ``first_fix.tick`` and at or before ``tick``,
    gap ``<= max_gap_s``) is predicted to ``first_fix.tick`` by drift through the current
    field and by dead reckoning on its velocity; the closer prediction counts. It is a
    candidate when that distance is within ``gate_base_m + gate_growth_mps * gap_s`` and,
    when ``speed_mps`` is known, ``|speed_mps - |velocity|| <= speed_tol_mps``.
    Candidates order by ``distance / gate`` then lowest ``contact_id``;
    ``match_confidence = 1 - distance / gate``.
    """
    rc = cfg.reacquire
    best: tuple[float, str, ReacquireMatch] | None = None
    for cand in sorted(lost, key=lambda c: c.contact_id):
        if cand.contact_id == new_id:
            continue
        if not cand.last_tick < first_fix.tick or cand.last_tick > tick:
            continue
        gap_s = (first_fix.tick - cand.last_tick) * float(tick_s)
        if gap_s > rc.max_gap_s:
            continue
        old_speed = math.hypot(*cand.velocity_mps)
        if speed_mps is not None and abs(speed_mps - old_speed) > rc.speed_tol_mps:
            continue
        a = cand.last_fix
        dlat, dlon = advect(a.lat, a.lon, gap_s, current_field, tide_phase, cfg.search_box.step_s)
        ax, ay = to_local_m(a.lat, a.lon)
        rlat, rlon = from_local_m(
            ax + cand.velocity_mps[0] * gap_s, ay + cand.velocity_mps[1] * gap_s
        )
        d_drift = distance_m(dlat, dlon, first_fix.lat, first_fix.lon)
        d_reckon = distance_m(rlat, rlon, first_fix.lat, first_fix.lon)
        kind, dist = ("drift", d_drift) if d_drift <= d_reckon else ("dead-reckoning", d_reckon)
        gate = rc.gate_base_m + rc.gate_growth_mps * gap_s
        if dist > gate:
            continue
        ratio = dist / gate
        speed_text = (
            "new speed unmeasured"
            if speed_mps is None
            else f"speed {speed_mps:.2f} vs {old_speed:.2f} m/s"
        )
        found = ReacquireMatch(
            old_id=cand.contact_id,
            new_id=new_id,
            distance_m=round6(dist),
            gate_m=round6(gate),
            match_confidence=round6(1.0 - ratio),
            reason=(
                f"{new_id} continues {cand.contact_id}: {dist:.1f} m from the {kind} "
                f"prediction after a {gap_s:.0f} s gap (gate {gate:.1f} m; {speed_text})"
            ),
        )
        key = (round6(ratio), cand.contact_id)
        if best is None or key < (best[0], best[1]):
            best = (key[0], key[1], found)
    return None if best is None else best[2]


__all__ = [
    "MATCHER_CONFIG_PATH",
    "AskConfig",
    "EvidenceView",
    "LostTrack",
    "MatchCode",
    "MatchConfig",
    "MatchOutcome",
    "PlanWatchConfig",
    "PositionFix",
    "ReacquireConfig",
    "ReacquireMatch",
    "RegistryLookup",
    "SearchBox",
    "SearchBoxConfig",
    "VectorField",
    "advect",
    "distance_to_zone_m",
    "drift_search_box",
    "evidence_view",
    "in_window",
    "load_match_config",
    "match",
    "match_reacquisition",
    "registry_lookup",
    "registry_lookup_rows",
]
