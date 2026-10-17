"""The evidence row: the nine fixed SPEC-INVESTIGATION section 2 columns plus provenance.

CLAUDE.md 2.6: facts, assumptions and derived rows are distinct kinds. An assumption
names what it assumes (``assumes``); a derived row names the rows it rests on
(``derived_from``); a fact carries neither. ``None`` is never a value, unit or
confidence: a missing value is the explicit ``"unknown"`` sentinel. Only ``probe_id``
is nullable, because the spec types it ``probe_id | null``.
"""

from __future__ import annotations

from functools import cached_property
from typing import Annotated, Any, Literal

from pydantic import (
    ModelWrapValidatorHandler,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationInfo,
    computed_field,
    model_validator,
)

from soundings.canonical import stable_hash
from soundings.schemas._base import NonNegInt, Probability, StrictModel, from_file_data
from soundings.schemas.denylist import FreeText
from soundings.schemas.enums import EvidenceKind, FeatureName, ProbeId, Unit

Unknown = Literal["unknown"]
UNKNOWN: Unknown = "unknown"

EvidenceValue = StrictBool | StrictInt | StrictFloat | StrictStr | Unknown

ContactId = Annotated[str, StringConstraints(pattern=r"^c-\d{4,6}$")]
"""Observed contact ids, for example ``c-0001``."""

Source = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.:-]+$")]
"""Where a row came from, for example ``sim.detector:hyd_1`` or ``probe:acoustic_hail``."""

RowId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
"""The first 16 hex characters of a row's content hash."""

ROW_ID_LEN = 16


class EvidenceRow(StrictModel):
    """One row per (contact, feature, source, tick). Immutable; ``row_id`` is its content hash."""

    contact_id: ContactId
    t: NonNegInt
    feature: FeatureName
    value: EvidenceValue
    unit: Unit
    source: Source
    confidence: Probability | Unknown
    kind: EvidenceKind
    probe_id: ProbeId | None
    derived_from: tuple[RowId, ...] = ()
    assumes: FreeText = ""

    @model_validator(mode="wrap")
    @classmethod
    def _accept_matching_row_id(
        cls, data: Any, handler: ModelWrapValidatorHandler[EvidenceRow], info: ValidationInfo
    ) -> EvidenceRow:
        """A serialized row carries ``row_id``; accept it only if it matches the content."""
        given: Any = None
        if isinstance(data, dict) and "row_id" in data:
            given = data["row_id"]
            data = {k: v for k, v in data.items() if k != "row_id"}
            if info.mode == "json":  # JSON arrays arrive as lists once re-entered as a dict
                data = from_file_data(data)
        row = handler(data)
        if given is not None and given != row.row_id:
            raise ValueError("row_id does not match the row's content")
        return row

    @model_validator(mode="after")
    def _kind_rules(self) -> EvidenceRow:
        if self.kind is EvidenceKind.DERIVED and not self.derived_from:
            raise ValueError("a derived row must name the rows it derives from (derived_from)")
        if self.kind is not EvidenceKind.DERIVED and self.derived_from:
            raise ValueError("only a derived row may carry derived_from")
        if self.kind is EvidenceKind.ASSUMPTION and not self.assumes.strip():
            raise ValueError("an assumption row must name what it assumes (assumes)")
        if self.kind is not EvidenceKind.ASSUMPTION and self.assumes:
            raise ValueError("only an assumption row may carry assumes")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def row_id(self) -> str:
        payload = self.model_dump(mode="json", exclude={"row_id"})
        return stable_hash(payload)[:ROW_ID_LEN]
