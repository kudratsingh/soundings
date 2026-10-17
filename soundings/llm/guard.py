"""The guard between a model reply and anything the system does (CLAUDE.md 2.2).

A model may propose one allowlisted probe, or hypotheses from the fixed eight with a
question for a person. It may never name a rung, an approval, an action or a tool call.
Every reply passes through ``check_probe_plan`` or ``check_hypothesis_set`` before use;
a rejection names one ``ViolationCode`` and the caller appends a ``ViolationRow`` to
``docs/log/VIOLATIONS.md`` (or a path it passes) and uses its deterministic fallback.

The guard does not import the probe allowlist module: the allowlist with each probe's
required rung and costs, the remaining budgets, and the probes already run arrive as a
``GuardContext`` built by the caller. Both checks are pure: no I/O, no clock, no rng.

Check order (the first failing check names the violation):

1. ``SCHEMA_INVALID`` when the reply is not a JSON object.
2. Forbidden keys anywhere in the raw reply, before schema validation, so the specific
   code wins: ``NAMES_TOOL_CALL``, then ``NAMES_APPROVAL``, then ``NAMES_RUNG``.
3. A ``next_probe`` value that looks like a call, an approval, a rung, a ladder state or
   a ``config/ladder.yaml`` boundary action that is not itself a probe id.
4. Schema validation. A failure caused by an id outside the eight hypotheses is
   ``HYPOTHESIS_OUTSIDE_SET``; one caused by denylisted text is ``DENYLISTED_TEXT``;
   anything else is ``SCHEMA_INVALID``.
5. ``NOT_IN_ALLOWLIST``, ``ABOVE_CURRENT_RUNG``, ``OVER_BUDGET``, ``ALREADY_RUN``.
"""

from __future__ import annotations

import enum
import functools
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from soundings.llm.client import REPO_ROOT
from soundings.schemas.config import load_ladder_config
from soundings.schemas.denylist import FreeText, find_denylisted
from soundings.schemas.enums import Hypothesis, LadderState, ProbeId, Rung

__all__ = [
    "DEFAULT_VIOLATIONS_PATH",
    "VIOLATIONS_HEADER",
    "WHY_MAX_CHARS",
    "GuardContext",
    "GuardResult",
    "HypothesisEntry",
    "HypothesisSetOutput",
    "ProbePlanOutput",
    "ProbeTerms",
    "RemainingBudget",
    "ViolationCode",
    "ViolationRow",
    "append_violation",
    "check_hypothesis_set",
    "check_probe_plan",
    "format_violation_row",
]

DEFAULT_VIOLATIONS_PATH = REPO_ROOT / "docs" / "log" / "VIOLATIONS.md"
WHY_MAX_CHARS = 200

VIOLATIONS_HEADER = (
    "# Model-output violations\n"
    "\n"
    "Every time a model response was rejected by a guard. Appended automatically by "
    '`soundings/llm/guard.py`. This file is demo material: "here is every time the model '
    "tried to do something it isn't allowed to, and what happened instead.\"\n"
    "\n"
    "| when | role | fixture key | violation | fallback used |\n"
    "|---|---|---|---|---|\n"
)
"""The exact header of ``docs/log/VIOLATIONS.md``; written when the log file is missing."""


class ViolationCode(enum.StrEnum):
    """Why a model reply was rejected, in check-precedence order."""

    SCHEMA_INVALID = "SCHEMA_INVALID"
    NAMES_TOOL_CALL = "NAMES_TOOL_CALL"
    NAMES_APPROVAL = "NAMES_APPROVAL"
    NAMES_RUNG = "NAMES_RUNG"
    NOT_IN_ALLOWLIST = "NOT_IN_ALLOWLIST"
    ABOVE_CURRENT_RUNG = "ABOVE_CURRENT_RUNG"
    OVER_BUDGET = "OVER_BUDGET"
    ALREADY_RUN = "ALREADY_RUN"
    HYPOTHESIS_OUTSIDE_SET = "HYPOTHESIS_OUTSIDE_SET"
    DENYLISTED_TEXT = "DENYLISTED_TEXT"


# --- output schemas ----------------------------------------------------------------------


class _OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _one_line(value: str | None) -> str | None:
    if value is not None and ("\n" in value or "\r" in value):
        raise ValueError("must be one line")
    return value


class ProbePlanOutput(_OutputModel):
    """The probe planner's reply. ``next_probe`` is a plain string so the guard can say
    why a bad value is bad instead of failing at the schema stage."""

    next_probe: str
    why: FreeText = Field(min_length=1, max_length=WHY_MAX_CHARS)
    hypotheses_to_discriminate: list[Hypothesis] = Field(min_length=1, max_length=3)
    question_for_human: FreeText | None = Field(default=None, max_length=WHY_MAX_CHARS)

    @field_validator("why", "question_for_human")
    @classmethod
    def _single_line(cls, v: str | None) -> str | None:
        return _one_line(v)

    @field_validator("hypotheses_to_discriminate")
    @classmethod
    def _unique(cls, v: list[Hypothesis]) -> list[Hypothesis]:
        if len(set(v)) != len(v):
            raise ValueError("hypotheses_to_discriminate must be unique")
        return v


class HypothesisEntry(_OutputModel):
    """One proposed hypothesis. ``id`` is a plain string; the guard checks it is one of
    the fixed eight. There is no probability field: the posterior is the scorer's."""

    id: str
    why: FreeText = Field(min_length=1, max_length=WHY_MAX_CHARS)

    @field_validator("why")
    @classmethod
    def _single_line(cls, v: str) -> str:
        _one_line(v)
        return v


class HypothesisSetOutput(_OutputModel):
    """The hypothesis writer's reply: 1 to 8 entries with unique ids, and a question."""

    hypotheses: list[HypothesisEntry] = Field(min_length=1, max_length=len(Hypothesis))
    question_for_human: FreeText | None = Field(default=None, max_length=WHY_MAX_CHARS)

    @field_validator("question_for_human")
    @classmethod
    def _single_line(cls, v: str | None) -> str | None:
        return _one_line(v)

    @field_validator("hypotheses")
    @classmethod
    def _unique_ids(cls, v: list[HypothesisEntry]) -> list[HypothesisEntry]:
        ids = [e.id for e in v]
        if len(set(ids)) != len(ids):
            raise ValueError("hypothesis ids must be unique")
        return v

    def ids(self) -> tuple[Hypothesis, ...]:
        """The entries' ids as ``Hypothesis`` members (only valid after the guard)."""
        return tuple(Hypothesis(e.id) for e in self.hypotheses)


# --- context and result ------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _ladder_rungs() -> tuple[Rung, ...]:
    """The rung order from ``config/ladder.yaml``, never a literal list."""
    return load_ladder_config().rungs


@dataclass(frozen=True)
class ProbeTerms:
    """What the guard needs to know about one allowlisted probe."""

    rung_required: Rung
    cost_usd: float
    latency_s: float = 0.0
    interruption: int = 0

    @property
    def paid(self) -> bool:
        return self.cost_usd > 0.0


@dataclass(frozen=True)
class RemainingBudget:
    """Per-contact budget left: planner probes, paid probes, human interruptions."""

    probes: int
    paid_probes: int
    interruptions: int

    def allows(self, terms: ProbeTerms) -> str | None:
        """``None`` when one more run of this probe fits, else which limit it breaks."""
        if self.probes < 1:
            return "probes_per_contact"
        if terms.paid and self.paid_probes < 1:
            return "paid_probes_per_contact"
        if terms.interruption > self.interruptions:
            return "human_interruptions_per_contact"
        return None


@dataclass(frozen=True)
class GuardContext:
    """Everything a probe-plan check depends on, supplied by the caller.

    ``allowlist`` maps each allowlisted probe to its terms (allowlist order is kept);
    ``already_run`` lists probes whose idempotency key already ran in this bucket;
    ``rung_order`` defaults to ``config/ladder.yaml`` ``rungs``.
    """

    allowlist: Mapping[ProbeId, ProbeTerms]
    current_rung: Rung
    remaining: RemainingBudget
    contact_id: str
    tick: int
    already_run: frozenset[ProbeId] = frozenset()
    rung_order: tuple[Rung, ...] = field(default_factory=_ladder_rungs)

    def rung_index(self, rung: Rung) -> int:
        return self.rung_order.index(rung)


@dataclass(frozen=True)
class GuardResult:
    """An accepted value, or the one violation that rejected the reply."""

    accepted: bool
    value: ProbePlanOutput | HypothesisSetOutput | None
    violation: ViolationCode | None
    detail: str

    @classmethod
    def accept(cls, value: ProbePlanOutput | HypothesisSetOutput) -> GuardResult:
        return cls(True, value, None, "")

    @classmethod
    def reject(cls, code: ViolationCode, detail: str) -> GuardResult:
        return cls(False, None, code, detail)


# --- forbidden vocabulary ----------------------------------------------------------------

TOOL_CALL_KEYS: frozenset[str] = frozenset(
    {
        "tool_use",
        "tool_uses",
        "tool_call",
        "tool_calls",
        "tool",
        "tools",
        "function_call",
        "function",
        "action",
        "actions",
        "command",
        "execute",
        "invoke",
    }
)
APPROVAL_KEYS: frozenset[str] = frozenset(
    {
        "approve",
        "approval",
        "approvals",
        "approved",
        "approval_token",
        "token",
        "authorize",
        "authorization",
        "authorized",
        "grant",
    }
)
RUNG_KEYS: frozenset[str] = frozenset(
    {
        "rung",
        "recommended_rung",
        "next_rung",
        "set_rung",
        "ladder_state",
        "next_state",
        "transition",
    }
)


RUNG_SPOKEN_FORMS: frozenset[str] = frozenset(
    {"hand to a person", "hand off", "handoff", "hand over", "slow down", "climb", "escalate"}
)
"""Spoken forms of a rung or a climb that ``next_probe`` may carry instead of the id."""


def _norm(text: str) -> str:
    return "_".join(text.strip().lower().replace("-", " ").split())


@functools.lru_cache(maxsize=1)
def _rung_words() -> frozenset[str]:
    """Rung and state values plus every boundary action that is not a probe id."""
    cfg = load_ladder_config()
    probe_ids = {p.value for p in ProbeId}
    actions = {
        a
        for b in cfg.boundary.values()
        for a in (*b.platform_alone, *b.standing_orders, *b.person_approves)
    }
    words = (
        {r.value for r in Rung}
        | {s.value.lower() for s in LadderState}
        | actions
        | RUNG_SPOKEN_FORMS
    )
    return frozenset(_norm(w) for w in words if w not in probe_ids)


def _walk_keys(obj: Any) -> Iterator[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _walk_keys(v)
    elif isinstance(obj, list | tuple):
        for v in obj:
            yield from _walk_keys(v)


def _walk_strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(obj, list | tuple):
        for v in obj:
            yield from _walk_strings(v)


def _forbidden_key(raw: dict[str, Any]) -> GuardResult | None:
    keys = [_norm(k) for k in _walk_keys(raw)]
    for code, vocab in (
        (ViolationCode.NAMES_TOOL_CALL, TOOL_CALL_KEYS),
        (ViolationCode.NAMES_APPROVAL, APPROVAL_KEYS),
        (ViolationCode.NAMES_RUNG, RUNG_KEYS),
    ):
        hits = sorted({k for k in keys if k in vocab})
        if hits:
            return GuardResult.reject(code, f"forbidden key {hits[0]!r}")
    return None


def _classify_probe_value(value: str) -> GuardResult | None:
    """Reject a ``next_probe`` that is a call, an approval or a rung, not a probe."""
    norm = _norm(value)
    if any(ch in value for ch in "(){}") or norm in TOOL_CALL_KEYS:
        return GuardResult.reject(
            ViolationCode.NAMES_TOOL_CALL, f"next_probe {value!r} looks like a call"
        )
    if norm in APPROVAL_KEYS or norm.startswith("approv"):
        return GuardResult.reject(
            ViolationCode.NAMES_APPROVAL, f"next_probe {value!r} names an approval"
        )
    if norm in _rung_words():
        return GuardResult.reject(
            ViolationCode.NAMES_RUNG, f"next_probe {value!r} names a rung, state or action"
        )
    return None


def _schema_failure(raw: dict[str, Any], err: ValidationError, hyp_paths: list[Any]) -> GuardResult:
    known = {h.value for h in Hypothesis}
    outside = sorted({v for v in hyp_paths if isinstance(v, str) and v not in known})
    if outside:
        return GuardResult.reject(
            ViolationCode.HYPOTHESIS_OUTSIDE_SET, f"hypothesis {outside[0]!r} is not in the eight"
        )
    hits = [h for s in _walk_strings(raw) for h in find_denylisted(s)]
    if hits:
        return GuardResult.reject(
            ViolationCode.DENYLISTED_TEXT, f"{len(hits)} denylisted term(s) in the reply"
        )
    first = err.errors()[0]
    loc = ".".join(str(p) for p in first["loc"]) or "<root>"
    return GuardResult.reject(ViolationCode.SCHEMA_INVALID, f"{loc}: {first['msg']}")


# --- checks ------------------------------------------------------------------------------


def check_probe_plan(raw: Any, ctx: GuardContext) -> GuardResult:
    """Accept a probe plan only if it names one allowlisted, permitted, affordable probe."""
    if not isinstance(raw, dict):
        return GuardResult.reject(ViolationCode.SCHEMA_INVALID, "reply is not a JSON object")
    found = _forbidden_key(raw)
    if found is not None:
        return found
    probe_value = raw.get("next_probe")
    if isinstance(probe_value, str):
        found = _classify_probe_value(probe_value)
        if found is not None:
            return found
    try:
        plan = ProbePlanOutput.model_validate(raw)
    except ValidationError as err:
        hyps = raw.get("hypotheses_to_discriminate")
        return _schema_failure(raw, err, hyps if isinstance(hyps, list) else [])

    probe_ids = {p.value: p for p in ProbeId}
    probe = probe_ids.get(plan.next_probe)
    if probe is None or probe not in ctx.allowlist:
        return GuardResult.reject(
            ViolationCode.NOT_IN_ALLOWLIST, f"next_probe {plan.next_probe!r} is not allowlisted"
        )
    terms = ctx.allowlist[probe]
    if ctx.rung_index(terms.rung_required) > ctx.rung_index(ctx.current_rung):
        return GuardResult.reject(
            ViolationCode.ABOVE_CURRENT_RUNG,
            f"{probe.value} needs {terms.rung_required.value}; current rung is "
            f"{ctx.current_rung.value}",
        )
    broken = ctx.remaining.allows(terms)
    if broken is not None:
        return GuardResult.reject(ViolationCode.OVER_BUDGET, f"{probe.value} would exceed {broken}")
    if probe in ctx.already_run:
        return GuardResult.reject(
            ViolationCode.ALREADY_RUN, f"{probe.value} already ran in this bucket"
        )
    return GuardResult.accept(plan)


def check_hypothesis_set(raw: Any) -> GuardResult:
    """Accept hypotheses only from the fixed eight, with no rung, approval or tool call."""
    if not isinstance(raw, dict):
        return GuardResult.reject(ViolationCode.SCHEMA_INVALID, "reply is not a JSON object")
    found = _forbidden_key(raw)
    if found is not None:
        return found
    entries = raw.get("hypotheses")
    ids = [e.get("id") for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    try:
        value = HypothesisSetOutput.model_validate(raw)
    except ValidationError as err:
        return _schema_failure(raw, err, ids)
    known = {h.value for h in Hypothesis}
    outside = [e.id for e in value.hypotheses if e.id not in known]
    if outside:
        return GuardResult.reject(
            ViolationCode.HYPOTHESIS_OUTSIDE_SET, f"hypothesis {outside[0]!r} is not in the eight"
        )
    return GuardResult.accept(value)


# --- violation log -----------------------------------------------------------------------


@dataclass(frozen=True)
class ViolationRow:
    """One rejected reply. ``when`` comes from the caller (run, seed, tick), never a clock."""

    when: str
    role: str
    fixture_key: str
    violation: ViolationCode
    fallback_used: str
    detail: str = ""


def _cell(text: str) -> str:
    return " ".join(text.replace("\\", "\\\\").replace("|", "\\|").split())


def format_violation_row(row: ViolationRow) -> str:
    """One markdown table line in the header's column order, ``|`` escaped."""
    violation = row.violation.value + (f": {row.detail}" if row.detail else "")
    cells = (row.when, row.role, row.fixture_key, violation, row.fallback_used)
    return "| " + " | ".join(_cell(c) for c in cells) + " |\n"


def append_violation(row: ViolationRow, path: Path = DEFAULT_VIOLATIONS_PATH) -> None:
    """Append one row; write ``VIOLATIONS_HEADER`` first when the file is missing or empty."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(VIOLATIONS_HEADER, encoding="utf-8")
    existing = path.read_bytes()
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        if not existing.endswith(b"\n"):
            fh.write("\n")
        fh.write(format_violation_row(row))
