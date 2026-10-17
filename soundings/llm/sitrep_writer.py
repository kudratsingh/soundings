"""The ``sitrep_writer`` role behind a guard (WO-042 part B; CLAUDE.md 2.2, 2.3, 2.6, section 5).

The model writes prose only: a contact summary, fact, assumption and unknown lines (each
with the evidence row ids it rests on) and what a person must decide. Everything else in
the SITREP is computed: the hypotheses (labeled "stress-test estimate"), the actions
already taken, the search box and, above all, the recommended rung, which is copied from
``SitrepInputs.recommended_rung`` and never read from the reply.

``guard_sitrep(raw, inputs)`` rejects, in this order:

* ``recommended_rung_mismatch``: the reply names a ``recommended_rung`` other than the
  computed one (the attempt is logged even when the rest of the reply is malformed);
* ``schema``: not the strict ``SitrepWriterOutput`` shape, or an extra key such as
  ``approve``, ``next_state``, ``tool`` or ``rung_override``;
* ``denylist``: any denylisted word in the reply;
* ``probability_in_prose``: a percentage or probability wording in prose (numbers belong
  to the computed, labeled Hypotheses section);
* ``citation``: a fact, assumption or unknown line with no id, an id not in the table, a
  fact citing an assumption row or an assumption citing a fact row; a ``missing:`` line in
  "What we don't know" must cite nothing and name a feature with no row;
* ``sections``: any other ``validate_sitrep`` issue on the assembled SITREP.

On a rejection one ``ViolationRow`` is appended to the violations file (a parameter) and
the template SITREP (``template_sitrep``) is delivered. On a replay miss the template is
delivered and no violation is written. The role is called at most once per handoff and
never retried; drafts and updates are template only (``soundings.ladder.handoff``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import ConfigDict, StrictStr, ValidationError

from soundings.canonical import canonical_json
from soundings.investigation.posterior import STRESS_TEST_LABEL
from soundings.ladder.sitrep import (
    FACT_KINDS,
    MISSING_PREFIX,
    SPEC_FEATURES,
    Sitrep,
    SitrepInputs,
    SitrepIssueCode,
    SitrepLine,
    SitrepWriter,
    build_sitrep,
    sitrep_inputs,
    validate_sitrep,
)
from soundings.llm.client import ModelClient, ModelMode, fixture_key
from soundings.llm.guard import (
    DEFAULT_VIOLATIONS_PATH,
    ViolationCode,
    ViolationRow,
    append_violation,
)
from soundings.schemas._base import StrictModel
from soundings.schemas.denylist import find_denylisted
from soundings.schemas.enums import EvidenceKind, Rung
from soundings.schemas.evidence import EvidenceRow

ROLE_SITREP_WRITER: Final = "sitrep_writer"
SITREP_PROMPT_VERSION: Final = "s1.0"
FALLBACK_TEMPLATE_SITREP: Final = "template_sitrep"

SITREP_WRITER_PROMPT: Final = f"""\
You write the prose of a harbor SITREP for a watch officer. Reply with one JSON object and
nothing else, with exactly these keys:
  contact_summary: one sentence about the contact;
  facts: [{{text, evidence_ids}}], each citing at least one fact or derived row id;
  assumptions: [{{text, evidence_ids}}], each citing at least one assumption row id;
  unknowns: [{{text, evidence_ids}}], each citing at least one row id, except a line
    "{MISSING_PREFIX}<feature>" for a feature listed in missing_features, which cites none;
  person_must_decide: [text], the decisions left to the person.
You may echo recommended_rung, but only exactly as given: the rung is computed by the
ladder and you cannot change it. Do not add keys. Do not write probabilities or
percentages; the posterior is printed separately and labeled "{STRESS_TEST_LABEL}".
Cite only row ids from the evidence list. Facts and assumptions stay in separate sections.
Describe what is known and what a person must decide; propose nothing beyond the handoff.
Prompt version {SITREP_PROMPT_VERSION}.
"""


class SitrepViolation(StrEnum):
    """Why a ``sitrep_writer`` reply was rejected."""

    SCHEMA = "schema"
    RECOMMENDED_RUNG_MISMATCH = "recommended_rung_mismatch"
    CITATION = "citation"
    PROBABILITY_IN_PROSE = "probability_in_prose"
    DENYLIST = "denylist"
    SECTIONS = "sections"


VIOLATION_CODE: Final[dict[SitrepViolation, ViolationCode]] = {
    SitrepViolation.SCHEMA: ViolationCode.SCHEMA_INVALID,
    SitrepViolation.RECOMMENDED_RUNG_MISMATCH: ViolationCode.NAMES_RUNG,
    SitrepViolation.CITATION: ViolationCode.SCHEMA_INVALID,
    SitrepViolation.PROBABILITY_IN_PROSE: ViolationCode.SCHEMA_INVALID,
    SitrepViolation.DENYLIST: ViolationCode.DENYLISTED_TEXT,
    SitrepViolation.SECTIONS: ViolationCode.SCHEMA_INVALID,
}
"""WO-032's ``ViolationRow`` takes a ``ViolationCode``; the SITREP reason leads the detail."""

PROBABILITY_IN_PROSE: Final = re.compile(
    r"\d\s*%|\bper\s?cent\b|\bprobab\w*|\blikelihood\b|\bodds\b|\bchance\b", re.IGNORECASE
)


class SitrepPath(StrEnum):
    """Which path produced a delivered SITREP (written to its audit row)."""

    TEMPLATE = "template"
    FIXTURE_REPLAY = "fixture-replay"
    FALLBACK_AFTER_REJECTION = "fallback-after-rejection"
    LIVE = "live"
    """Reserved for WO-087's bounded live mode; nothing in this WO produces it."""


# --- output schema ------------------------------------------------------------------------


class _Strict(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WriterLine(_Strict):
    text: StrictStr
    evidence_ids: list[StrictStr] = []


class SitrepWriterOutput(_Strict):
    """The strict reply shape. Extra keys are refused (``schema``)."""

    contact_summary: StrictStr
    facts: list[WriterLine]
    assumptions: list[WriterLine]
    unknowns: list[WriterLine]
    person_must_decide: list[StrictStr]
    recommended_rung: Rung | None = None


@dataclass(frozen=True)
class SitrepGuardResult:
    """An accepted ``Sitrep`` (``writer=llm``), or the violation that rejected the reply."""

    accepted: bool
    value: Sitrep | None
    violation: SitrepViolation | None
    detail: str

    @classmethod
    def accept(cls, value: Sitrep) -> SitrepGuardResult:
        return cls(True, value, None, "")

    @classmethod
    def reject(cls, code: SitrepViolation, detail: str) -> SitrepGuardResult:
        return cls(False, None, code, detail)


# --- prompt -------------------------------------------------------------------------------


def _row_view(row: EvidenceRow) -> dict[str, Any]:
    return {
        "row_id": row.row_id,
        "t": row.t,
        "feature": row.feature.value,
        "value": row.value,
        "unit": row.unit.value,
        "kind": row.kind.value,
        "source": row.source,
        "assumes": row.assumes,
    }


def missing_features(inputs: SitrepInputs) -> list[str]:
    """The section 2 features with no row for the contact (the only uncited lines)."""
    present = {r.feature for r in inputs.contact_rows}
    return [f.value for f in SPEC_FEATURES if f not in present]


def prompt_rows(inputs: SitrepInputs) -> list[EvidenceRow]:
    """The rows the prompt shows: the latest per feature, every assumption row, and every
    row the posterior, the decision, the actions or the search box cite (table order)."""
    rows = inputs.contact_rows
    latest: dict[Any, EvidenceRow] = {}
    for r in rows:
        cur = latest.get(r.feature)
        if cur is None or r.t >= cur.t:
            latest[r.feature] = r
    wanted = {r.row_id for r in latest.values()}
    wanted |= {r.row_id for r in rows if r.kind is EvidenceKind.ASSUMPTION}
    wanted |= set(inputs.posterior_row_ids) | set(inputs.decision_row_ids)
    for a in inputs.actions_taken:
        wanted |= set(a.evidence_ids)
    if inputs.search_box is not None:
        wanted.add(inputs.search_box.assumption_row_id)
    return [r for r in rows if r.row_id in wanted]


def sitrep_writer_prompt(inputs: SitrepInputs) -> dict[str, Any]:
    """The canonical model inputs (hashed into the fixture key).

    Evidence rows with ids (``prompt_rows``), the posterior labeled "stress-test
    estimate", the computed recommended rung, the actions taken, the search box, the
    flags and the missing features (the "What we don't know" lines without a row).
    """
    full = sitrep_inputs(inputs)
    data = {k: v for k, v in full.items() if k != "evidence"}
    data.update(
        {
            "prompt_version": SITREP_PROMPT_VERSION,
            "role": ROLE_SITREP_WRITER,
            "evidence_rows": [_row_view(r) for r in prompt_rows(inputs)],
            "evidence_row_count": len(inputs.contact_rows),
            "missing_features": missing_features(inputs),
        }
    )
    loaded: dict[str, Any] = json.loads(canonical_json(data))
    return loaded


def template_sitrep(inputs: SitrepInputs) -> Sitrep:
    """The deterministic template SITREP (no model); always passes ``validate_sitrep``."""
    return build_sitrep(inputs)


# --- guard --------------------------------------------------------------------------------


def _texts(out: SitrepWriterOutput) -> list[str]:
    texts = [out.contact_summary, *out.person_must_decide]
    for section in (out.facts, out.assumptions, out.unknowns):
        texts.extend(ln.text for ln in section)
    return texts


def _raw_strings(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, dict):
        return [s for k, v in raw.items() for s in (*_raw_strings(k), *_raw_strings(v))]
    if isinstance(raw, list | tuple):
        return [s for v in raw for s in _raw_strings(v)]
    return []


def _check_cites(
    where: str,
    lines: Sequence[WriterLine],
    index: dict[str, EvidenceRow],
    kinds: frozenset[EvidenceKind] | None,
    *,
    allow_missing: bool = False,
    missing_ok: frozenset[str] = frozenset(),
) -> str | None:
    for i, ln in enumerate(lines):
        if allow_missing and ln.text.startswith(MISSING_PREFIX):
            name = ln.text[len(MISSING_PREFIX) :].strip()
            if ln.evidence_ids or name not in missing_ok:
                return f"{where}[{i}]: a missing: line must name a feature with no row, uncited"
            continue
        if not ln.evidence_ids:
            return f"{where}[{i}]: no evidence id"
        for row_id in ln.evidence_ids:
            row = index.get(row_id)
            if row is None:
                return f"{where}[{i}]: {row_id} is not in the evidence table"
            if kinds is not None and row.kind not in kinds:
                return f"{where}[{i}]: {row_id} is a {row.kind.value} row"
    return None


def _issue_violation(code: SitrepIssueCode) -> SitrepViolation:
    return {
        SitrepIssueCode.CITATION: SitrepViolation.CITATION,
        SitrepIssueCode.RECOMMENDED_RUNG_MISMATCH: SitrepViolation.RECOMMENDED_RUNG_MISMATCH,
        SitrepIssueCode.DENYLIST: SitrepViolation.DENYLIST,
    }.get(code, SitrepViolation.SECTIONS)


def guard_sitrep(raw: Any, inputs: SitrepInputs) -> SitrepGuardResult:
    """Check one reply against ``inputs``; on success the SITREP with ``writer=llm``."""
    computed = inputs.recommended_rung
    if isinstance(raw, dict) and "recommended_rung" in raw:
        claimed = raw["recommended_rung"]
        if claimed is not None and claimed != computed.value:
            return SitrepGuardResult.reject(
                SitrepViolation.RECOMMENDED_RUNG_MISMATCH,
                f"reply names {claimed!r}; the computed rung is {computed.value}",
            )
    try:
        out = SitrepWriterOutput.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return SitrepGuardResult.reject(SitrepViolation.SCHEMA, f"{loc}: {first.get('type')}")
    hits = sorted({w.lower() for s in _raw_strings(raw) for w in find_denylisted(s)})
    if hits:
        return SitrepGuardResult.reject(SitrepViolation.DENYLIST, f"{len(hits)} denylisted term(s)")
    for text in _texts(out):
        m = PROBABILITY_IN_PROSE.search(text)
        if m:
            return SitrepGuardResult.reject(
                SitrepViolation.PROBABILITY_IN_PROSE, f"prose names {m.group(0)!r}"
            )
    index = dict(inputs.index)
    contact_index = {k: v for k, v in index.items() if v.contact_id == inputs.contact_id}
    missing = frozenset(missing_features(inputs))
    for where, lines, kinds in (
        ("facts", out.facts, FACT_KINDS),
        ("assumptions", out.assumptions, frozenset({EvidenceKind.ASSUMPTION})),
    ):
        err = _check_cites(where, lines, contact_index, kinds)
        if err:
            return SitrepGuardResult.reject(SitrepViolation.CITATION, err)
    err = _check_cites(
        "unknowns", out.unknowns, contact_index, None, allow_missing=True, missing_ok=missing
    )
    if err:
        return SitrepGuardResult.reject(SitrepViolation.CITATION, err)

    template = build_sitrep(inputs)
    contact_cite = template.contact[0].evidence_ids
    decide_cite = template.person_must_decide[0].evidence_ids
    llm_decide = tuple(SitrepLine(text=t, evidence_ids=decide_cite) for t in out.person_must_decide)
    sitrep = template.model_copy(
        update={
            "contact": (SitrepLine(text=out.contact_summary, evidence_ids=contact_cite),)
            + template.contact[1:],
            "facts": tuple(
                SitrepLine(text=ln.text, evidence_ids=tuple(ln.evidence_ids)) for ln in out.facts
            ),
            "assumptions": tuple(
                SitrepLine(text=ln.text, evidence_ids=tuple(ln.evidence_ids))
                for ln in out.assumptions
            ),
            "unknowns": tuple(
                SitrepLine(text=ln.text, evidence_ids=tuple(ln.evidence_ids)) for ln in out.unknowns
            ),
            # The computed decision lines (flags, unreachable approver) stay; prose is added.
            "person_must_decide": template.person_must_decide + llm_decide,
            "recommended_rung": computed,
            "writer": SitrepWriter.LLM,
        }
    )
    sitrep = Sitrep.model_validate(sitrep.model_dump())
    issues = validate_sitrep(sitrep, inputs)
    if issues:
        return SitrepGuardResult.reject(_issue_violation(issues[0].code), issues[0].detail)
    return SitrepGuardResult.accept(sitrep)


# --- the role -----------------------------------------------------------------------------


@dataclass(frozen=True)
class SitrepOutcome:
    """A delivered SITREP and how it was produced (the audit row's fields)."""

    sitrep: Sitrep
    path: SitrepPath
    fixture_key: str | None
    llm_outcome: Literal["accepted", "rejected", "fallback_no_fixture"] | None
    violation: SitrepViolation | None = None
    detail: str = ""


class SitrepWriterRole:
    """One guarded ``sitrep_writer`` call per ``write``; the template on a miss or rejection."""

    def __init__(
        self, client: ModelClient, *, violations_path: Path = DEFAULT_VIOLATIONS_PATH
    ) -> None:
        self.client = client
        self.violations_path = Path(violations_path)

    def write(self, inputs: SitrepInputs, *, when: str) -> SitrepOutcome:
        prompt = sitrep_writer_prompt(inputs)
        key = fixture_key(ROLE_SITREP_WRITER, prompt)
        raw = self.client.call(ROLE_SITREP_WRITER, prompt, system=SITREP_WRITER_PROMPT)
        if raw is None:
            return SitrepOutcome(
                template_sitrep(inputs), SitrepPath.TEMPLATE, key, "fallback_no_fixture"
            )
        result = guard_sitrep(raw, inputs)
        if not result.accepted or result.value is None:
            code = result.violation or SitrepViolation.SCHEMA
            append_violation(
                ViolationRow(
                    when,
                    ROLE_SITREP_WRITER,
                    key,
                    VIOLATION_CODE[code],
                    FALLBACK_TEMPLATE_SITREP,
                    f"{code.value}: {result.detail}",
                ),
                self.violations_path,
            )
            return SitrepOutcome(
                template_sitrep(inputs),
                SitrepPath.FALLBACK_AFTER_REJECTION,
                key,
                "rejected",
                code,
                result.detail,
            )
        path = (
            SitrepPath.FIXTURE_REPLAY if self.client.mode is ModelMode.REPLAY else SitrepPath.LIVE
        )
        return SitrepOutcome(result.value, path, key, "accepted")


__all__ = [
    "FALLBACK_TEMPLATE_SITREP",
    "PROBABILITY_IN_PROSE",
    "ROLE_SITREP_WRITER",
    "SITREP_PROMPT_VERSION",
    "SITREP_WRITER_PROMPT",
    "VIOLATION_CODE",
    "SitrepGuardResult",
    "SitrepOutcome",
    "SitrepPath",
    "SitrepViolation",
    "SitrepWriterOutput",
    "SitrepWriterRole",
    "WriterLine",
    "guard_sitrep",
    "missing_features",
    "prompt_rows",
    "sitrep_writer_prompt",
    "template_sitrep",
]
