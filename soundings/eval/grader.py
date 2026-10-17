"""Deterministic grader: a ``PolicyRun`` against a scenario's ``expected`` block.

``grade(run, expected)`` returns one ``KeyGrade`` per key the scenario declares, each
``pass``, ``fail`` or ``not_graded`` with the expected value, the observed value and a
reason a person can read. No model is involved and nothing is re-decided: the grader
only compares what the run logged with what the scenario file states.

The graded subject is one ground-truth contact (by default the first non-false-alarm
contact in id order, which is the scenario's own ``gt-1``), rolled up over every
observed id it was tracked under. Keys the ``PolicyRun`` cannot answer on its own
(``top_hypothesis``, ``min_threat_mass`` and the SITREP keys) are graded only when the
caller passes them in ``Observations``; otherwise they are ``not_graded``, never passed.

``max_human_interruptions`` uses the one interruption definition (CLAUDE.md section 8
A5; WO-050 ``interruption_kind``): approval request, backup page, handoff to the watch
officer, or the officer asked to look. Operator notices and watch-after-handoff updates
do not count.

Every field of ``Expected`` has a rule in ``RULES``; a field added later without a rule
is reported ``not_graded`` with ``NO_RULE`` and a test fails on the gap.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from soundings.eval.policies import PolicyRun, interruption_kind, outcome_for_gt
from soundings.schemas import Hypothesis
from soundings.schemas._base import NonNegInt, StrictModel
from soundings.schemas.scenario import Expected

NO_RULE = "no grading rule for this key yet"
NEEDS_OBSERVATION = "the policy run does not carry this; pass it in Observations"
UNDETECTED = "UNDETECTED"


class KeyStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_GRADED = "not_graded"


class KeyGrade(StrictModel):
    key: str
    status: KeyStatus
    expected: str
    observed: str
    reason: str


class Observations(StrictModel):
    """Values a ``PolicyRun`` does not carry, supplied by a caller that has them."""

    threat_mass: float | None = None
    top_hypothesis: Hypothesis | None = None
    sitrep_has_search_box: bool | None = None
    search_box_contains_truth: bool | None = None


class GradeResult(StrictModel):
    scenario_id: str
    seed: int
    policy: str
    gt_id: str | None
    checks: tuple[KeyGrade, ...]
    passed: bool
    """True when no declared key failed (``not_graded`` keys do not fail a run)."""
    failed_keys: tuple[str, ...]
    graded: NonNegInt

    def check(self, key: str) -> KeyGrade:
        for c in self.checks:
            if c.key == key:
                return c
        raise KeyError(key)


class _Subject:
    """The graded ground-truth contact's slice of the run."""

    def __init__(self, run: PolicyRun, gt_id: str | None) -> None:
        self.run = run
        self.gt_id = gt_id
        if gt_id is None:
            self.ids: frozenset[str] = frozenset()
        else:
            self.ids = frozenset(c.observed_contact_id for c in run.contacts if c.gt_id == gt_id)
        self.transitions = tuple(t for t in run.transition_log if t.contact_id in self.ids)
        self.outcome = outcome_for_gt(run, gt_id) if gt_id is not None else None


def _default_gt(run: PolicyRun) -> str | None:
    for g in sorted(run.gt_contacts, key=lambda g: g.gt_id):
        if not g.is_false_alarm:
            return g.gt_id
    return None


def _text(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, tuple):
        return " > ".join(_text(v) for v in value) if value else "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(getattr(value, "value", value))


Rule = Callable[[str, object, _Subject, Observations], KeyGrade]


def _verdict(key: str, ok: bool, expected: object, observed: object, why: str) -> KeyGrade:
    return KeyGrade(
        key=key,
        status=KeyStatus.PASS if ok else KeyStatus.FAIL,
        expected=_text(expected),
        observed=_text(observed),
        reason=why,
    )


def _not_graded(key: str, expected: object, why: str) -> KeyGrade:
    return KeyGrade(
        key=key,
        status=KeyStatus.NOT_GRADED,
        expected=_text(expected),
        observed="none",
        reason=why,
    )


def _terminal(key: str, want: object, s: _Subject, _o: Observations) -> KeyGrade:
    got = s.outcome.terminal_state if s.outcome is not None else None
    shown = got.value if got is not None else UNDETECTED
    if got == want:
        return _verdict(key, True, want, shown, "terminal state matches")
    return _verdict(key, False, want, shown, f"ended {shown}, scenario expects {_text(want)}")


def _passes(key: str, want: object, s: _Subject, _o: Observations) -> KeyGrade:
    wanted = tuple(want) if isinstance(want, tuple) else ()
    seen = {t.to_state for t in s.transitions}
    missing = [state for state in wanted if state not in seen]
    visited = tuple(sorted(seen, key=lambda st: st.value))
    if not missing:
        return _verdict(key, True, wanted, visited, "every required state was entered")
    names = ", ".join(m.value for m in missing)
    return _verdict(key, False, wanted, visited, f"never entered {names}")


def _interruptions(key: str, want: object, s: _Subject, _o: Observations) -> KeyGrade:
    limit = int(want) if isinstance(want, int) else 0
    kinds = [k for t in s.transitions if (k := interruption_kind(t)) is not None]
    n = len(kinds)
    ok = n <= limit
    named = ", ".join(sorted({k.value for k in kinds})) or "none"
    why = (
        f"{n} interruptions ({named}), limit {limit}"
        if ok
        else f"{n} interruptions ({named}) exceed the limit of {limit}"
    )
    return _verdict(key, ok, limit, n, why)


def _probes(key: str, want: object, s: _Subject, _o: Observations) -> KeyGrade:
    limit = int(want) if isinstance(want, int) else 0
    n = s.outcome.probes_used if s.outcome is not None else 0
    ok = n <= limit
    return _verdict(key, ok, limit, n, f"{n} probes used, limit {limit}")


def _top(key: str, want: object, _s: _Subject, o: Observations) -> KeyGrade:
    if o.top_hypothesis is None:
        return _not_graded(key, want, NEEDS_OBSERVATION)
    ok = o.top_hypothesis == want
    why = "top hypothesis matches" if ok else f"top hypothesis was {o.top_hypothesis.value}"
    return _verdict(key, ok, want, o.top_hypothesis, why)


def _min_mass(key: str, want: object, _s: _Subject, o: Observations) -> KeyGrade:
    if o.threat_mass is None:
        return _not_graded(key, want, NEEDS_OBSERVATION)
    floor = float(want) if isinstance(want, int | float) else 0.0
    ok = o.threat_mass >= floor
    why = f"threat_mass {o.threat_mass:.6f} {'reaches' if ok else 'is below'} {floor:.6f}"
    return _verdict(key, ok, floor, o.threat_mass, why)


def _flag(attr: str) -> Rule:
    def rule(key: str, want: object, _s: _Subject, o: Observations) -> KeyGrade:
        got = getattr(o, attr)
        if got is None:
            return _not_graded(key, want, NEEDS_OBSERVATION)
        ok = got == want
        return _verdict(key, ok, want, got, "matches" if ok else "differs from the scenario")

    return rule


RULES: dict[str, Rule] = {
    "terminal_state": _terminal,
    "passes_through": _passes,
    "top_hypothesis": _top,
    "min_threat_mass": _min_mass,
    "max_human_interruptions": _interruptions,
    "max_probes": _probes,
    "sitrep_has_search_box": _flag("sitrep_has_search_box"),
    "search_box_contains_truth": _flag("search_box_contains_truth"),
}


def _declared(value: object) -> bool:
    return value is not None and value != ()


def grade(
    run: PolicyRun,
    expected: Expected,
    *,
    observations: Observations | None = None,
    gt_id: str | None = None,
) -> GradeResult:
    """Grade one run against the scenario's ``expected`` block, key by key, in the
    block's field order. Keys the scenario leaves unset are not listed."""
    subject = _Subject(run, gt_id if gt_id is not None else _default_gt(run))
    obs = observations if observations is not None else Observations()
    checks: list[KeyGrade] = []
    for key in Expected.model_fields:
        want = getattr(expected, key)
        if not _declared(want):
            continue
        rule = RULES.get(key)
        if rule is None:
            checks.append(_not_graded(key, want, NO_RULE))
            continue
        checks.append(rule(key, want, subject, obs))
    failed = tuple(c.key for c in checks if c.status is KeyStatus.FAIL)
    return GradeResult(
        scenario_id=run.scenario_id,
        seed=run.seed,
        policy=run.policy.value,
        gt_id=subject.gt_id,
        checks=tuple(checks),
        passed=not failed,
        failed_keys=failed,
        graded=sum(1 for c in checks if c.status is not KeyStatus.NOT_GRADED),
    )
