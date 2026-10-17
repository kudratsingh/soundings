"""Surge triage (WO-085): a pure priority queue and one serial watch-officer channel.

When contacts arrive faster than the harbor can look at them, Soundings investigates and
interrupts in order of urgency, not in order of arrival:

    priority = round6(threat_mass * zone_criticality / max(ttc_s, ttc_floor_s))

ties broken by contact id, then case id. ``TriageQueue`` orders the cases waiting for an
investigation slot; ``OfficerArbiter`` orders the requests that need the watch officer
(DECISIONS 2026-10-16 23:05, one interruption definition). Handoffs, approval requests
and backup pages are mandatory: they are ordered, never refused, never discarded. An
eyes-on look is optional and capped per trailing window (DECISIONS 2026-10-17 06:37).

Rules (CLAUDE.md section 2): triage only orders work. It writes no ladder state and sets
no rung (2.1); it reads evidence rows and the posterior only, never model text (2.2); it
has no clock, no randomness and no hash-order dependence; the queue is a list sorted on
every read (2.4); every queued decision cites the rows behind its priority (2.6). Every
number comes from ``config/triage.yaml`` or ``config/eval.yaml`` (section 3).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import Field, NonNegativeInt, PositiveFloat, PositiveInt

from soundings.canonical import canonical_json, round6
from soundings.eval.policies import EVAL_CONFIG_PATH, InterruptionKind, load_eval_config
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import NOT_CLOSING_S
from soundings.investigation.posterior import (
    LikelihoodTables,
    PriorConfig,
    score_contact,
)
from soundings.schemas._base import StrictModel, from_file_data
from soundings.schemas.enums import FeatureName

TRIAGE_PATH = Path(__file__).resolve().parent.parent / "config" / "triage.yaml"

MANDATORY_KINDS: frozenset[InterruptionKind] = frozenset(
    {InterruptionKind.HANDOFF, InterruptionKind.APPROVAL_REQUEST, InterruptionKind.BACKUP_PAGE}
)
"""Ordered, never refused (DECISIONS 2026-10-17 06:37)."""
OPTIONAL_KINDS: frozenset[InterruptionKind] = frozenset({InterruptionKind.EYES_ON})


# --- config -------------------------------------------------------------------------------


class _TriageFile(StrictModel):
    ttc_floor_s: PositiveFloat
    investigation_slots: PositiveInt
    optional_interruptions_per_hour: NonNegativeInt
    optional_window_s: PositiveFloat
    unknown_zone_criticality: float = Field(ge=0.0, le=1.0)


class TriageConfig(StrictModel):
    """``config/triage.yaml`` plus ``officer_review_s`` from ``config/eval.yaml``."""

    ttc_floor_s: PositiveFloat
    investigation_slots: PositiveInt
    optional_interruptions_per_hour: NonNegativeInt
    optional_window_s: PositiveFloat
    unknown_zone_criticality: float = Field(ge=0.0, le=1.0)
    officer_review_s: PositiveFloat


def load_triage_config(
    path: str | Path = TRIAGE_PATH, eval_path: str | Path = EVAL_CONFIG_PATH
) -> TriageConfig:
    """Read ``config/triage.yaml`` (extra keys rejected) and the officer's review time
    (``baselines.human_review_s``, an existing labelled assumption)."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    for key in ("ttc_floor_s", "optional_window_s", "unknown_zone_criticality"):
        if key in doc and isinstance(doc[key], int) and not isinstance(doc[key], bool):
            doc[key] = float(doc[key])
    tf = _TriageFile.model_validate(from_file_data(doc))
    review = load_eval_config(eval_path).baselines.human_review_s
    return TriageConfig(**tf.model_dump(), officer_review_s=float(review))


# --- priority -----------------------------------------------------------------------------


def _check_finite(name: str, x: float) -> float:
    v = float(x)
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite, got {x!r}")
    return v


def _check_unit(name: str, x: float) -> float:
    v = _check_finite(name, x)
    if not 0.0 <= v <= 1.0:
        raise ValueError(f"{name} must be in [0, 1], got {x!r}")
    return v


def priority(threat_mass: float, zone_criticality: float, ttc_s: float, *, floor_s: float) -> float:
    """``round6(threat_mass * zone_criticality / max(ttc_s, floor_s))``."""
    tm = _check_unit("threat_mass", threat_mass)
    zc = _check_unit("zone_criticality", zone_criticality)
    ttc = _check_finite("ttc_s", ttc_s)
    floor = _check_finite("floor_s", floor_s)
    if floor <= 0.0:
        raise ValueError(f"floor_s must be > 0, got {floor_s!r}")
    return round6(tm * zc / max(ttc, floor))


@dataclass(frozen=True)
class TriageInputs:
    contact_id: str
    case_id: str
    tick: int
    threat_mass: float
    zone_criticality: float
    ttc_s: float
    evidence_row_ids: tuple[str, ...]


def _numeric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def triage_inputs(
    table: EvidenceTable,
    contact_id: str,
    case_id: str,
    tick: int,
    tables: LikelihoodTables,
    prior_cfg: PriorConfig,
    cfg: TriageConfig,
) -> TriageInputs:
    """The three priority inputs from evidence rows only, with the rows they cite.

    ``threat_mass`` from ``score_contact`` (its ``used_row_ids``); ``zone_criticality`` from
    the latest row (unknown/absent: ``cfg.unknown_zone_criticality``, no row cited);
    ``ttc_s`` from the latest ``time_to_nearest_critical_asset`` row (unknown/absent:
    ``NOT_CLOSING_S``, no row cited)."""
    report = score_contact(table, contact_id, tables, prior_cfg)
    ids: list[str] = list(report.used_row_ids)
    zone = cfg.unknown_zone_criticality
    zrow = table.latest(contact_id, FeatureName.ZONE_CRITICALITY)
    if zrow is not None:
        zv = _numeric(zrow.value)
        if zv is not None:
            zone = min(1.0, max(0.0, zv))
            ids.append(zrow.row_id)
    ttc = float(NOT_CLOSING_S)
    trow = table.latest(contact_id, FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET)
    if trow is not None:
        tv = _numeric(trow.value)
        if tv is not None:
            ttc = tv
            ids.append(trow.row_id)
    return TriageInputs(
        contact_id=contact_id,
        case_id=case_id,
        tick=int(tick),
        threat_mass=round6(min(1.0, max(0.0, report.posterior.threat_mass()))),
        zone_criticality=round6(zone),
        ttc_s=round6(ttc),
        evidence_row_ids=tuple(sorted(set(ids))),
    )


@dataclass(frozen=True)
class TriageEntry:
    inputs: TriageInputs
    priority: float

    @classmethod
    def of(cls, inputs: TriageInputs, cfg: TriageConfig) -> TriageEntry:
        p = priority(
            inputs.threat_mass, inputs.zone_criticality, inputs.ttc_s, floor_s=cfg.ttc_floor_s
        )
        return cls(inputs=inputs, priority=p)

    @property
    def case_id(self) -> str:
        return self.inputs.case_id

    @property
    def contact_id(self) -> str:
        return self.inputs.contact_id

    @property
    def sort_key(self) -> tuple[float, str, str]:
        return (-self.priority, self.inputs.contact_id, self.inputs.case_id)


# --- the queue ----------------------------------------------------------------------------


class TriageQueue:
    """Cases waiting for an investigation slot. A plain list sorted on every read."""

    def __init__(self, entries: Iterable[TriageEntry] = ()) -> None:
        self._entries: list[TriageEntry] = []
        for e in entries:
            self.push(e)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, case_id: object) -> bool:
        return any(e.case_id == case_id for e in self._entries)

    def push(self, entry: TriageEntry) -> None:
        if entry.case_id in self:
            raise ValueError(f"{entry.case_id} is already queued")
        self._entries.append(entry)

    def rekey(self, entry: TriageEntry) -> None:
        """Replace the entry with the same ``case_id``."""
        self.remove(entry.case_id)
        self._entries.append(entry)

    def remove(self, case_id: str) -> TriageEntry:
        for i, e in enumerate(self._entries):
            if e.case_id == case_id:
                return self._entries.pop(i)
        raise KeyError(case_id)

    def get(self, case_id: str) -> TriageEntry:
        for e in self._entries:
            if e.case_id == case_id:
                return e
        raise KeyError(case_id)

    def ordered(self) -> tuple[TriageEntry, ...]:
        return tuple(sorted(self._entries, key=lambda e: e.sort_key))

    def rank(self, case_id: str) -> int:
        for i, e in enumerate(self.ordered()):
            if e.case_id == case_id:
                return i
        raise KeyError(case_id)

    def admit(self, free_slots: int) -> tuple[TriageEntry, ...]:
        """Pop the top ``free_slots`` entries (nothing when ``free_slots <= 0``)."""
        if free_slots <= 0:
            return ()
        top = self.ordered()[:free_slots]
        for e in top:
            self.remove(e.case_id)
        return top


# --- the officer channel ------------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    case_id: str
    contact_id: str
    kind: InterruptionKind
    requested_tick: int
    tick: int
    priority: float


@dataclass(frozen=True)
class _Request:
    seq: int
    case_id: str
    contact_id: str
    kind: InterruptionKind
    tick: int
    priority: float

    @property
    def sort_key(self) -> tuple[float, str, str, int]:
        return (-self.priority, self.contact_id, self.case_id, self.seq)


def _ticks(seconds: float, tick_s: float) -> int:
    return max(1, math.ceil(round(seconds / tick_s, 9)))


@dataclass
class OfficerArbiter:
    """One serial watch-officer channel (DECISIONS 2026-10-17 06:37).

    ``request`` queues; ``grant_due(tick)`` grants pending requests in priority order, one
    each ``officer_review_s``. Mandatory kinds are always granted eventually; an EYES_ON
    request is granted only while fewer than ``optional_interruptions_per_hour`` EYES_ON
    grants fall in the trailing ``optional_window_s``. Nothing is ever discarded.
    """

    cfg: TriageConfig
    tick_s: float
    _pending: list[_Request] = field(default_factory=list)
    _grants: list[Grant] = field(default_factory=list)
    _free_at: int = 0
    _seq: int = 0

    @property
    def review_ticks(self) -> int:
        return _ticks(self.cfg.officer_review_s, self.tick_s)

    @property
    def window_ticks(self) -> int:
        return _ticks(self.cfg.optional_window_s, self.tick_s)

    @property
    def pending(self) -> tuple[Grant, ...]:
        """Pending requests (as would-be grants with ``tick=-1``), in grant order."""
        return tuple(
            Grant(r.case_id, r.contact_id, r.kind, r.tick, -1, r.priority)
            for r in sorted(self._pending, key=lambda r: r.sort_key)
        )

    @property
    def grants(self) -> tuple[Grant, ...]:
        return tuple(self._grants)

    @property
    def free_at(self) -> int:
        return self._free_at

    def request(
        self,
        case_id: str,
        kind: InterruptionKind,
        tick: int,
        priority: float,
        *,
        contact_id: str | None = None,
    ) -> None:
        cid = contact_id if contact_id is not None else case_id.partition("#")[0]
        self._pending.append(
            _Request(self._seq, case_id, cid, InterruptionKind(kind), int(tick), float(priority))
        )
        self._seq += 1

    def _optional_in_window(self, tick: int) -> int:
        lo = tick - self.window_ticks
        return sum(1 for g in self._grants if g.kind in OPTIONAL_KINDS and g.tick > lo)

    def _eligible(self, r: _Request, tick: int) -> bool:
        if r.tick > tick:
            return False
        if r.kind in MANDATORY_KINDS:
            return True
        return self._optional_in_window(tick) < self.cfg.optional_interruptions_per_hour

    def grant_due(self, tick: int) -> tuple[Grant, ...]:
        out: list[Grant] = []
        while self._pending and tick >= self._free_at:
            ready = [
                r
                for r in sorted(self._pending, key=lambda r: r.sort_key)
                if self._eligible(r, tick)
            ]
            if not ready:
                break
            r = ready[0]
            self._pending.remove(r)
            g = Grant(r.case_id, r.contact_id, r.kind, r.tick, int(tick), r.priority)
            self._grants.append(g)
            out.append(g)
            self._free_at = int(tick) + self.review_ticks
        return tuple(out)

    def eyes_on_allowed(self, case_id: str, tick: int) -> bool:
        """What the planner sees: room for one more EYES_ON in the trailing window,
        counting grants already made and EYES_ON requests still pending."""
        del case_id  # the cap is per channel, not per case
        pending = sum(1 for r in self._pending if r.kind in OPTIONAL_KINDS)
        cap = self.cfg.optional_interruptions_per_hour
        return self._optional_in_window(tick) + pending < cap


# --- the log ------------------------------------------------------------------------------


class TriageEvent(StrEnum):
    ENQUEUED = "enqueued"
    REKEYED = "rekeyed"
    ADMITTED = "admitted"
    REMOVED = "removed"
    GRANTED = "granted"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class TriageDecision:
    seq: int
    tick: int
    event: TriageEvent
    case_id: str
    contact_id: str
    priority: float
    rank: int
    evidence_row_ids: tuple[str, ...]
    kind: str | None = None

    def canonical(self) -> str:
        return canonical_json(
            {
                "seq": self.seq,
                "tick": self.tick,
                "event": self.event.value,
                "case_id": self.case_id,
                "contact_id": self.contact_id,
                "priority": round6(self.priority),
                "rank": self.rank,
                "evidence_row_ids": list(self.evidence_row_ids),
                "kind": self.kind,
            }
        )


def triage_log_text(decisions: Sequence[TriageDecision]) -> str:
    return "".join(f"{d.canonical()}\n" for d in sorted(decisions, key=lambda d: d.seq))


def triage_log_hash_of(decisions: Sequence[TriageDecision]) -> str:
    """sha256 of the canonical JSONL of ``decisions``; empty string for an empty log."""
    if not decisions:
        return ""
    return hashlib.sha256(triage_log_text(decisions).encode("utf-8")).hexdigest()


class TriageRecorder:
    """Appends ``TriageDecision`` rows with consecutive ``seq``."""

    def __init__(self) -> None:
        self.decisions: list[TriageDecision] = []

    def record(
        self,
        tick: int,
        event: TriageEvent,
        *,
        case_id: str,
        contact_id: str,
        priority: float,
        rank: int,
        evidence_row_ids: Iterable[str] = (),
        kind: str | None = None,
    ) -> TriageDecision:
        d = TriageDecision(
            seq=len(self.decisions),
            tick=int(tick),
            event=event,
            case_id=case_id,
            contact_id=contact_id,
            priority=round6(priority),
            rank=int(rank),
            evidence_row_ids=tuple(sorted(set(evidence_row_ids))),
            kind=kind,
        )
        self.decisions.append(d)
        return d


__all__ = [
    "MANDATORY_KINDS",
    "OPTIONAL_KINDS",
    "TRIAGE_PATH",
    "Grant",
    "OfficerArbiter",
    "TriageConfig",
    "TriageDecision",
    "TriageEntry",
    "TriageEvent",
    "TriageInputs",
    "TriageQueue",
    "TriageRecorder",
    "load_triage_config",
    "priority",
    "triage_inputs",
    "triage_log_hash_of",
    "triage_log_text",
]
