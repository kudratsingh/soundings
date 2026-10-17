"""The SITREP template writer (SPEC-LADDER section 6; WO-042 part A).

A SITREP has the fixed section 6 sections, in this order:

1. Contact
2. What we know (facts, each with evidence ids)
3. What we assume
4. What we don't know
5. Hypotheses (posterior, labeled stress-test estimate)
6. Actions already taken (rung, time, standing-order clause)
7. Recommended rung (computed; not editable by the writer)
8. What a person must decide
9. Search box (LOST_VEHICLE only)

Rules (CLAUDE.md 2.2, 2.5, 2.6; SPEC-INVESTIGATION section 8):

* Every line cites at least one evidence row id that exists. The single exception is a
  "What we don't know" line for a feature with no row at all, written ``missing: <feature>``.
* Facts cite only ``fact`` or ``derived`` rows; assumptions cite only ``assumption`` rows and
  quote each row's ``assumes``. Facts and assumptions never share a section.
* "What we don't know" is generated from the assumption rows, the rows whose value is
  ``unknown``, and the section 2 features with no row.
* Every probability is labeled "stress-test estimate".
* ``recommended_rung`` is copied from ``SitrepInputs.recommended_rung`` (the machine's rung);
  no writer output is ever read for it.
* No denylisted word appears anywhere (``validate_sitrep`` checks the rendered text).

``build_sitrep(inputs)`` is deterministic, uses no model, and always passes
``validate_sitrep``. ``SitrepInputs`` is the adapter point: WO-033's recommendation and the
WO-043 driver build one, and WO-042 part B's LLM writer and handoff service consume it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import Field, StrictBool, StrictStr, field_validator

from soundings.canonical import canonical_json, round6
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.posterior import (
    STRESS_TEST_LABEL,
    Posterior,
    PosteriorReport,
)
from soundings.matcher import PositionFix, SearchBox
from soundings.schemas._base import NonNegFloat, StrictModel
from soundings.schemas.denylist import find_denylisted
from soundings.schemas.enums import EvidenceKind, FeatureName, Hypothesis, Rung
from soundings.schemas.evidence import UNKNOWN, EvidenceRow

SITREP_HEADINGS: Final[tuple[str, ...]] = (
    "Contact",
    "What we know (facts, each with evidence ids)",
    "What we assume",
    "What we don't know",
    f"Hypotheses (posterior, labeled {STRESS_TEST_LABEL})",
    "Actions already taken (rung, time, standing-order clause)",
    "Recommended rung (computed; not editable by the writer)",
    "What a person must decide",
    "Search box (LOST_VEHICLE only)",
)
"""The nine SPEC-LADDER section 6 headings in fixed order."""

SEARCH_BOX_HEADING: Final = SITREP_HEADINGS[8]
MISSING_PREFIX: Final = "missing: "
"""Prefix of the only line kind exempt from the citation rule (coordinator addendum)."""

HYPOTHESIS_ROWS_PER_FEATURE: Final = 3
"""A hypothesis line cites the three most recent rows of each feature the posterior used."""

SPEC_FEATURES: Final[tuple[FeatureName, ...]] = tuple(FeatureName)[:32]
"""The 32 SPEC-INVESTIGATION section 2 features (support names excluded)."""

FACT_KINDS: Final = frozenset({EvidenceKind.FACT, EvidenceKind.DERIVED})

RowIdRef = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
ClauseId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]


class SitrepError(ValueError):
    """The inputs cannot make a SITREP (no rows for the contact, bad posterior)."""


class SitrepStatus(StrEnum):
    DRAFT = "draft"
    FINAL = "final"
    UPDATE = "update"


class SitrepWriter(StrEnum):
    TEMPLATE = "template"
    LLM = "llm"


# --- lines and the SITREP -----------------------------------------------------------------


class SitrepLine(StrictModel):
    """One prose line and the evidence rows it rests on."""

    text: StrictStr
    evidence_ids: tuple[RowIdRef, ...] = ()

    @property
    def is_missing(self) -> bool:
        return self.text.startswith(MISSING_PREFIX)


class HypothesisLine(StrictModel):
    """One hypothesis, its posterior probability (6 dp) and the stress-test label."""

    hypothesis: Hypothesis
    p: Annotated[float, Field(ge=0.0, le=1.0)]
    label: Literal["stress-test estimate"] = "stress-test estimate"
    evidence_ids: tuple[RowIdRef, ...] = ()

    @field_validator("p", mode="after")
    @classmethod
    def _six_dp(cls, v: float) -> float:
        return round6(v)


class ActionLine(StrictModel):
    """An action already taken: rung, run-clock time and the standing-order clause."""

    rung: Rung
    t_s: NonNegFloat
    clause: ClauseId
    evidence_ids: tuple[RowIdRef, ...] = ()

    @field_validator("t_s", mode="after")
    @classmethod
    def _six_dp(cls, v: float) -> float:
        return round6(v)


class SearchBoxSection(StrictModel):
    """WO-022's drift search box (no second box type) with its last fix and drift basis."""

    last_known: PositionFix
    box: SearchBox
    drift_basis: StrictStr
    evidence_ids: tuple[RowIdRef, ...] = ()


class Sitrep(StrictModel):
    """A SITREP. ``recommended_rung`` is always the machine's rung, never a writer's."""

    contact_id: StrictStr
    status: SitrepStatus
    contact: tuple[SitrepLine, ...]
    facts: tuple[SitrepLine, ...]
    assumptions: tuple[SitrepLine, ...]
    unknowns: tuple[SitrepLine, ...]
    hypotheses: tuple[HypothesisLine, ...]
    actions_taken: tuple[ActionLine, ...]
    recommended_rung: Rung
    person_must_decide: tuple[SitrepLine, ...]
    search_box: SearchBoxSection | None = None
    writer: SitrepWriter = SitrepWriter.TEMPLATE
    uncertain: StrictBool = False
    approver_unreachable: StrictBool = False

    def to_dict(self) -> dict[str, Any]:
        """Canonical JSON-ready dict (6-dp floats, enums by value) with the headings."""
        data: dict[str, Any] = json.loads(canonical_json(self))
        data["headings"] = list(headings_for(self))
        data["label"] = STRESS_TEST_LABEL
        return data


# --- inputs -------------------------------------------------------------------------------


@dataclass(frozen=True)
class SitrepInputs:
    """Everything a SITREP writer may read. The rung is computed upstream, never here.

    ``evidence`` is the evidence table (all contacts, so carried-over rows a search box
    cites still resolve); only ``contact_id``'s rows fill the sections. ``posterior`` maps
    every hypothesis to its probability; ``posterior_row_ids`` are the rows it used
    (``PosteriorReport.used_row_ids``). ``decision_row_ids`` are the rows that put the
    contact over the handoff threshold (default: the posterior's rows).
    """

    contact_id: str
    evidence: tuple[EvidenceRow, ...]
    posterior: Mapping[Hypothesis, float]
    recommended_rung: Rung
    actions_taken: tuple[ActionLine, ...] = ()
    search_box: SearchBox | None = None
    approver_unreachable: bool = False
    status: SitrepStatus = SitrepStatus.FINAL
    posterior_row_ids: tuple[str, ...] = ()
    decision_row_ids: tuple[str, ...] = ()
    question_for_human: str | None = None
    uncertain: bool = False
    _index: dict[str, EvidenceRow] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        rows = tuple(self.evidence)
        object.__setattr__(self, "evidence", rows)
        object.__setattr__(self, "recommended_rung", Rung(self.recommended_rung))
        object.__setattr__(self, "status", SitrepStatus(self.status))
        try:
            post = Posterior(probs={Hypothesis(k): float(v) for k, v in self.posterior.items()})
        except ValueError as exc:
            raise SitrepError(f"posterior is not a probability vector: {exc}") from exc
        object.__setattr__(self, "posterior", dict(post.probs))
        object.__setattr__(self, "_index", {r.row_id: r for r in rows})
        if not self.contact_rows:
            raise SitrepError(f"no evidence rows for {self.contact_id}")
        for name in ("posterior_row_ids", "decision_row_ids"):
            ids = tuple(getattr(self, name))
            object.__setattr__(self, name, ids)
            missing = [i for i in ids if i not in self._index]
            if missing:
                raise SitrepError(f"{name} cites rows not in the evidence: {missing}")

    @classmethod
    def from_report(
        cls,
        table: EvidenceTable,
        report: PosteriorReport,
        *,
        recommended_rung: Rung,
        **kwargs: Any,
    ) -> SitrepInputs:
        """Inputs from a WO-030 ``PosteriorReport`` over ``table``."""
        return cls(
            contact_id=report.contact_id,
            evidence=table.rows(),
            posterior=dict(report.posterior.probs),
            recommended_rung=recommended_rung,
            posterior_row_ids=tuple(report.used_row_ids),
            **kwargs,
        )

    @property
    def index(self) -> Mapping[str, EvidenceRow]:
        return self._index

    @property
    def contact_rows(self) -> tuple[EvidenceRow, ...]:
        return tuple(r for r in self.evidence if r.contact_id == self.contact_id)

    @property
    def lost_vehicle(self) -> bool:
        return self.search_box is not None


def sitrep_inputs(inputs: SitrepInputs) -> dict[str, Any]:
    """The canonical dict of ``inputs`` (what part B hashes into the fixture key)."""
    data = {
        "contact_id": inputs.contact_id,
        "status": inputs.status.value,
        "recommended_rung": inputs.recommended_rung.value,
        "posterior": {h.value: round6(p) for h, p in inputs.posterior.items()},
        "posterior_row_ids": list(inputs.posterior_row_ids),
        "decision_row_ids": list(inputs.decision_row_ids),
        "evidence": [r.model_dump(mode="json") for r in inputs.contact_rows],
        "actions_taken": [a.model_dump(mode="json") for a in inputs.actions_taken],
        "search_box": None if inputs.search_box is None else inputs.search_box.model_dump(),
        "approver_unreachable": inputs.approver_unreachable,
        "uncertain": inputs.uncertain,
        "question_for_human": inputs.question_for_human,
        "label": STRESS_TEST_LABEL,
    }
    loaded: dict[str, Any] = json.loads(canonical_json(data))
    return loaded


# --- the template writer ------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(round6(value))
    return str(value)


def _latest_per_feature(rows: Iterable[EvidenceRow]) -> dict[FeatureName, EvidenceRow]:
    """Latest row per feature: highest tick, ties to the later append (``EvidenceTable.latest``)."""
    best: dict[FeatureName, EvidenceRow] = {}
    for row in rows:
        cur = best.get(row.feature)
        if cur is None or row.t >= cur.t:
            best[row.feature] = row
    return best


def _feature_order(features: Iterable[FeatureName]) -> list[FeatureName]:
    order = {f: i for i, f in enumerate(FeatureName)}
    return sorted(set(features), key=lambda f: order[f])


def _recent_rows(rows: Sequence[EvidenceRow], feature: FeatureName, n: int) -> list[str]:
    """The ``n`` most recent rows of ``feature`` (tick, then append order), in append order."""
    indexed = [(i, r) for i, r in enumerate(rows) if r.feature is feature]
    picked = sorted(indexed, key=lambda ir: (ir[1].t, ir[0]))[-n:]
    return [r.row_id for _, r in sorted(picked, key=lambda ir: ir[0])]


def _dedupe(ids: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ids))


def _value_text(row: EvidenceRow) -> str:
    unit = "" if row.unit.value in ("unknown", "category", "id", "bool") else f" {row.unit.value}"
    return f"{_fmt(row.value)}{unit}"


def _contact_lines(
    inputs: SitrepInputs, latest: Mapping[FeatureName, EvidenceRow]
) -> list[SitrepLine]:
    rows = inputs.contact_rows
    first, last = min(r.t for r in rows), max(r.t for r in rows)
    parts = [f"Contact {inputs.contact_id}: {len(rows)} evidence rows, ticks {first} to {last}"]
    cite: list[str] = []
    for feature, label in (
        (FeatureName.ZONE_ID, "zone"),
        (FeatureName.NEAREST_ASSET_ID, "nearest asset"),
        (FeatureName.TRANSIT_PATTERN, "transit pattern"),
    ):
        row = latest.get(feature)
        if row is not None and row.kind in FACT_KINDS and row.value != UNKNOWN:
            parts.append(f"{label} {_fmt(row.value)}")
            cite.append(row.row_id)
    if not cite:
        cite.append(max(enumerate(rows), key=lambda ir: (ir[1].t, ir[0]))[1].row_id)
    lines = [SitrepLine(text="; ".join(parts), evidence_ids=tuple(cite))]
    if inputs.uncertain:
        lines.append(
            SitrepLine(
                text="Status: uncertain (the investigation budget ran out before the case settled)",
                evidence_ids=tuple(cite),
            )
        )
    return lines


def _fact_lines(latest: Mapping[FeatureName, EvidenceRow]) -> list[SitrepLine]:
    lines: list[SitrepLine] = []
    for feature in _feature_order(latest):
        row = latest[feature]
        if row.kind not in FACT_KINDS or row.value == UNKNOWN:
            continue
        tag = " (derived)" if row.kind is EvidenceKind.DERIVED else ""
        lines.append(
            SitrepLine(
                text=(
                    f"{feature.value} = {_value_text(row)} at tick {row.t}{tag}; "
                    f"source {row.source}"
                ),
                evidence_ids=(row.row_id,),
            )
        )
    return lines


def _assumption_groups(
    rows: Sequence[EvidenceRow],
) -> list[tuple[FeatureName, str, list[EvidenceRow]]]:
    groups: dict[tuple[FeatureName, str], list[EvidenceRow]] = {}
    for row in rows:
        if row.kind is EvidenceKind.ASSUMPTION:
            groups.setdefault((row.feature, row.assumes), []).append(row)
    order = {f: i for i, f in enumerate(FeatureName)}
    keys = sorted(groups, key=lambda k: (order[k[0]], k[1]))
    return [(f, a, groups[(f, a)]) for f, a in keys]


def _assumption_lines(rows: Sequence[EvidenceRow]) -> list[SitrepLine]:
    lines: list[SitrepLine] = []
    for feature, assumes, group in _assumption_groups(rows):
        latest = max(enumerate(group), key=lambda ir: (ir[1].t, ir[0]))[1]
        lines.append(
            SitrepLine(
                text=f"{feature.value} = {_value_text(latest)}; assumes: {assumes}",
                evidence_ids=tuple(r.row_id for r in group),
            )
        )
    return lines


def _unknown_lines(
    rows: Sequence[EvidenceRow], latest: Mapping[FeatureName, EvidenceRow]
) -> list[SitrepLine]:
    lines: list[SitrepLine] = []
    for feature, assumes, group in _assumption_groups(rows):
        lines.append(
            SitrepLine(
                text=f"{feature.value} is assumed, not observed ({assumes})",
                evidence_ids=tuple(r.row_id for r in group),
            )
        )
    for feature in _feature_order(latest):
        row = latest[feature]
        if row.kind in FACT_KINDS and row.value == UNKNOWN:
            lines.append(
                SitrepLine(
                    text=f"{feature.value} reported unknown at tick {row.t}; source {row.source}",
                    evidence_ids=(row.row_id,),
                )
            )
    present = {r.feature for r in rows}
    for feature in SPEC_FEATURES:
        if feature not in present:
            lines.append(SitrepLine(text=f"{MISSING_PREFIX}{feature.value}"))
    return lines


def _hypothesis_ids(inputs: SitrepInputs) -> tuple[str, ...]:
    rows = inputs.contact_rows
    used = [inputs.index[i] for i in inputs.posterior_row_ids]
    features = _feature_order(r.feature for r in used if r.contact_id == inputs.contact_id)
    ids: list[str] = []
    for feature in features:
        ids.extend(_recent_rows(rows, feature, HYPOTHESIS_ROWS_PER_FEATURE))
    ids.extend(r.row_id for r in used if r.contact_id != inputs.contact_id)
    if not ids:  # prior only: cite what conditioned it, else the latest row
        latest = _latest_per_feature(rows)
        ids = [
            latest[f].row_id
            for f in (FeatureName.HOUR_OF_DAY, FeatureName.ZONE_CRITICALITY)
            if f in latest
        ] or [max(enumerate(rows), key=lambda ir: (ir[1].t, ir[0]))[1].row_id]
    return _dedupe(ids)


def _hypothesis_lines(inputs: SitrepInputs) -> list[HypothesisLine]:
    post = Posterior(probs=dict(inputs.posterior))
    ids = _hypothesis_ids(inputs)
    return [HypothesisLine(hypothesis=h, p=p, evidence_ids=ids) for h, p in post.ranked()]


def _search_box_section(inputs: SitrepInputs) -> SearchBoxSection | None:
    box = inputs.search_box
    if box is None:
        return None
    basis_row = inputs.index.get(box.assumption_row_id)
    if basis_row is None:
        raise SitrepError(f"search box drift basis {box.assumption_row_id} is not in the evidence")
    basis = basis_row.assumes or f"{basis_row.feature.value} row {basis_row.row_id}"
    ids = [box.assumption_row_id]
    if box.anchor.row_id is not None and box.anchor.row_id in inputs.index:
        ids.insert(0, box.anchor.row_id)
    return SearchBoxSection(
        last_known=box.anchor, box=box, drift_basis=basis, evidence_ids=_dedupe(ids)
    )


def _decision_lines(inputs: SitrepInputs, contact: Sequence[SitrepLine]) -> list[SitrepLine]:
    ids = inputs.decision_row_ids or inputs.posterior_row_ids or contact[0].evidence_ids
    cite = _dedupe(ids)
    rung = inputs.recommended_rung
    lines: list[SitrepLine] = []
    if inputs.lost_vehicle and inputs.search_box is not None:
        box_ids = _dedupe([*cite, inputs.search_box.assumption_row_id])
        lines.append(
            SitrepLine(
                text=(
                    "The vehicle's operator decides whether and how to recover it inside "
                    "the search box; the platform keeps listening"
                ),
                evidence_ids=box_ids,
            )
        )
    elif inputs.status is SitrepStatus.DRAFT:
        lines.append(
            SitrepLine(
                text=f"Approve or deny the requested action for {inputs.contact_id}; "
                "this draft is what the approver sees",
                evidence_ids=cite,
            )
        )
    elif rung is Rung.HAND_TO_PERSON:
        lines.append(
            SitrepLine(
                text=f"The watch officer decides what happens next with {inputs.contact_id}; "
                "the platform has stopped at the handoff",
                evidence_ids=cite,
            )
        )
    else:
        lines.append(
            SitrepLine(
                text=f"Confirm or change the {rung.value} rung for {inputs.contact_id}",
                evidence_ids=cite,
            )
        )
    if inputs.question_for_human:
        lines.append(SitrepLine(text=inputs.question_for_human, evidence_ids=cite))
    if inputs.uncertain:
        lines.append(
            SitrepLine(
                text="Decide whether more looking is warranted: the case is still uncertain",
                evidence_ids=cite,
            )
        )
    if inputs.approver_unreachable:
        lines.append(
            SitrepLine(
                text="The primary approver was unreachable; the backup approver must respond",
                evidence_ids=cite,
            )
        )
    return lines


def build_sitrep(inputs: SitrepInputs) -> Sitrep:
    """The template SITREP: deterministic, no model, always valid for ``inputs``."""
    rows = inputs.contact_rows
    latest = _latest_per_feature(rows)
    contact = _contact_lines(inputs, latest)
    return Sitrep(
        contact_id=inputs.contact_id,
        status=inputs.status,
        contact=tuple(contact),
        facts=tuple(_fact_lines(latest)),
        assumptions=tuple(_assumption_lines(rows)),
        unknowns=tuple(_unknown_lines(rows, latest)),
        hypotheses=tuple(_hypothesis_lines(inputs)),
        actions_taken=inputs.actions_taken,
        recommended_rung=inputs.recommended_rung,
        person_must_decide=tuple(_decision_lines(inputs, contact)),
        search_box=_search_box_section(inputs),
        writer=SitrepWriter.TEMPLATE,
        uncertain=inputs.uncertain,
        approver_unreachable=inputs.approver_unreachable,
    )


# --- rendering ----------------------------------------------------------------------------


def headings_for(sitrep: Sitrep) -> tuple[str, ...]:
    """The headings ``render_markdown`` prints: all nine for LOST_VEHICLE, else the first eight."""
    return SITREP_HEADINGS if sitrep.search_box is not None else SITREP_HEADINGS[:8]


def _cite(ids: Sequence[str]) -> str:
    return f" [evidence: {', '.join(ids)}]" if ids else ""


def _bullets(lines: Sequence[SitrepLine]) -> list[str]:
    if not lines:
        return ["- none"]
    return [f"- {ln.text}{_cite(ln.evidence_ids)}" for ln in lines]


def render_markdown(sitrep: Sitrep) -> str:
    """Markdown with the section 6 headings in fixed order; ends with a newline."""
    out = [f"# SITREP {sitrep.contact_id} ({sitrep.status.value}, writer {sitrep.writer.value})"]
    sections: list[list[str]] = [
        _bullets(sitrep.contact),
        _bullets(sitrep.facts),
        _bullets(sitrep.assumptions),
        _bullets(sitrep.unknowns),
        [
            f"- {h.hypothesis.value}: {_fmt(h.p)} ({h.label}){_cite(h.evidence_ids)}"
            for h in sitrep.hypotheses
        ]
        or ["- none"],
        [
            f"- rung {a.rung.value} at t={_fmt(a.t_s)} s under standing-order clause "
            f"{a.clause}{_cite(a.evidence_ids)}"
            for a in sitrep.actions_taken
        ]
        or ["- none"],
        [f"- {sitrep.recommended_rung.value} (computed by the ladder; not editable)"],
        _bullets(sitrep.person_must_decide),
    ]
    sb = sitrep.search_box
    if sb is not None:
        b = sb.box
        sections.append(
            [
                f"- last known {_fmt(sb.last_known.lat)}, {_fmt(sb.last_known.lon)} at tick "
                f"{sb.last_known.tick} (sigma {_fmt(sb.last_known.sigma_m)} m)"
                f"{_cite(sb.evidence_ids)}",
                f"- centre {_fmt(b.center_lat)}, {_fmt(b.center_lon)}; radius "
                f"{_fmt(b.radius_m)} m at tick {b.to_tick}",
                f"- bounds lat {_fmt(b.min_lat)} to {_fmt(b.max_lat)}, lon {_fmt(b.min_lon)} "
                f"to {_fmt(b.max_lon)}",
                f"- drift basis (assumption): {sb.drift_basis}",
            ]
        )
    for heading, body in zip(headings_for(sitrep), sections, strict=True):
        out.append("")
        out.append(f"## {heading}")
        out.extend(body)
    return "\n".join(out) + "\n"


# --- validation ---------------------------------------------------------------------------


class SitrepIssueCode(StrEnum):
    CITATION = "citation"
    SECTIONS = "sections"
    LABEL = "label"
    RECOMMENDED_RUNG_MISMATCH = "recommended_rung_mismatch"
    SEARCH_BOX = "search_box"
    DENYLIST = "denylist"


@dataclass(frozen=True)
class SitrepIssue:
    code: SitrepIssueCode
    detail: str


def _check_ids(
    where: str,
    line_ids: Sequence[str],
    index: Mapping[str, EvidenceRow],
    kinds: frozenset[EvidenceKind] | None,
    issues: list[SitrepIssue],
) -> None:
    if not line_ids:
        issues.append(SitrepIssue(SitrepIssueCode.CITATION, f"{where}: no evidence id"))
        return
    for row_id in line_ids:
        row = index.get(row_id)
        if row is None:
            issues.append(SitrepIssue(SitrepIssueCode.CITATION, f"{where}: {row_id} not in table"))
        elif kinds is not None and row.kind not in kinds:
            issues.append(
                SitrepIssue(
                    SitrepIssueCode.CITATION,
                    f"{where}: {row_id} is a {row.kind.value} row",
                )
            )


def validate_sitrep(sitrep: Sitrep, inputs: SitrepInputs) -> list[SitrepIssue]:
    """Every rule a delivered SITREP must meet; an empty list means valid."""
    issues: list[SitrepIssue] = []
    index = inputs.index
    if sitrep.contact_id != inputs.contact_id:
        issues.append(SitrepIssue(SitrepIssueCode.SECTIONS, "contact id differs from the inputs"))
    if sitrep.recommended_rung is not inputs.recommended_rung:
        issues.append(
            SitrepIssue(
                SitrepIssueCode.RECOMMENDED_RUNG_MISMATCH,
                f"{sitrep.recommended_rung.value} != computed {inputs.recommended_rung.value}",
            )
        )
    for i, ln in enumerate(sitrep.contact):
        _check_ids(f"contact[{i}]", ln.evidence_ids, index, None, issues)
    for i, ln in enumerate(sitrep.facts):
        _check_ids(f"facts[{i}]", ln.evidence_ids, index, FACT_KINDS, issues)
    for i, ln in enumerate(sitrep.assumptions):
        _check_ids(
            f"assumptions[{i}]",
            ln.evidence_ids,
            index,
            frozenset({EvidenceKind.ASSUMPTION}),
            issues,
        )
    present = {r.feature.value for r in inputs.contact_rows}
    for i, ln in enumerate(sitrep.unknowns):
        if ln.is_missing:
            name = ln.text[len(MISSING_PREFIX) :]
            if ln.evidence_ids or name not in {f.value for f in FeatureName} or name in present:
                issues.append(
                    SitrepIssue(
                        SitrepIssueCode.CITATION,
                        f"unknowns[{i}]: '{ln.text}' must name a feature with no row, uncited",
                    )
                )
            continue
        _check_ids(f"unknowns[{i}]", ln.evidence_ids, index, None, issues)
    for i, h in enumerate(sitrep.hypotheses):
        _check_ids(f"hypotheses[{i}]", h.evidence_ids, index, None, issues)
        if h.label != STRESS_TEST_LABEL:
            issues.append(SitrepIssue(SitrepIssueCode.LABEL, f"hypotheses[{i}] unlabeled"))
    for i, a in enumerate(sitrep.actions_taken):
        _check_ids(f"actions_taken[{i}]", a.evidence_ids, index, None, issues)
    for i, ln in enumerate(sitrep.person_must_decide):
        _check_ids(f"person_must_decide[{i}]", ln.evidence_ids, index, None, issues)
    fact_text = {ln.text for ln in sitrep.facts}
    shared = fact_text & {ln.text for ln in sitrep.assumptions}
    if shared:
        issues.append(
            SitrepIssue(
                SitrepIssueCode.SECTIONS, f"lines in both facts and assumptions: {sorted(shared)}"
            )
        )
    if inputs.search_box is None:
        if sitrep.search_box is not None:
            issues.append(
                SitrepIssue(SitrepIssueCode.SEARCH_BOX, "search box outside LOST_VEHICLE")
            )
    elif sitrep.search_box is None or sitrep.search_box.box != inputs.search_box:
        issues.append(
            SitrepIssue(SitrepIssueCode.SEARCH_BOX, "LOST_VEHICLE needs the computed box")
        )
    else:
        _check_ids("search_box", sitrep.search_box.evidence_ids, index, None, issues)
    markdown = render_markdown(sitrep)
    if STRESS_TEST_LABEL not in markdown:
        issues.append(
            SitrepIssue(SitrepIssueCode.LABEL, "rendered text lacks the stress-test label")
        )
    if find_denylisted(markdown):
        issues.append(SitrepIssue(SitrepIssueCode.DENYLIST, "denylisted vocabulary in the SITREP"))
    return issues


def sitrep_json(sitrep: Sitrep) -> dict[str, Any]:
    """The JSON form: ``Sitrep.to_dict()`` plus the rendered markdown."""
    return {**sitrep.to_dict(), "markdown": render_markdown(sitrep)}


__all__ = [
    "FACT_KINDS",
    "HYPOTHESIS_ROWS_PER_FEATURE",
    "MISSING_PREFIX",
    "SEARCH_BOX_HEADING",
    "SITREP_HEADINGS",
    "SPEC_FEATURES",
    "ActionLine",
    "HypothesisLine",
    "SearchBoxSection",
    "Sitrep",
    "SitrepError",
    "SitrepInputs",
    "SitrepIssue",
    "SitrepIssueCode",
    "SitrepLine",
    "SitrepStatus",
    "SitrepWriter",
    "build_sitrep",
    "headings_for",
    "render_markdown",
    "sitrep_inputs",
    "sitrep_json",
    "validate_sitrep",
]
