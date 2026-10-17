"""A synthetic S05-shaped feed for the ladder driver (WO-043 part A; part B uses runner v3).

The shape follows ``tests/support/guard_streams.py`` and SPEC-SIM S05: an unfiled contact
inside ``fuel_berths`` that does not answer the hail, threat mass 0.7, zone criticality
0.9, and a time to ``fuel_berth_1`` that falls by one tick's worth of seconds per tick from
450 s at the handoff recommendation. Every number is a stress-test setting for the
demonstration, not an operational figure. ``depart_at_tick`` moves the contact outside
every harbor zone from that tick on (the watch-after-handoff departure case).
"""

from __future__ import annotations

from soundings.canonical import round6
from soundings.schemas import (
    EvidenceKind,
    EvidenceRow,
    EvidenceValue,
    FeatureName,
    ProbeId,
    Rung,
    TrackPoint,
    Unit,
)

from .driver import ContactFeed, Snapshot

S05_CONTACT = "c-0005"
S05_HOUR = 2
S05_TICK_S = 2.0
S05_BASIN = "inner_harbor_west"
S05_ASSET = "fuel_berth_1"
INSIDE = (33.743, -118.264)
"""A point inside ``fuel_berths`` (config/harbor.geojson)."""
OUTSIDE = (33.70, -118.23)
"""A point outside every harbor zone."""

MONITOR_SOURCE = "ladder_monitor"
HANDOFF_TICK = 30
HANDOFF_TTA_S = 450.0
THREAT = 0.7
BENIGN = 0.1
CRITICALITY = 0.9

F = FeatureName
P = ProbeId


def _row(
    cid: str,
    t: int,
    feature: FeatureName,
    value: EvidenceValue,
    unit: Unit,
    probe: ProbeId | None,
    source: str | None = None,
    confidence: float = 0.9,
) -> EvidenceRow:
    if source is None:
        source = f"probe:{probe.value}" if probe is not None else "sim.detector:hyd_1"
    return EvidenceRow(
        contact_id=cid,
        t=t,
        feature=feature,
        value=value,
        unit=unit,
        source=source,
        confidence=confidence,
        kind=EvidenceKind.FACT,
        probe_id=probe,
    )


def _monitor(cid: str, t: int, tta: float) -> tuple[EvidenceRow, ...]:
    return (
        _row(cid, t, F.TIME_TO_NEAREST_CRITICAL_ASSET, tta, Unit.S, None, MONITOR_SOURCE),
        _row(cid, t, F.ZONE_CRITICALITY, CRITICALITY, Unit.RATIO, None, MONITOR_SOURCE),
        _row(cid, t, F.NEAREST_ASSET_ID, S05_ASSET, Unit.ID, None, MONITOR_SOURCE),
    )


def s05_feed(
    *,
    ticks: int = 1000,
    depart_at_tick: int | None = None,
    cid: str = S05_CONTACT,
    tick_s: float = S05_TICK_S,
) -> ContactFeed:
    """The S05-shaped feed over ticks ``0..ticks-1``."""
    recs: list[Snapshot] = []
    for t in range(HANDOFF_TICK, ticks):
        tta = max(0.0, round6(HANDOFF_TTA_S - (t - HANDOFF_TICK) * tick_s))
        recs.append(
            Snapshot(
                tick=t,
                rows=_monitor(cid, t, tta),
                outcome="HANDOFF" if t == HANDOFF_TICK else None,
                recommended_rung=Rung.HAND_TO_PERSON,
                threat_mass=THREAT,
                benign_mass=BENIGN,
                basin=S05_BASIN,
                nearest_asset_id=S05_ASSET,
                time_to_nearest_critical_asset=tta,
            )
        )
    points = []
    for t in range(ticks):
        lat, lon = OUTSIDE if depart_at_tick is not None and t >= depart_at_tick else INSIDE
        points.append(
            TrackPoint(
                tick=t,
                lat=lat,
                lon=lon,
                depth_m=6.0,
                sigma_m=10.0,
                sensor_id="hyd_1",
                confidence=0.9,
            )
        )
    return ContactFeed(
        contact_id=cid,
        detected_tick=0,
        detection_rows=(_row(cid, 0, F.ZONE_ID, "fuel_berths", Unit.ID, None),),
        watch_tick=1,
        watch_rows=(
            _row(cid, 1, F.SPEED_OVER_GROUND, 1.2, Unit.M_PER_S, P.KINEMATICS_PROFILE),
            _row(cid, 1, F.DRIFT_CONSISTENCY, 0.1, Unit.RATIO, P.DRIFT_CHECK),
        ),
        ask_tick=12,
        ask_rows=(
            _row(cid, 12, F.REGISTRY_MATCH, "none", Unit.CATEGORY, P.REGISTRY_LOOKUP, None, 1.0),
            _row(cid, 12, F.HAIL_REPLY, "none", Unit.CATEGORY, P.ACOUSTIC_HAIL),
        ),
        recommendations=tuple(recs),
        points=tuple(points),
    )


__all__ = ["INSIDE", "OUTSIDE", "S05_CONTACT", "S05_HOUR", "S05_TICK_S", "s05_feed"]
