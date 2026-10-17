"""Allowlisted, costed, idempotent probes (docs/SPEC-INVESTIGATION.md section 5).

A probe is the only way new evidence enters the table during an investigation. This
module holds the allowlist (``ALLOWLIST``: every section 5 probe with its exact cost,
latency, rung and interruption count), the one idempotency key (re-exported from
``soundings.probe_keys`` so the hail and every other probe share it), a per-process
``ProbeCache`` and ``run_probe``, the single executor entry point.

Rules this module enforces (CLAUDE.md 2.1, 2.2, 2.4):

* no probe exists at or above ``slow``; ``run_probe`` raises ``ProbeNotAllowed`` for a
  probe above the context's rung (the rung order is read from ``config/ladder.yaml``);
* a probe id is a typed ``ProbeId`` from ``ALLOWLIST``; free text never runs anything;
* a repeated key (same contact, probe, args and 30 s tick bucket) returns the cached
  result with ``cached=True``, appends nothing and never calls the executor again;
* every row a probe returns is a typed ``EvidenceRow`` with ``probe_id`` set and ``t``
  equal to the tick the result lands (``sent tick + ceil(latency_s / tick_s)``). Rows a
  probe builds itself carry ``source="probe:<id>"``; ``registry_lookup`` keeps the
  sources of ``soundings.matcher.registry_lookup_rows`` (``probe:registry_lookup``,
  ``registry:<plan_id>``, ``registry.position:<plan_id>``) because runner lineage reads
  them. A feature a probe cannot compute becomes an ``unknown`` row, never a missing one.

What each probe does:

* the six watch probes re-run the WO-012 extractors (``contact_evidence``) on the
  observed track truncated at the probe's tick and keep the rows for the features they
  return plus the provenance rows those derive from (``track_segment``, assumptions,
  context). ``acoustic_feature_read`` keeps one row per acoustic feature: the latest
  reading that reports it (the posterior reads one row per feature anyway);
* ``registry_lookup`` calls ``soundings.matcher.registry_lookup`` over the registry's
  ``overlapping_ticks`` (or a plan list) and appends ``registry_lookup_rows``;
* ``acoustic_hail`` calls ``HailSimulator.send`` and appends ``hail_evidence_rows``;
* ``camera_cue`` returns ``visibility_for_camera``: 0 under ``fog_no_camera``, else the
  hour-of-day visibility. WO-012 already labels that feature an assumption for every
  contact and the table never mixes fact and assumption on one feature, so the cue's row
  is ``derived`` from the contact's visibility row when one exists, else a fact;
* ``second_sensor_dwell`` returns ``depth`` and ``speed_over_ground`` from the two best
  sensor nodes over the latency window (confidence ``1 - (1 - c1)(1 - c2)``);
* ``ask_operator_by_phone`` returns ``operator_known`` from the known-operator table by
  ``modem_id`` (``unknown`` without one);
* ``request_watch_officer_look`` returns ``depth`` and ``transit_pattern`` from the
  simulated eyes-on, with confidence equal to the camera visibility. Like the hail, it is
  a simulated physical observation: it reads the simulator's path for the contact, and
  its rows carry no hypothesis label.

Everything is deterministic: no randomness here (the hail draws its own seeded stream),
no wall-clock reads, floats rounded to 6 dp at the row boundary. No model output enters.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol

from pydantic import Field, StrictBool, StrictInt, StrictStr

from soundings.canonical import round6
from soundings.challenge.hail import (
    HailSimulator,
    KnownOperatorTable,
    hail_evidence_rows,
    load_known_operators,
)
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import (
    ACOUSTIC_FEATURES,
    FEATURE_CATALOG,
    KINEMATIC_FEATURES,
    PATTERN_WINDOW_S,
    CurrentAt,
    PolBaseline,
    TrackArrays,
    classify_transit_pattern,
    contact_evidence,
    current_at_from_field,
    depth,
    hour_at,
    speed_over_ground,
    track_arrays,
    track_segments,
    visibility_default,
)
from soundings.matcher import (
    MatchConfig,
    SearchBox,
    load_match_config,
    registry_lookup,
    registry_lookup_rows,
)
from soundings.ports import CloseLook, ObservedTracks, WorldRun
from soundings.probe_keys import TICK_BUCKET_S, idempotency_key, tick_bucket
from soundings.registry.models import PlanView
from soundings.registry.store import RegistryStore
from soundings.schemas._base import NonNegFloat, NonNegInt, Probability, StrictModel
from soundings.schemas.config import LADDER_PATH, load_ladder_config
from soundings.schemas.enums import (
    EvidenceKind,
    FeatureName,
    HailReply,
    ProbeId,
    Rung,
    ToggleName,
    Unit,
)
from soundings.schemas.evidence import UNKNOWN, EvidenceRow, EvidenceValue
from soundings.schemas.geo import distance_m, to_local_m
from soundings.schemas.harbor import Harbor
from soundings.schemas.scenario import Scenario
from soundings.schemas.track import Track

PROBE_SOURCE_PREFIX: Final = "probe:"
PLAN_HORIZON_TICK: Final = 10**9
"""``overlapping_ticks(0, PLAN_HORIZON_TICK)`` reads every tick-windowed plan."""

UNKNOWN_CONFIDENCE: Final = 0.0
PHONE_CONFIDENCE: Final = 1.0
"""An operator confirmation by phone is a stated fact (simulated)."""
CAMERA_CONFIDENCE: Final = 1.0
EYES_ON_SIGMA_M: Final = 1.0
"""Position sigma used to classify the eyes-on transit pattern (a person sees the path)."""
DWELL_NODES: Final = 2


# --- the allowlist ------------------------------------------------------------------------


class ProbeSpec(StrictModel):
    """One allowlisted probe. The first six fields are SPEC-INVESTIGATION section 5 exactly.

    ``nominal_confidence`` is the confidence the deterministic planner expects the probe's
    rows to carry (``c_probe`` in the information-gain formula). Section 5 gives no such
    number, so these are stress-test settings for this demonstration (see
    ``NOMINAL_CONFIDENCE``), overridable per call through ``PlannerState``.
    """

    probe_id: ProbeId
    rung_required: Rung
    cost_usd: NonNegFloat
    latency_s: NonNegFloat
    interruption: NonNegInt
    returns: tuple[FeatureName, ...] = Field(min_length=1)
    nominal_confidence: Probability = 1.0

    @property
    def paid(self) -> bool:
        """True when the probe costs money (counts toward ``paid_probes_per_contact``)."""
        return self.cost_usd > 0.0


TRACK_DERIVED_CONFIDENCE: Final = 0.5
"""Track-derived rows carry their window's minimum reading confidence (0.1-0.7 on the
catalog runs); 0.5 is the stress-test planning value for a re-read of the track."""

NOMINAL_CONFIDENCE: Final[Mapping[ProbeId, float]] = MappingProxyType(
    {
        ProbeId.REGISTRY_LOOKUP: TRACK_DERIVED_CONFIDENCE,  # min(track segment, hail rows)
        ProbeId.KINEMATICS_PROFILE: TRACK_DERIVED_CONFIDENCE,
        ProbeId.DRIFT_CHECK: TRACK_DERIVED_CONFIDENCE,
        ProbeId.PATTERN_OF_LIFE_CHECK: TRACK_DERIVED_CONFIDENCE,
        ProbeId.AIS_SURFACE_CORRELATE: 0.0,  # no surface feed exists (DECISIONS 22:25)
        ProbeId.ACOUSTIC_FEATURE_READ: TRACK_DERIVED_CONFIDENCE,  # one reading's confidence
        ProbeId.ACOUSTIC_HAIL: 0.9,  # hail rows: 1.0 with a reply, 0.9 without
        ProbeId.CAMERA_CUE: CAMERA_CONFIDENCE,
        ProbeId.SECOND_SENSOR_DWELL: 0.75,  # 1 - (1 - 0.5)(1 - 0.5)
        ProbeId.ASK_OPERATOR_BY_PHONE: PHONE_CONFIDENCE,
        ProbeId.REQUEST_WATCH_OFFICER_LOOK: TRACK_DERIVED_CONFIDENCE,  # visibility 0.2-0.8
    }
)


def _spec(
    probe_id: ProbeId,
    rung: Rung,
    cost_usd: float,
    latency_s: float,
    interruption: int,
    returns: Sequence[FeatureName],
) -> ProbeSpec:
    return ProbeSpec(
        probe_id=probe_id,
        rung_required=rung,
        cost_usd=float(cost_usd),
        latency_s=float(latency_s),
        interruption=interruption,
        returns=tuple(returns),
        nominal_confidence=NOMINAL_CONFIDENCE[probe_id],
    )


_F = FeatureName
SECTION_5_ALLOWLIST: Final[Mapping[ProbeId, ProbeSpec]] = MappingProxyType(
    {
        spec.probe_id: spec
        for spec in (
            _spec(
                ProbeId.REGISTRY_LOOKUP,
                Rung.WATCH,
                0,
                0,
                0,
                (_F.REGISTRY_MATCH, _F.PLAN_ID),
            ),
            _spec(ProbeId.KINEMATICS_PROFILE, Rung.WATCH, 0, 0, 0, KINEMATIC_FEATURES),
            _spec(ProbeId.DRIFT_CHECK, Rung.WATCH, 0, 0, 0, (_F.DRIFT_CONSISTENCY,)),
            _spec(
                ProbeId.PATTERN_OF_LIFE_CHECK,
                Rung.WATCH,
                0,
                0,
                0,
                (_F.PATTERN_OF_LIFE_ANOMALY,),
            ),
            _spec(
                ProbeId.AIS_SURFACE_CORRELATE,
                Rung.WATCH,
                0,
                1,
                0,
                (_F.AIS_SURFACE_CORRELATION,),
            ),
            _spec(ProbeId.ACOUSTIC_FEATURE_READ, Rung.WATCH, 0, 2, 0, ACOUSTIC_FEATURES),
            _spec(
                ProbeId.ACOUSTIC_HAIL,
                Rung.ASK,
                0.01,
                20,
                0,
                (_F.HAIL_SENT_AT, _F.HAIL_REPLY, _F.MODEM_ID, _F.OPERATOR_KNOWN),
            ),
            _spec(ProbeId.CAMERA_CUE, Rung.ASK, 0.05, 30, 0, (_F.VISIBILITY_FOR_CAMERA,)),
            _spec(
                ProbeId.SECOND_SENSOR_DWELL,
                Rung.ASK,
                0.10,
                60,
                0,
                (_F.DEPTH, _F.SPEED_OVER_GROUND),
            ),
            _spec(ProbeId.ASK_OPERATOR_BY_PHONE, Rung.ASK, 0, 120, 0, (_F.OPERATOR_KNOWN,)),
            _spec(
                ProbeId.REQUEST_WATCH_OFFICER_LOOK,
                Rung.ASK,
                0,
                60,
                1,
                (_F.DEPTH, _F.TRANSIT_PATTERN),
            ),
        )
    }
)
"""Every section 5 probe in section 5 order (which is also ``ProbeId`` order). WO-088 adds
the two responder probes after them in ``ALLOWLIST`` (see the WO-088 block below)."""


class ProbeNotAllowed(ValueError):
    """The probe is not allowlisted, or needs a higher rung than the context is on."""


@lru_cache(maxsize=4)
def _rung_order(path: str) -> tuple[Rung, ...]:
    return tuple(load_ladder_config(path).rungs)


def rung_index(rung: Rung | str, *, ladder_path: str | Path = LADDER_PATH) -> int:
    """Position of ``rung`` in ``config/ladder.yaml`` ``rungs`` (watch = 0)."""
    return _rung_order(str(ladder_path)).index(Rung(rung))


def latency_ticks(latency_s: float, tick_s: float) -> int:
    """``ceil(latency_s / tick_s)``; 0 for a zero-latency probe (20 s is 10 ticks at 2 s)."""
    if latency_s < 0 or tick_s <= 0:
        raise ValueError("latency_s must be >= 0 and tick_s > 0")
    return int(math.ceil(round(latency_s / tick_s, 9)))


def as_probe_id(probe_id: ProbeId | str) -> ProbeId:
    """The typed id for an allowlisted probe; anything else raises ``ProbeNotAllowed``."""
    try:
        pid = ProbeId(probe_id)
    except ValueError as exc:
        raise ProbeNotAllowed(f"{probe_id!r} is not an allowlisted probe") from exc
    if pid not in ALLOWLIST:
        raise ProbeNotAllowed(f"{probe_id!r} is not an allowlisted probe")
    return pid


# --- context, result, cache ---------------------------------------------------------------


class _VectorField(Protocol):
    def vector_at(self, lat: float, lon: float, tide_phase: Any) -> tuple[float, float]: ...

    def phase_for_hour(self, hour: int) -> Any: ...


@dataclass(frozen=True, slots=True)
class ProbeContext:
    """Everything a probe may read, at one tick, for one contact.

    ``current_field`` is a WO-011 ``CurrentField`` (anything with ``vector_at`` and
    ``phase_for_hour``) or a local-metre ``CurrentAt`` callable. ``registry`` is a
    ``RegistryStore`` (read through ``overlapping_ticks``) or a list of ``PlanView``.
    ``hail`` lets a caller share one ``HailSimulator`` across calls (one is built from
    the scenario and run otherwise); ``match_cfg`` and ``known`` default to the
    config files.
    """

    run: WorldRun
    scenario: Scenario
    harbor: Harbor
    current_field: _VectorField | CurrentAt
    pol: PolBaseline | None
    registry: RegistryStore | Sequence[PlanView]
    tick: int
    current_rung: Rung
    contact_id: str
    hail: HailSimulator | None = None
    match_cfg: MatchConfig | None = None
    known: KnownOperatorTable | None = None
    tide_phase_for_hour: Callable[[int], Any] | None = None
    # --- WO-088 begin --- (the harbor's responder, when one exists; LOST_VEHICLE box)
    responder: ResponderPort | None = None
    search_box: SearchBox | None = None
    # --- WO-088 end ---

    @property
    def tick_s(self) -> float:
        return float(self.run.tick_s)

    def bucket(self) -> int:
        """The 30 s idempotency bucket of ``tick``."""
        return tick_bucket(self.tick, self.tick_s)


class ProbeResult(StrictModel):
    """What one probe produced. ``rows`` are every row it returned, in append order.

    ``cached`` is True when ``run_probe`` served the result from the cache (nothing was
    appended and the executor did not run).
    """

    key: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_id: ProbeId
    contact_id: str
    sent_tick: NonNegInt
    landed_tick: NonNegInt
    args: dict[str, StrictBool | StrictInt | StrictStr]
    rows: tuple[EvidenceRow, ...]
    cached: StrictBool = False


class ProbeCache:
    """Idempotency-key -> ``ProbeResult``, for one process (WO-042 records the keys).

    ``executions`` counts executor calls per probe; a cache hit never increments it.
    """

    def __init__(self) -> None:
        self._results: dict[str, ProbeResult] = {}
        self.executions: dict[ProbeId, int] = {}

    def __contains__(self, key: object) -> bool:
        return key in self._results

    def __len__(self) -> int:
        return len(self._results)

    def get(self, key: str) -> ProbeResult | None:
        return self._results.get(key)

    def keys(self) -> tuple[str, ...]:
        """Cached keys in insertion order."""
        return tuple(self._results)

    def put(self, result: ProbeResult) -> None:
        if result.key in self._results:
            raise ValueError(f"key {result.key} is already cached")
        self._results[result.key] = result.model_copy(update={"cached": False})

    def count(self, probe_id: ProbeId | str | None = None) -> int:
        """Executor calls for one probe, or in total."""
        if probe_id is None:
            return sum(self.executions.values())
        return self.executions.get(ProbeId(probe_id), 0)

    def _executed(self, probe_id: ProbeId) -> None:
        self.executions[probe_id] = self.executions.get(probe_id, 0) + 1


# --- row helpers --------------------------------------------------------------------------


def probe_source(probe_id: ProbeId) -> str:
    return f"{PROBE_SOURCE_PREFIX}{probe_id.value}"


def _is_unknown(value: object) -> bool:
    return isinstance(value, str) and value == UNKNOWN


def _value(value: EvidenceValue) -> EvidenceValue:
    if isinstance(value, float):
        return round6(value)
    return value


def _fact(
    ctx: ProbeContext,
    probe_id: ProbeId,
    feature: FeatureName,
    value: EvidenceValue,
    confidence: float,
    t: int,
) -> EvidenceRow:
    known = not _is_unknown(value)
    return EvidenceRow(
        contact_id=ctx.contact_id,
        t=t,
        feature=feature,
        value=_value(value),
        unit=FEATURE_CATALOG[feature].unit,
        source=probe_source(probe_id),
        confidence=round6(confidence) if known else UNKNOWN_CONFIDENCE,
        kind=EvidenceKind.FACT,
        probe_id=probe_id,
    )


def _unknown_rows(
    ctx: ProbeContext, probe_id: ProbeId, features: Sequence[FeatureName], t: int
) -> list[EvidenceRow]:
    return [_fact(ctx, probe_id, f, UNKNOWN, UNKNOWN_CONFIDENCE, t) for f in features]


def restamp(rows: Sequence[EvidenceRow], probe_id: ProbeId, t: int) -> list[EvidenceRow]:
    """Copies of ``rows`` as this probe's output: ``source="probe:<id>"``, ``probe_id``
    and ``t`` set. ``derived_from`` ids are rewritten to the copies (sources come first);
    an id not among ``rows`` is kept as is (it must already be in the table)."""
    ids: dict[str, str] = {}
    out: list[EvidenceRow] = []
    for row in rows:
        data = row.model_dump(exclude={"row_id"})
        data.update(
            t=t,
            source=probe_source(probe_id),
            probe_id=probe_id,
            derived_from=tuple(ids.get(i, i) for i in row.derived_from),
        )
        copy = EvidenceRow.model_validate(data)
        ids[row.row_id] = copy.row_id
        out.append(copy)
    return out


def with_provenance(
    rows: Sequence[EvidenceRow], features: Sequence[FeatureName]
) -> list[EvidenceRow]:
    """The rows for ``features`` plus every row they derive from, in the original order."""
    by_id = {r.row_id: r for r in rows}
    keep: set[str] = set()
    stack = [r.row_id for r in rows if r.feature in set(features)]
    while stack:
        rid = stack.pop()
        if rid in keep:
            continue
        keep.add(rid)
        stack.extend(i for i in by_id[rid].derived_from if i in by_id)
    out: list[EvidenceRow] = []
    seen: set[str] = set()
    for row in rows:
        if row.row_id in keep and row.row_id not in seen:
            seen.add(row.row_id)
            out.append(row)
    return out


def latest_per_feature(rows: Sequence[EvidenceRow]) -> list[EvidenceRow]:
    """One row per feature: the highest tick (later rows win ties), in first-seen order."""
    best: dict[FeatureName, EvidenceRow] = {}
    for row in rows:
        prior = best.get(row.feature)
        if prior is None or row.t >= prior.t:
            best[row.feature] = row
    return list(best.values())


def track_upto(run: ObservedTracks, contact_id: str, tick: int) -> Track | None:
    """The observed track of ``contact_id`` truncated at ``tick`` (None if no point)."""
    track = run.tracks.get(contact_id)
    if track is None:
        return None
    points = tuple(p for p in track.points if p.tick <= tick)
    if not points:
        return None
    if len(points) == len(track.points):
        return track
    gaps = tuple(g for g in track.gaps if g.end_tick < points[-1].tick)
    return track.model_copy(update={"points": points, "gaps": gaps})


def _current_at(ctx: ProbeContext) -> CurrentAt:
    field = ctx.current_field
    vector_at = getattr(field, "vector_at", None)
    if vector_at is not None:
        phase = ctx.tide_phase_for_hour
        if phase is None:
            phase = getattr(field, "phase_for_hour")  # noqa: B009
        return current_at_from_field(vector_at, phase, hour_of_day=ctx.scenario.hour_of_day)
    if callable(field):
        return field
    raise TypeError("current_field must be a CurrentAt callable or have vector_at")


def _plans(ctx: ProbeContext) -> list[PlanView]:
    if isinstance(ctx.registry, RegistryStore):
        return ctx.registry.overlapping_ticks(0, PLAN_HORIZON_TICK)
    return list(ctx.registry)


def _latest_known(table: EvidenceTable, contact_id: str, feature: FeatureName) -> Any:
    row = table.latest(contact_id, feature)
    if row is None or _is_unknown(row.value):
        return None
    return row.value


def fog_active(scenario: Scenario, tick: int) -> bool:
    """True when a ``fog_no_camera`` toggle holds at ``tick`` (no ``at_tick``: from 0)."""
    for toggle in scenario.toggles:
        if toggle.name is ToggleName.FOG_NO_CAMERA:
            if toggle.at_tick is None or tick >= toggle.at_tick:
                return True
    return False


def camera_visibility(ctx: ProbeContext, tick: int) -> float:
    """Camera visibility at ``tick``: 0 under fog, else the hour-of-day default."""
    if fog_active(ctx.scenario, tick):
        return 0.0
    return visibility_default(hour_at(ctx.scenario.hour_of_day, tick * ctx.tick_s))


# --- executors ----------------------------------------------------------------------------

Executor = Callable[[ProbeContext, EvidenceTable, Mapping[str, Any], int], list[EvidenceRow]]


def _watch_executor(probe_id: ProbeId) -> Executor:
    spec = SECTION_5_ALLOWLIST[probe_id]

    def run(
        ctx: ProbeContext, table: EvidenceTable, args: Mapping[str, Any], landed: int
    ) -> list[EvidenceRow]:
        track = track_upto(ctx.run, ctx.contact_id, ctx.tick)
        if track is None:
            return _unknown_rows(ctx, probe_id, spec.returns, landed)
        rows = contact_evidence(
            track,
            ctx.harbor,
            _current_at(ctx),
            ctx.pol,
            hour_of_day=ctx.scenario.hour_of_day,
        )
        if probe_id is ProbeId.ACOUSTIC_FEATURE_READ:
            picked = latest_per_feature([r for r in rows if r.feature in spec.returns])
        else:
            picked = with_provenance(rows, spec.returns)
        return restamp(picked, probe_id, landed)

    return run


def _registry_lookup(
    ctx: ProbeContext, table: EvidenceTable, args: Mapping[str, Any], landed: int
) -> list[EvidenceRow]:
    pid = ProbeId.REGISTRY_LOOKUP
    track = track_upto(ctx.run, ctx.contact_id, ctx.tick)
    if track is None:
        return _unknown_rows(ctx, pid, (FeatureName.REGISTRY_MATCH,), landed)
    modem = args.get("modem_id")
    cfg = ctx.match_cfg if ctx.match_cfg is not None else load_match_config()
    last = track.points[-1]
    lookup = registry_lookup(
        _plans(ctx), ctx.harbor, last.lat, last.lon, landed, cfg, modem_id=modem
    )
    segment = restamp([track_segments(track)["kinematic"]], pid, landed)[0]
    hail_rows: list[EvidenceRow] = []
    if modem is not None and _modem_arg(table, ctx.contact_id) == modem:
        for feature in (FeatureName.HAIL_REPLY, FeatureName.MODEM_ID):
            row = table.latest(ctx.contact_id, feature)
            if row is not None:
                hail_rows.append(row)
    rows = registry_lookup_rows(
        lookup, contact_id=ctx.contact_id, track_segment=segment, hail_rows=hail_rows
    )
    return [segment, *rows]


def _previously_replied(table: EvidenceTable, contact_id: str) -> bool:
    replies = (HailReply.VALID.value, HailReply.INVALID.value)
    return any(r.value in replies for r in table.feature_rows(contact_id, FeatureName.HAIL_REPLY))


def _acoustic_hail(
    ctx: ProbeContext, table: EvidenceTable, args: Mapping[str, Any], landed: int
) -> list[EvidenceRow]:
    hail = ctx.hail
    if hail is None:
        hail = HailSimulator(ctx.scenario, ctx.run, seed=ctx.run.seed, known=ctx.known)
    flag = args.get("previously_replied")
    previously = bool(flag) if flag is not None else _previously_replied(table, ctx.contact_id)
    result = hail.send(ctx.contact_id, ctx.tick, previously_replied=previously)
    rows = hail_evidence_rows(result, tick_s=ctx.tick_s)
    if result.reply_tick != landed:
        raise RuntimeError("hail latency and the allowlist latency disagree")
    return rows


def _camera_cue(
    ctx: ProbeContext, table: EvidenceTable, args: Mapping[str, Any], landed: int
) -> list[EvidenceRow]:
    pid = ProbeId.CAMERA_CUE
    feature = FeatureName.VISIBILITY_FOR_CAMERA
    value = round6(camera_visibility(ctx, landed))
    prior = table.latest(ctx.contact_id, feature)
    if feature in table.assumptions(ctx.contact_id) and prior is not None:
        ceiling = prior.confidence if isinstance(prior.confidence, float) else 0.0
        return [
            EvidenceRow(
                contact_id=ctx.contact_id,
                t=landed,
                feature=feature,
                value=value,
                unit=FEATURE_CATALOG[feature].unit,
                source=probe_source(pid),
                confidence=round6(min(CAMERA_CONFIDENCE, ceiling)),
                kind=EvidenceKind.DERIVED,
                probe_id=pid,
                derived_from=(prior.row_id,),
            )
        ]
    return [_fact(ctx, pid, feature, value, CAMERA_CONFIDENCE, landed)]


def _node_estimates(track: Track) -> list[tuple[str, float, float | None, float | None]]:
    """Per sensor node: (node_id, mean confidence, depth, speed), best first."""
    by_node: dict[str, list[int]] = {}
    for i, p in enumerate(track.points):
        by_node.setdefault(p.sensor_id, []).append(i)
    out: list[tuple[str, float, float | None, float | None]] = []
    for node_id in sorted(by_node):
        idx = by_node[node_id]
        sub = track.model_copy(update={"points": tuple(track.points[i] for i in idx), "gaps": ()})
        arrays = track_arrays(sub)
        conf = math.fsum(track.points[i].confidence for i in idx) / len(idx)
        out.append((node_id, conf, depth(arrays), speed_over_ground(arrays)))
    out.sort(key=lambda e: (-e[1], e[0]))
    return out


def _combine(values: Sequence[tuple[float, float]]) -> tuple[float, float] | None:
    """Confidence-weighted mean and ``1 - prod(1 - c)`` of (value, confidence) pairs."""
    if not values:
        return None
    total = math.fsum(c for _, c in values)
    mean = (
        math.fsum(v * c for v, c in values) / total
        if total > 0
        else math.fsum(v for v, _ in values) / len(values)
    )
    miss = 1.0
    for _, c in values:
        miss *= 1.0 - c
    return mean, 1.0 - miss


def _second_sensor_dwell(
    ctx: ProbeContext, table: EvidenceTable, args: Mapping[str, Any], landed: int
) -> list[EvidenceRow]:
    """Readings in the ``latency_s`` window ending at the landing tick (or at the last
    reading, if the contact falls silent first), split by node; the two best nodes by
    mean reading confidence (ties by node id) are combined."""
    pid = ProbeId.SECOND_SENSOR_DWELL
    spec = ALLOWLIST[pid]
    track = track_upto(ctx.run, ctx.contact_id, landed)
    if track is None:
        return _unknown_rows(ctx, pid, spec.returns, landed)
    end = track.points[-1].tick
    start = end - latency_ticks(spec.latency_s, ctx.tick_s)
    points = tuple(p for p in track.points if p.tick > start)
    window = track.model_copy(update={"points": points, "gaps": ()})
    best = _node_estimates(window)[:DWELL_NODES]
    rows: list[EvidenceRow] = []
    for feature, slot in ((FeatureName.DEPTH, 2), (FeatureName.SPEED_OVER_GROUND, 3)):
        pairs = [(float(e[slot]), e[1]) for e in best if e[slot] is not None]  # type: ignore[arg-type]
        combined = _combine(pairs)
        if combined is None:
            rows.append(_fact(ctx, pid, feature, UNKNOWN, UNKNOWN_CONFIDENCE, landed))
        else:
            rows.append(_fact(ctx, pid, feature, combined[0], combined[1], landed))
    return rows


def _modem_arg(table: EvidenceTable, contact_id: str) -> str | None:
    value = _latest_known(table, contact_id, FeatureName.MODEM_ID)
    return value if isinstance(value, str) else None


def _ask_operator_by_phone(
    ctx: ProbeContext, table: EvidenceTable, args: Mapping[str, Any], landed: int
) -> list[EvidenceRow]:
    pid = ProbeId.ASK_OPERATOR_BY_PHONE
    modem = args.get("modem_id")
    if modem is None:
        return _unknown_rows(ctx, pid, (FeatureName.OPERATOR_KNOWN,), landed)
    known = ctx.known if ctx.known is not None else load_known_operators(scenario=ctx.scenario)
    entry = known.lookup(str(modem))
    return [
        _fact(ctx, pid, FeatureName.OPERATOR_KNOWN, entry is not None, PHONE_CONFIDENCE, landed)
    ]


def _simulated_path(ctx: ProbeContext) -> list[tuple[int, float, float, float]]:
    """(tick, lat, lon, depth) a watch officer sent to look would see.

    The world answers this (``WorldRun.eyes_on_path``, inside the simulator); the probe
    never reads ground truth itself.
    """
    return list(ctx.run.eyes_on_path(ctx.contact_id))


def _request_watch_officer_look(
    ctx: ProbeContext, table: EvidenceTable, args: Mapping[str, Any], landed: int
) -> list[EvidenceRow]:
    pid = ProbeId.REQUEST_WATCH_OFFICER_LOOK
    spec = ALLOWLIST[pid]
    visibility = camera_visibility(ctx, landed)
    seen = [p for p in _simulated_path(ctx) if p[0] <= landed]
    if visibility <= 0.0 or not seen:
        return _unknown_rows(ctx, pid, spec.returns, landed)
    rows = [_fact(ctx, pid, FeatureName.DEPTH, seen[-1][3], visibility, landed)]
    t_cut = landed * ctx.tick_s - PATTERN_WINDOW_S
    window = [p for p in seen if p[0] * ctx.tick_s >= t_cut]
    xy = [to_local_m(lat, lon) for _, lat, lon, _ in window]
    arrays = TrackArrays.from_columns(
        [p[0] * ctx.tick_s for p in window],
        [x for x, _ in xy],
        [y for _, y in xy],
        [p[3] for p in window],
        [1.0] * len(window),
        [EYES_ON_SIGMA_M] * len(window),
    )
    pattern = classify_transit_pattern(arrays)
    value: EvidenceValue = UNKNOWN if pattern is None else pattern.value
    rows.append(_fact(ctx, pid, FeatureName.TRANSIT_PATTERN, value, visibility, landed))
    return rows


SECTION_5_EXECUTORS: Final[Mapping[ProbeId, Executor]] = MappingProxyType(
    {
        ProbeId.REGISTRY_LOOKUP: _registry_lookup,
        ProbeId.KINEMATICS_PROFILE: _watch_executor(ProbeId.KINEMATICS_PROFILE),
        ProbeId.DRIFT_CHECK: _watch_executor(ProbeId.DRIFT_CHECK),
        ProbeId.PATTERN_OF_LIFE_CHECK: _watch_executor(ProbeId.PATTERN_OF_LIFE_CHECK),
        ProbeId.AIS_SURFACE_CORRELATE: _watch_executor(ProbeId.AIS_SURFACE_CORRELATE),
        ProbeId.ACOUSTIC_FEATURE_READ: _watch_executor(ProbeId.ACOUSTIC_FEATURE_READ),
        ProbeId.ACOUSTIC_HAIL: _acoustic_hail,
        ProbeId.CAMERA_CUE: _camera_cue,
        ProbeId.SECOND_SENSOR_DWELL: _second_sensor_dwell,
        ProbeId.ASK_OPERATOR_BY_PHONE: _ask_operator_by_phone,
        ProbeId.REQUEST_WATCH_OFFICER_LOOK: _request_watch_officer_look,
    }
)


# --- WO-088 begin ---
# Responder platform probes (CLAUDE.md section 8 A2). The responder is a mobile probe at
# rung ask: it is dispatched to look at a contact (and orbits at standoff, never inside)
# or to search a LOST_VEHICLE drift box. It is never an action against anything. The
# planner may request a look, never a position: the station point, the standoff and the
# geofence belong to the responder simulation (``soundings.sim.responder``).

RESPONDER_PROBES: Final[frozenset[ProbeId]] = frozenset(
    {ProbeId.RESPONDER_LOOK, ProbeId.RESPONDER_SEARCH_BOX}
)
ON_STATION: Final = "ON_STATION"
LOOK_BASE_CONFIDENCE: Final = 0.9
"""Demo value: a close look in clear water at standoff (scaled by visibility and range)."""
CLOSE_ACOUSTIC_CONFIDENCE: Final = 0.9
"""Demo value: close-range acoustic features heard at standoff (no visibility term)."""
LOOK_RANGE_REF_M: Final = 25.0
"""Demo value: the range factor is 1 at or inside this range, ``ref / range`` beyond."""
SEARCH_DETECT_RADIUS_M: Final = 50.0
"""Demo value: the responder's search sensor sees a vehicle within this range."""
SEARCH_CONFIDENCE: Final = 0.9
"""Demo value: confidence of a ``search_result`` row (found or not found)."""
NOMINAL_RESPONDER_SPEED_MPS: Final = 1.5
"""Demo value (config/responder.yaml ``speed_mps``); used only without a responder."""
NOMINAL_LOOK_TRANSIT_S: Final = 400.0
"""Demo value: the static ``latency_s`` of ``responder_look`` (600 m at 1.5 m/s); a real
dispatch uses the transit time from the responder's position (``responder_args``)."""
STATION_ROUND_M: Final = (math.pi + 2.0) * 25.0
"""Demo value: half an orbit plus a diameter at the 25 m standoff."""
MAX_MOVING_CONTACT_FACTOR: Final = 3.0
"""Demo value: cap on the transit stretch for a moving contact (it may never be reached;
the look then lands with no rows)."""
SPEED_WINDOW_S: Final = 120.0
"""Demo value: the contact's speed for the transit estimate is its net displacement over
the last two minutes of track."""
NOMINAL_SEARCH_S: Final = 900.0
"""Demo value: the static ``latency_s`` of ``responder_search_box``."""
LOOK_ACOUSTIC_FEATURES: Final[tuple[FeatureName, ...]] = (
    FeatureName.PROPELLER_TONAL_PRESENT,
    FeatureName.TONAL_BAND,
    FeatureName.CAVITATION_INDEX,
    FeatureName.BREATHING_OR_CLICK_PRESENT,
)
LOOK_RETURNS: Final[tuple[FeatureName, ...]] = (
    FeatureName.VISUAL_CLASS,
    FeatureName.DEPTH,
    FeatureName.TRANSIT_PATTERN,
    *LOOK_ACOUSTIC_FEATURES,
)
SEARCH_RETURNS: Final[tuple[FeatureName, ...]] = (
    FeatureName.SEARCH_RESULT,
    FeatureName.SEARCH_POSITION,
)
_RESPONDER_UNITS: Final[Mapping[FeatureName, Unit]] = MappingProxyType(
    {
        FeatureName.VISUAL_CLASS: Unit.CATEGORY,
        FeatureName.SEARCH_RESULT: Unit.CATEGORY,
        FeatureName.SEARCH_POSITION: Unit.ID,
    }
)
RESPONDER_ALLOWLIST: Final[Mapping[ProbeId, ProbeSpec]] = MappingProxyType(
    {
        ProbeId.RESPONDER_LOOK: ProbeSpec(
            probe_id=ProbeId.RESPONDER_LOOK,
            rung_required=Rung.ASK,
            cost_usd=0.0,
            latency_s=NOMINAL_LOOK_TRANSIT_S,
            interruption=0,
            returns=LOOK_RETURNS,
            nominal_confidence=CLOSE_ACOUSTIC_CONFIDENCE,
        ),
        ProbeId.RESPONDER_SEARCH_BOX: ProbeSpec(
            probe_id=ProbeId.RESPONDER_SEARCH_BOX,
            rung_required=Rung.ASK,
            cost_usd=0.0,
            latency_s=NOMINAL_SEARCH_S,
            interruption=0,
            returns=SEARCH_RETURNS,
            nominal_confidence=SEARCH_CONFIDENCE,
        ),
    }
)
"""The two responder probes: rung ask, no money, latency = transit (and search) time."""

ALLOWLIST: Final[Mapping[ProbeId, ProbeSpec]] = MappingProxyType(
    {**SECTION_5_ALLOWLIST, **RESPONDER_ALLOWLIST}
)
"""Section 5 in order, then the WO-088 responder probes (``ProbeId`` order)."""


class _SnapshotLike(Protocol):
    """The ``ResponderSnapshot`` fields a probe reads (``soundings.sim.responder``)."""

    @property
    def tick(self) -> int: ...
    @property
    def lat(self) -> float: ...
    @property
    def lon(self) -> float: ...
    @property
    def state(self) -> Any: ...
    @property
    def distance_to_target_m(self) -> float | None: ...


class _DispatchLike(Protocol):
    @property
    def accepted(self) -> bool: ...
    @property
    def reason(self) -> str: ...
    @property
    def eta_s(self) -> float: ...


class ResponderPort(Protocol):
    """The responder simulation as the probe runner sees it (WO-088 contract)."""

    def dispatch_to_look(
        self, contact_id: str, contact_lat: float, contact_lon: float, tick: int
    ) -> _DispatchLike: ...

    def dispatch_search(self, contact_id: str, search_box: Any, tick: int) -> _DispatchLike: ...

    def step(
        self,
        tick: int,
        *,
        contact_lat: float | None = None,
        contact_lon: float | None = None,
        contact_speed_mps: float | None = None,
        nearest_asset_lat: float | None = None,
        nearest_asset_lon: float | None = None,
    ) -> _SnapshotLike: ...

    @property
    def snapshot(self) -> _SnapshotLike: ...

    def travel_time_s(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float: ...


def catch_up(port: ResponderPort, tick: int) -> _SnapshotLike:
    """Step an idle or returning responder up to ``tick`` (no contact: it carries on
    with what it was doing, usually the return to dock). Deterministic; no clock."""
    snap = port.snapshot
    for t in range(int(snap.tick) + 1, tick + 1):
        snap = port.step(t)
    return snap


def release(port: ResponderPort, tick: int) -> None:
    """The look or search is done: return to dock (``recall``), if the port has one."""
    recall = getattr(port, "recall", None)
    if callable(recall):
        recall(tick)


def state_name(snapshot: _SnapshotLike) -> str:
    """The responder state as a plain string (``ResponderState`` member or str)."""
    state = snapshot.state
    return str(getattr(state, "name", state))


def on_station(snapshot: _SnapshotLike | None) -> bool:
    return snapshot is not None and state_name(snapshot) == ON_STATION


def _contact_fix(ctx: ProbeContext, tick: int) -> tuple[float, float, float | None] | None:
    """The contact's last observed (lat, lon, speed) at or before ``tick``."""
    track = track_upto(ctx.run, ctx.contact_id, tick)
    if track is None:
        return None
    last = track.points[-1]
    speed: float | None = None
    window = [p for p in track.points if (last.tick - p.tick) * ctx.tick_s <= SPEED_WINDOW_S]
    first = window[0]
    dt_s = (last.tick - first.tick) * ctx.tick_s
    if dt_s > 0:  # net displacement over the window (single-reading noise averages out)
        speed = distance_m(first.lat, first.lon, last.lat, last.lon) / dt_s
    return float(last.lat), float(last.lon), speed


def _responder_eta_s(ctx: ProbeContext, pid: ProbeId) -> float:
    """Transit time from where the responder is to the contact (or to the drift box)."""
    port = ctx.responder
    if port is None:
        return ALLOWLIST[pid].latency_s
    snap = port.snapshot
    if pid is ProbeId.RESPONDER_SEARCH_BOX:
        box = ctx.search_box
        if box is None:
            return ALLOWLIST[pid].latency_s
        transit = port.travel_time_s(snap.lat, snap.lon, box.center_lat, box.center_lon)
        side = 2.0 * box.radius_m
        lanes = max(1, math.ceil(side / (2.0 * SEARCH_DETECT_RADIUS_M)))
        sweep_m = lanes * side + side
        speed = NOMINAL_RESPONDER_SPEED_MPS
        return transit + sweep_m / speed
    fix = _contact_fix(ctx, ctx.tick)
    if fix is None:
        return ALLOWLIST[pid].latency_s
    transit = port.travel_time_s(snap.lat, snap.lon, fix[0], fix[1])
    # Room to come round to the station point on the far side of the contact.
    base = transit + STATION_ROUND_M / NOMINAL_RESPONDER_SPEED_MPS
    speed = NOMINAL_RESPONDER_SPEED_MPS
    contact_speed = fix[2] if fix[2] is not None else 0.0
    if contact_speed >= speed:
        return base * MAX_MOVING_CONTACT_FACTOR
    return base * min(MAX_MOVING_CONTACT_FACTOR, speed / (speed - contact_speed))


def responder_args(pid: ProbeId, ctx: ProbeContext) -> dict[str, Any]:
    """``{"eta_ticks": n}``: the ticks the dispatch takes, fixed when it is chosen."""
    return {"eta_ticks": latency_ticks(round6(_responder_eta_s(ctx, pid)), ctx.tick_s)}


def key_args(pid: ProbeId, args: Mapping[str, Any]) -> dict[str, Any]:
    """The args in the idempotency key: a responder's ``eta_ticks`` depends on where the
    responder is now, so it is left out (one dispatch per contact and bucket)."""
    if pid in RESPONDER_PROBES:
        return {k: v for k, v in sorted(args.items()) if k != "eta_ticks"}
    return dict(args)


def probe_latency_ticks(pid: ProbeId, args: Mapping[str, Any], tick_s: float) -> int:
    """Ticks from send to landing: the allowlist latency, or a responder's ``eta_ticks``."""
    eta = args.get("eta_ticks") if pid in RESPONDER_PROBES else None
    if isinstance(eta, int) and not isinstance(eta, bool) and eta >= 0:
        return eta
    return latency_ticks(ALLOWLIST[pid].latency_s, tick_s)


def _responder_row(
    ctx: ProbeContext,
    pid: ProbeId,
    feature: FeatureName,
    value: EvidenceValue,
    confidence: float,
    t: int,
) -> EvidenceRow:
    unit = _RESPONDER_UNITS.get(feature)
    if unit is None:
        return _fact(ctx, pid, feature, value, confidence, t)
    known = not _is_unknown(value)
    return EvidenceRow(
        contact_id=ctx.contact_id,
        t=t,
        feature=feature,
        value=_value(value),
        unit=unit,
        source=probe_source(pid),
        confidence=round6(confidence) if known and confidence > 0 else UNKNOWN_CONFIDENCE,
        kind=EvidenceKind.FACT,
        probe_id=pid,
    )


def _responder_unknown(ctx: ProbeContext, pid: ProbeId, t: int) -> list[EvidenceRow]:
    return [
        _responder_row(ctx, pid, f, UNKNOWN, UNKNOWN_CONFIDENCE, t) for f in ALLOWLIST[pid].returns
    ]


def _nearest_asset(ctx: ProbeContext, lat: float, lon: float) -> tuple[float, float] | None:
    asset = ctx.harbor.nearest_asset(lat, lon)
    return None if asset is None else (float(asset.lat), float(asset.lon))


def _step_to(ctx: ProbeContext, port: ResponderPort, start: int, end: int) -> _SnapshotLike | None:
    """Step the responder tick by tick over ``(start, end]`` with the contact's last fix;
    the last ``ON_STATION`` snapshot on the way (None if it never got on station)."""
    snap = port.snapshot
    best = snap if on_station(snap) else None
    for t in range(max(start, int(snap.tick)) + 1, end + 1):
        fix = _contact_fix(ctx, t)
        asset = None if fix is None else _nearest_asset(ctx, fix[0], fix[1])
        snap = port.step(
            t,
            contact_lat=None if fix is None else fix[0],
            contact_lon=None if fix is None else fix[1],
            contact_speed_mps=None if fix is None else fix[2],
            nearest_asset_lat=None if asset is None else asset[0],
            nearest_asset_lon=None if asset is None else asset[1],
        )
        if on_station(snap):
            best = snap
    return best


def range_factor(range_m: float | None) -> float:
    """1 at or inside ``LOOK_RANGE_REF_M``, ``ref / range`` beyond (0 when unknown)."""
    if range_m is None or range_m < 0:
        return 0.0
    return 1.0 if range_m <= LOOK_RANGE_REF_M else LOOK_RANGE_REF_M / range_m


def _close_look(ctx: ProbeContext, t: int) -> CloseLook | None:
    """The world's answer to a close look (``CloseLookWorld.close_look``), if it has one."""
    look = getattr(ctx.run, "close_look", None)
    if not callable(look):
        return None
    answer = look(ctx.contact_id, t)
    return answer if isinstance(answer, CloseLook) else None


def _responder_look(
    ctx: ProbeContext, table: EvidenceTable, args: Mapping[str, Any], landed: int
) -> list[EvidenceRow]:
    """Dispatch to look, step to the landing tick, and read only once ``ON_STATION``.

    Without a responder (none in this harbor) every feature is an ``unknown`` row. A
    dispatch the responder declines (outside the geofence, battery, already busy) is
    ``unknown`` too. Before ``ON_STATION`` the look returns no rows at all.
    """
    pid = ProbeId.RESPONDER_LOOK
    port = ctx.responder
    fix = _contact_fix(ctx, ctx.tick)
    if port is None or fix is None:
        return _responder_unknown(ctx, pid, landed)
    catch_up(port, ctx.tick)
    dispatch = port.dispatch_to_look(ctx.contact_id, fix[0], fix[1], ctx.tick)
    if not dispatch.accepted:
        return _responder_unknown(ctx, pid, landed)
    snap = _step_to(ctx, port, ctx.tick, landed)
    release(port, landed)
    if snap is None:
        return []
    visibility = camera_visibility(ctx, landed)
    factor = range_factor(snap.distance_to_target_m)
    close_look = _close_look(ctx, landed)
    rows: list[EvidenceRow] = []
    visual: EvidenceValue = UNKNOWN
    seen_class = None if close_look is None else close_look.visual_class
    if seen_class is not None and visibility > 0.0 and factor > 0.0:
        visual = seen_class
    rows.append(
        _responder_row(
            ctx, pid, FeatureName.VISUAL_CLASS, visual,
            LOOK_BASE_CONFIDENCE * visibility * factor, landed,
        )
    )  # fmt: skip
    seen = [p for p in _simulated_path(ctx) if p[0] <= landed]
    depth_value: EvidenceValue = seen[-1][3] if seen else UNKNOWN
    close = CLOSE_ACOUSTIC_CONFIDENCE * factor
    rows.append(_responder_row(ctx, pid, FeatureName.DEPTH, depth_value, close, landed))
    window = [p for p in seen if p[0] * ctx.tick_s >= landed * ctx.tick_s - PATTERN_WINDOW_S]
    pattern_value: EvidenceValue = UNKNOWN
    if window:
        xy = [to_local_m(lat, lon) for _, lat, lon, _ in window]
        arrays = TrackArrays.from_columns(
            [p[0] * ctx.tick_s for p in window],
            [x for x, _ in xy],
            [y for _, y in xy],
            [p[3] for p in window],
            [1.0] * len(window),
            [EYES_ON_SIGMA_M] * len(window),
        )
        pattern = classify_transit_pattern(arrays)
        pattern_value = UNKNOWN if pattern is None else pattern.value
    rows.append(_responder_row(ctx, pid, FeatureName.TRANSIT_PATTERN, pattern_value, close, landed))
    heard = {} if close_look is None else close_look.acoustics
    for feature in LOOK_ACOUSTIC_FEATURES:
        value: EvidenceValue = heard.get(feature.value, UNKNOWN)
        rows.append(_responder_row(ctx, pid, feature, value, close, landed))
    return rows


def _truth_at(ctx: ProbeContext, tick: int) -> tuple[float, float] | None:
    seen = [p for p in _simulated_path(ctx) if p[0] <= tick]
    return None if not seen else (seen[-1][1], seen[-1][2])


def _responder_search_box(
    ctx: ProbeContext, table: EvidenceTable, args: Mapping[str, Any], landed: int
) -> list[EvidenceRow]:
    """Lawnmower the LOST_VEHICLE drift box; ``found`` with a position or ``not_found``.

    The responder's search sensor is simulated: it sees the vehicle when the simulated
    vehicle is within ``SEARCH_DETECT_RADIUS_M`` of the responder at a tick.
    """
    pid = ProbeId.RESPONDER_SEARCH_BOX
    port = ctx.responder
    box = ctx.search_box
    if port is None or box is None:
        return _responder_unknown(ctx, pid, landed)
    catch_up(port, ctx.tick)
    dispatch = port.dispatch_search(ctx.contact_id, box, ctx.tick)
    if not dispatch.accepted:
        return _responder_unknown(ctx, pid, landed)
    found: tuple[float, float] | None = None
    snap = port.snapshot
    for t in range(max(ctx.tick, int(snap.tick)) + 1, landed + 1):
        snap = port.step(t)
        where = _truth_at(ctx, t)
        if where is not None and distance_m(snap.lat, snap.lon, *where) <= SEARCH_DETECT_RADIUS_M:
            found = where
            break
    release(port, int(snap.tick))
    if found is None:
        return [
            _responder_row(
                ctx, pid, FeatureName.SEARCH_RESULT, "not_found", SEARCH_CONFIDENCE, landed
            ),
            _responder_row(ctx, pid, FeatureName.SEARCH_POSITION, UNKNOWN, 0.0, landed),
        ]
    position = f"{round6(found[0]):.6f},{round6(found[1]):.6f}"
    return [
        _responder_row(ctx, pid, FeatureName.SEARCH_RESULT, "found", SEARCH_CONFIDENCE, landed),
        _responder_row(ctx, pid, FeatureName.SEARCH_POSITION, position, SEARCH_CONFIDENCE, landed),
    ]


def search_position(rows: Sequence[EvidenceRow]) -> tuple[float, float] | None:
    """The ``(lat, lon)`` a ``responder_search_box`` found, or ``None``."""
    for row in rows:
        if row.feature is FeatureName.SEARCH_POSITION and isinstance(row.value, str):
            if row.value != UNKNOWN and "," in row.value:
                lat, lon = row.value.split(",", 1)
                return float(lat), float(lon)
    return None


RESPONDER_EXECUTORS: Final[Mapping[ProbeId, Executor]] = MappingProxyType(
    {
        ProbeId.RESPONDER_LOOK: _responder_look,
        ProbeId.RESPONDER_SEARCH_BOX: _responder_search_box,
    }
)
EXECUTORS: Final[Mapping[ProbeId, Executor]] = MappingProxyType(
    {**SECTION_5_EXECUTORS, **RESPONDER_EXECUTORS}
)
# --- WO-088 end ---


# --- the entry point ----------------------------------------------------------------------


def resolve_args(
    probe_id: ProbeId | str,
    ctx: ProbeContext,
    table: EvidenceTable,
    args: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The args a probe runs with, which are also the args in its idempotency key.

    Given args win. With none, ``registry_lookup`` and ``ask_operator_by_phone`` take the
    contact's latest known ``modem_id`` row (``{"modem_id": ...}``, the runner's key
    shape); every other probe runs with ``{}`` (the hail key matches
    ``HailSimulator.key``).
    """
    pid = as_probe_id(probe_id)
    if args:
        return dict(sorted(args.items()))
    if pid in RESPONDER_PROBES:  # WO-088: the transit time is part of the key
        return responder_args(pid, ctx)
    if pid in (ProbeId.REGISTRY_LOOKUP, ProbeId.ASK_OPERATOR_BY_PHONE):
        modem = _modem_arg(table, ctx.contact_id)
        if modem is not None:
            return {"modem_id": modem}
    return {}


def probe_key(
    probe_id: ProbeId | str,
    ctx: ProbeContext,
    table: EvidenceTable,
    args: Mapping[str, Any] | None = None,
) -> str:
    """The section 5 idempotency key ``run_probe`` would use for this call."""
    pid = as_probe_id(probe_id)
    resolved = key_args(pid, resolve_args(pid, ctx, table, args))  # WO-088
    return idempotency_key(ctx.contact_id, pid.value, resolved, ctx.bucket())


def run_probe(
    probe_id: ProbeId | str,
    ctx: ProbeContext,
    table: EvidenceTable,
    *,
    args: Mapping[str, Any] | None = None,
    cache: ProbeCache,
) -> ProbeResult:
    """Run one allowlisted probe and append its rows to ``table``.

    Raises ``ProbeNotAllowed`` (nothing appended) for an id outside ``ALLOWLIST`` or a
    probe whose ``rung_required`` is above ``ctx.current_rung``. A key already in
    ``cache`` returns the stored result with ``cached=True`` without running anything.
    Rows already in the table (same content hash) are not appended twice.
    """
    pid = as_probe_id(probe_id)
    spec = ALLOWLIST[pid]
    if rung_index(spec.rung_required) > rung_index(ctx.current_rung):
        raise ProbeNotAllowed(
            f"{pid.value} needs rung {spec.rung_required.value}; "
            f"the contact is at {Rung(ctx.current_rung).value}"
        )
    resolved = resolve_args(pid, ctx, table, args)
    key = idempotency_key(ctx.contact_id, pid.value, key_args(pid, resolved), ctx.bucket())
    hit = cache.get(key)
    if hit is not None:
        return hit.model_copy(update={"cached": True})
    landed = ctx.tick + probe_latency_ticks(pid, resolved, ctx.tick_s)  # WO-088: dynamic
    rows = EXECUTORS[pid](ctx, table, resolved, landed)
    cache._executed(pid)
    for row in rows:
        if row.probe_id is not pid or row.contact_id != ctx.contact_id:
            raise RuntimeError(f"{pid.value} returned a row it does not own: {row.row_id}")
    fresh = [r for r in dict.fromkeys(rows) if r.row_id not in table]
    table.extend(fresh)
    result = ProbeResult(
        key=key,
        probe_id=pid,
        contact_id=ctx.contact_id,
        sent_tick=ctx.tick,
        landed_tick=landed,
        args=resolved,
        rows=tuple(dict.fromkeys(rows)),
    )
    cache.put(result)
    return result


__all__ = [
    "ALLOWLIST",
    "EXECUTORS",
    "RESPONDER_ALLOWLIST",
    "RESPONDER_PROBES",
    "SECTION_5_ALLOWLIST",
    "ResponderPort",
    "on_station",
    "probe_latency_ticks",
    "key_args",
    "responder_args",
    "search_position",
    "catch_up",
    "release",
    "state_name",
    "NOMINAL_CONFIDENCE",
    "PROBE_SOURCE_PREFIX",
    "TICK_BUCKET_S",
    "TRACK_DERIVED_CONFIDENCE",
    "ProbeCache",
    "ProbeContext",
    "ProbeNotAllowed",
    "ProbeResult",
    "ProbeSpec",
    "as_probe_id",
    "camera_visibility",
    "fog_active",
    "idempotency_key",
    "latency_ticks",
    "latest_per_feature",
    "probe_key",
    "probe_source",
    "resolve_args",
    "restamp",
    "rung_index",
    "run_probe",
    "tick_bucket",
    "track_upto",
    "with_provenance",
]
