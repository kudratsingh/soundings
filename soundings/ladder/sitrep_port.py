"""The SITREP port the ladder driver renders through, and a template stub behind it.

WO-043 part A. The real writer is WO-042's (``soundings/ladder/sitrep.py``, built
concurrently); until it is wired (part B), the driver renders every SITREP with
``StubSitrepWriter``: a deterministic template over ``SitrepInputs`` that makes no model
call. ``SitrepInputs`` holds ids, enums and numbers only, so nothing a model writes can
reach it (CLAUDE.md 2.2), and every SITREP cites the evidence row ids it was built from
(CLAUDE.md 2.6).
"""

from __future__ import annotations

from typing import Literal, Protocol

from soundings.canonical import round6, stable_hash
from soundings.schemas import LadderState, Rung
from soundings.schemas._base import HarborId, NonNegInt, Probability, StrictModel

from .types import ActionScope

SitrepKind = Literal["draft", "final", "update"]
"""``draft`` goes with an approval request, ``final`` with the handoff, ``update`` after it."""

UNREACHABLE = "unreachable"
"""The SITREP flag for a handoff after no approver answered (SPEC-LADDER section 3)."""


class LastKnown(StrictModel):
    """The latest observed position of a contact and whether it lay inside a harbor zone."""

    tick: NonNegInt
    lat: float
    lon: float
    zone_id: HarborId | None = None


class SitrepInputs(StrictModel):
    """Everything a SITREP may say. No free text: ids, enums, numbers and row ids."""

    kind: SitrepKind
    contact_id: str
    tick: NonNegInt
    now_s: float
    state: LadderState
    rung: Rung
    recommended_rung: Rung | None = None
    threat_mass: Probability | None = None
    benign_mass: Probability | None = None
    basin: HarborId | None = None
    nearest_asset_id: HarborId | None = None
    time_to_nearest_critical_asset_s: float | None = None
    requested_scope: ActionScope | None = None
    cited_row_ids: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    last_known: LastKnown | None = None
    track_lost_at_tick: NonNegInt | None = None


class SitrepWriterLike(Protocol):
    """Renders one SITREP. WO-042's writer plugs in here in part B."""

    def render(self, inputs: SitrepInputs) -> str: ...


class RenderedSitrep(StrictModel):
    """One rendered SITREP, its inputs, and ``digest`` (sha256 of the text)."""

    kind: SitrepKind
    contact_id: str
    tick: NonNegInt
    inputs: SitrepInputs
    text: str
    digest: str

    @classmethod
    def build(cls, inputs: SitrepInputs, text: str) -> RenderedSitrep:
        return cls(
            kind=inputs.kind,
            contact_id=inputs.contact_id,
            tick=inputs.tick,
            inputs=inputs,
            text=text,
            digest=stable_hash(text),
        )


def _num(value: float | None, unit: str = "") -> str:
    return "unknown" if value is None else f"{round6(value)}{unit}"


class StubSitrepWriter:
    """A template SITREP (no model call). Labelled as the stub so nobody mistakes it."""

    def render(self, inputs: SitrepInputs) -> str:
        i = inputs
        title = {"draft": "SITREP draft", "final": "SITREP", "update": "SITREP update"}[i.kind]
        lines = [
            f"# {title}: {i.contact_id}",
            "",
            f"- run clock: tick {i.tick} ({_num(i.now_s, ' s')})",
            f"- ladder state: {i.state.value}; rung: {i.rung.value}",
            "- recommended rung: "
            + ("unknown" if i.recommended_rung is None else i.recommended_rung.value),
            f"- threat mass: {_num(i.threat_mass)}; benign mass: {_num(i.benign_mass)}",
            f"- basin: {i.basin or 'unknown'}; nearest asset: {i.nearest_asset_id or 'unknown'}",
            f"- time to nearest critical asset: {_num(i.time_to_nearest_critical_asset_s, ' s')}",
        ]
        if i.requested_scope is not None:
            s = i.requested_scope
            lines.append(
                f"- approval requested: {s.action} (asset {s.asset_id or 'none'}, "
                f"basin {s.basin or 'none'}, gates {s.gates}, "
                f"max minutes {s.max_minutes if s.max_minutes is not None else 'none'})"
            )
        if i.flags:
            lines.append(f"- flags: {', '.join(i.flags)}")
        if i.last_known is not None:
            where = i.last_known.zone_id or "outside every harbor zone"
            lines.append(f"- last known position: tick {i.last_known.tick}, {where}")
        if i.track_lost_at_tick is not None and i.last_known is not None:
            inside = "inside zone" if i.last_known.zone_id is not None else "outside every zone"
            lines.append(f"- track lost at tick {i.track_lost_at_tick}, last known {inside}")
        lines.append("- evidence rows cited: " + (", ".join(i.cited_row_ids) or "none"))
        lines.append("")
        lines.append("_Template SITREP (stub writer; the WO-042 writer replaces it)._")
        return "\n".join(lines) + "\n"


__all__ = [
    "UNREACHABLE",
    "LastKnown",
    "RenderedSitrep",
    "SitrepInputs",
    "SitrepKind",
    "SitrepWriterLike",
    "StubSitrepWriter",
]
