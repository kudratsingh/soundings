"""The append-only evidence table (SPEC-INVESTIGATION section 2, CLAUDE.md 2.6).

Rows are the frozen WO-010 :class:`~soundings.schemas.evidence.EvidenceRow` models.
A single row already enforces its own rules (no ``None`` outside ``probe_id``, derived
rows name their sources, assumption rows name what they assume). The table enforces
what a single row cannot see:

* every ``derived_from`` id is already in the table, so sources always come first;
* a ``row_id`` is never appended twice;
* a derived row's confidence is at most the minimum confidence of its sources (with a
  ``CONFIDENCE_TOLERANCE`` of 1e-9);
* for each contact, the feature names labeled ``fact`` and those labeled
  ``assumption`` never intersect.

There is no update and no delete. ``carry_over_reacquired`` re-keys an old contact's
rows under a new id by appending derived copies; the old rows stay exactly as they were.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import MappingProxyType
from typing import Any

from soundings.canonical import canonical_json, round6, stable_hash
from soundings.schemas._base import from_file_data
from soundings.schemas.enums import EvidenceKind, FeatureName, Unit
from soundings.schemas.evidence import EvidenceRow

CONFIDENCE_TOLERANCE = 1e-9
REACQUISITION_SOURCE = "reacquisition"

_OPPOSITE = {EvidenceKind.FACT: EvidenceKind.ASSUMPTION, EvidenceKind.ASSUMPTION: EvidenceKind.FACT}


class EvidenceError(ValueError):
    """A row would break a table invariant; the table is left unchanged."""


def _confidence(row: EvidenceRow) -> float:
    """A row's confidence as a number. An ``"unknown"`` confidence counts as 0.0."""
    return row.confidence if isinstance(row.confidence, float) else 0.0


class EvidenceTable:
    """Append-only rows keyed by contact. Read in append order or per contact."""

    def __init__(self) -> None:
        self._rows: list[EvidenceRow] = []
        self._by_id: dict[str, EvidenceRow] = {}
        self._by_contact: dict[str, list[EvidenceRow]] = {}
        self._kinds: dict[str, dict[EvidenceKind, set[FeatureName]]] = {}

    # --- writing ----------------------------------------------------------------------

    def append(self, row: EvidenceRow) -> str:
        """Append one row and return its ``row_id``. Raises ``EvidenceError`` on a violation."""
        if not isinstance(row, EvidenceRow):
            raise EvidenceError("only EvidenceRow instances can be appended")
        row_id = row.row_id
        if row_id in self._by_id:
            raise EvidenceError(f"row {row_id} is already in the table")
        sources: list[EvidenceRow] = []
        for source_id in row.derived_from:
            source = self._by_id.get(source_id)
            if source is None:
                raise EvidenceError(f"row {row_id} derives from {source_id}, not in the table")
            sources.append(source)
        if sources:
            ceiling = min(_confidence(s) for s in sources)
            if _confidence(row) > ceiling + CONFIDENCE_TOLERANCE:
                raise EvidenceError(
                    f"row {row_id} confidence {row.confidence} exceeds its sources' "
                    f"minimum {ceiling}"
                )
        kinds = self._kinds.setdefault(row.contact_id, {})
        other = _OPPOSITE.get(row.kind)
        if other is not None and row.feature in kinds.get(other, set()):
            raise EvidenceError(
                f"{row.contact_id}: {row.feature.value} is already labeled {other.value}; "
                f"it cannot also be {row.kind.value}"
            )
        kinds.setdefault(row.kind, set()).add(row.feature)
        self._rows.append(row)
        self._by_id[row_id] = row
        self._by_contact.setdefault(row.contact_id, []).append(row)
        return row_id

    def extend(self, rows: list[EvidenceRow] | tuple[EvidenceRow, ...]) -> list[str]:
        """Append rows in order; stops at the first violation (earlier rows stay)."""
        return [self.append(row) for row in rows]

    # --- reading ----------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._rows)

    def __iter__(self) -> Iterator[EvidenceRow]:
        return iter(tuple(self._rows))

    def __contains__(self, row_id: object) -> bool:
        return row_id in self._by_id

    def rows(self) -> tuple[EvidenceRow, ...]:
        """Every row in append order."""
        return tuple(self._rows)

    def get(self, row_id: str) -> EvidenceRow:
        """The row with this id (KeyError if absent)."""
        return self._by_id[row_id]

    def index(self) -> MappingProxyType[str, EvidenceRow]:
        """Read-only view of ``row_id -> row``."""
        return MappingProxyType(self._by_id)

    def contacts(self) -> tuple[str, ...]:
        """Contact ids with at least one row, sorted."""
        return tuple(sorted(self._by_contact))

    def for_contact(self, contact_id: str) -> tuple[EvidenceRow, ...]:
        """One contact's rows in append order (empty if none)."""
        return tuple(self._by_contact.get(contact_id, ()))

    rows_for = for_contact

    def feature_rows(self, contact_id: str, feature: FeatureName | str) -> tuple[EvidenceRow, ...]:
        """One contact's rows for one feature, in append order."""
        name = FeatureName(feature)
        return tuple(r for r in self._by_contact.get(contact_id, ()) if r.feature is name)

    def latest(self, contact_id: str, feature: FeatureName | str) -> EvidenceRow | None:
        """The row with the highest tick for (contact, feature); ties go to the later append."""
        best: EvidenceRow | None = None
        for row in self.feature_rows(contact_id, feature):
            if best is None or row.t >= best.t:
                best = row
        return best

    def _features_of_kind(self, contact_id: str, kind: EvidenceKind) -> frozenset[FeatureName]:
        return frozenset(self._kinds.get(contact_id, {}).get(kind, set()))

    def facts(self, contact_id: str) -> frozenset[FeatureName]:
        """Feature names this contact has fact rows for."""
        return self._features_of_kind(contact_id, EvidenceKind.FACT)

    def assumptions(self, contact_id: str) -> frozenset[FeatureName]:
        """Feature names this contact has assumption rows for."""
        return self._features_of_kind(contact_id, EvidenceKind.ASSUMPTION)

    def derived(self, contact_id: str) -> frozenset[FeatureName]:
        """Feature names this contact has derived rows for."""
        return self._features_of_kind(contact_id, EvidenceKind.DERIVED)

    # --- serialization ----------------------------------------------------------------

    def _dump(self) -> list[dict[str, Any]]:
        return [row.model_dump(mode="json") for row in self._rows]

    def to_json(self) -> str:
        """Canonical JSON (sorted keys, 6 dp) of every row in append order, with row_id."""
        return canonical_json({"rows": self._dump()})

    def table_hash(self) -> str:
        """``stable_hash`` of the rows in append order."""
        return stable_hash({"rows": self._dump()})

    @classmethod
    def from_json(cls, text: str) -> EvidenceTable:
        """Rebuild a table from ``to_json`` output; every row and invariant is re-checked."""
        doc = json.loads(text)
        table = cls()
        for raw in doc["rows"]:
            table.append(EvidenceRow.model_validate(from_file_data(raw)))
        return table


def carry_over_reacquired(
    table: EvidenceTable,
    old_id: str,
    new_id: str,
    *,
    t: int,
    match_confidence: float,
    reason: str,
) -> list[str]:
    """Carry ``old_id``'s rows over to a re-acquired contact ``new_id`` (SPEC-INV 9.9).

    First appends one ``reacquired_from`` assumption row for ``new_id`` (value
    ``old_id``, confidence ``match_confidence``, ``assumes`` = ``reason``). Then
    re-appends each of ``old_id``'s rows, in append order, under ``new_id`` as kind
    derived with ``derived_from=(original, assumption)`` and confidence
    ``min(original, match_confidence)``. Feature, value, unit, source and tick are kept;
    ``assumes`` is dropped because only assumption rows carry it (the original is one
    hop away through ``derived_from``). The old rows are not touched. Deciding *which*
    contact is re-acquired is the matcher's job (WO-022), not this function's.

    Returns the new row ids, the assumption first.
    """
    if old_id == new_id:
        raise EvidenceError("a contact cannot be re-acquired from itself")
    if not 0.0 <= match_confidence <= 1.0:
        raise EvidenceError("match_confidence must lie in [0, 1]")
    old_rows = table.for_contact(old_id)
    if not old_rows:
        raise EvidenceError(f"{old_id} has no rows to carry over")
    match = round6(match_confidence)
    assumption = EvidenceRow(
        contact_id=new_id,
        t=t,
        feature=FeatureName.REACQUIRED_FROM,
        value=old_id,
        unit=Unit.ID,
        source=REACQUISITION_SOURCE,
        confidence=match,
        kind=EvidenceKind.ASSUMPTION,
        probe_id=None,
        assumes=reason,
    )
    new_ids = [table.append(assumption)]
    for row in old_rows:
        carried = EvidenceRow(
            contact_id=new_id,
            t=row.t,
            feature=row.feature,
            value=row.value,
            unit=row.unit,
            source=row.source,
            confidence=min(_confidence(row), match),
            kind=EvidenceKind.DERIVED,
            probe_id=row.probe_id,
            derived_from=(row.row_id, assumption.row_id),
        )
        new_ids.append(table.append(carried))
    return new_ids


__all__ = [
    "CONFIDENCE_TOLERANCE",
    "EvidenceError",
    "EvidenceTable",
    "carry_over_reacquired",
]
