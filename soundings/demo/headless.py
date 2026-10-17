"""Headless demo execution (WO-062 Part A): every step in-process, no network.

A catalog step runs its scenario through an injected ``ScenarioExecutor`` (default:
``soundings.runner.ScenarioRunner``, runner v1 as merged) at the scenario's seed or the
step's ``seed``. A ``twist`` step runs ``scenario_ref`` with the step's toggles added. A
``continue_run`` step reuses the previous step's outcome. ``live`` prints a placeholder
until WO-061; ``eval`` prints the headline of ``artifacts/eval/report.json`` when present.
An ``action`` step (WO-062P) notes which WO ships it (``action ... pending WO-0xx``) or
that the presenter overlay renders it; a ``twist_toggles`` action runs ``scenario_ref``
with its toggles like a twist step. A catalog step's ``beats`` add their pending notes.

The transcript holds sim data only (marks, seeds, states, ticks) and never a wall time, so
two runs of one script give identical transcripts (CLAUDE.md 2.4). Wall-clock pacing uses
the injected ``clock`` and ``sleep``; elapsed time goes to stderr, never to the transcript.

Part B replaces the executor with the API run engine and runner v3; the Protocol keeps
the step loop independent of either.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from soundings.demo.schedule import DEFAULT_TAIL_S, DEFAULT_TOTAL_S, Pace, build_schedule
from soundings.demo.script import (
    ACTION,
    EVAL,
    LIVE,
    TWIST,
    ActionName,
    DemoScript,
    DemoStep,
)
from soundings.schemas.enums import LadderState
from soundings.schemas.scenario import Scenario, Toggle
from soundings.sim.scenario import REPO_ROOT, resolve_scenario

DEFAULT_EVAL_REPORT = REPO_ROOT / "artifacts" / "eval" / "report.json"
LIVE_PLACEHOLDER = "judge injection: wired in WO-061"
EVAL_PENDING = "eval: pending WO-051"
APPROVE_PENDING = "approve: wired with runner v3 (WO-043)"
OVERLAY_NOTE = "presenter overlay"

# Which case state headlines a run: the most consequential physical case first.
_HEADLINE_ORDER: tuple[LadderState, ...] = (
    LadderState.FAILED,
    LadderState.HANDED_OFF,
    LadderState.WATCH_AFTER_HANDOFF,
    LadderState.AWAITING_APPROVAL,
    LadderState.HOLDING,
    LadderState.ADVISING_SLOW,
    LadderState.LOST_VEHICLE,
    LadderState.HAZARD_NOTICE,
    LadderState.INVESTIGATING,
    LadderState.ASKING,
    LadderState.WATCHING,
    LadderState.DETECTED,
    LadderState.RESOLVED_DEPARTED,
    LadderState.RESOLVED_BENIGN,
    LadderState.CLEARED_AT_ASK,
)
_RANK = {s: i for i, s in enumerate(_HEADLINE_ORDER)}


@dataclass(frozen=True)
class CaseView:
    """What the demo needs from one case of a run."""

    case_id: str
    state: LadderState
    tick: int
    physical: bool
    """False for a sim false alarm (graded by ``soundings.eval.truth``); presentation only."""


@dataclass(frozen=True)
class StepOutcome:
    """A run's headline state, the tick it was reached, and the per-state case counts."""

    state: LadderState
    tick: int
    seed: int
    cases: tuple[tuple[str, int], ...] = ()


class ScenarioExecutor(Protocol):
    """Runs one scenario at one seed and returns its case views (in-process or via API)."""

    def run_scenario(self, scenario: Scenario, seed: int) -> Sequence[CaseView]: ...


class RunnerV1Executor:
    """``soundings.runner.ScenarioRunner`` as merged (runner v1, the ask rung)."""

    def run_scenario(self, scenario: Scenario, seed: int) -> Sequence[CaseView]:
        from soundings.eval.truth import physical_contact_ids
        from soundings.runner import ScenarioRunner

        result = ScenarioRunner(scenario, seed=seed).run()
        physical = physical_contact_ids(result.sim)
        return [
            CaseView(
                case_id=c.case_id,
                state=c.state,
                tick=c.closed_tick if c.closed_tick is not None else c.opened_tick,
                physical=c.contact_id in physical,
            )
            for c in result.cases
        ]


def headline(cases: Sequence[CaseView], seed: int, n_ticks_fallback: int = 0) -> StepOutcome:
    """The headline of a run: among physical cases (all cases if none), the state
    ranked first in ``_HEADLINE_ORDER``, at the latest tick that state was reached."""
    counts = Counter(c.state.value for c in cases)
    count_items = tuple(sorted(counts.items()))
    pool = [c for c in cases if c.physical] or list(cases)
    if not pool:
        return StepOutcome(LadderState.WATCHING, n_ticks_fallback, seed, count_items)
    best = min(pool, key=lambda c: (_RANK.get(c.state, len(_RANK)), c.state.value))
    tick = max(c.tick for c in pool if c.state is best.state)
    return StepOutcome(best.state, tick, seed, count_items)


def with_toggles(scenario: Scenario, step: DemoStep) -> Scenario:
    """``scenario`` with the step's toggles appended (names already present are kept once)."""
    have = {t.name for t in scenario.toggles}
    extra = tuple(Toggle(name=name) for name in step.toggles if name not in have)
    if not extra:
        return scenario
    data = scenario.model_dump()
    data["toggles"] = tuple(t.model_dump() for t in scenario.toggles + extra)
    return Scenario.model_validate(data)


def eval_headline(report: Path) -> str:
    """One line from ``artifacts/eval/report.json`` (WO-051), or the pending line."""
    if not report.is_file():
        return EVAL_PENDING
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"eval: {report.name} unreadable"
    if isinstance(data, dict):
        head = data.get("headline")
        if isinstance(head, str) and head.strip():
            return f"eval: {head.strip()}"
        policies = data.get("policies")
        if isinstance(policies, dict):
            return "eval: policies " + ", ".join(sorted(str(k) for k in policies))
        if isinstance(policies, list):
            names = [
                str(p.get("policy", p.get("name", "?"))) for p in policies if isinstance(p, dict)
            ]
            return "eval: policies " + ", ".join(names)
    return f"eval: {report.name} present"


@dataclass(frozen=True)
class TranscriptLine:
    """``[m:ss] <label> seed=<n> -> <state> tick=<n> | say: <line>`` (sim data only)."""

    mark: str
    label: str
    say: str
    seed: int | None = None
    state: LadderState | None = None
    tick: int | None = None
    note: str | None = None
    expect_state: LadderState | None = None

    @property
    def ok(self) -> bool:
        return self.expect_state is None or self.state is self.expect_state

    def render(self) -> str:
        head = f"[{self.mark}] {self.label}"
        if self.seed is not None:
            head += f" seed={self.seed}"
        parts: list[str] = []
        if self.state is not None:
            parts.append(f"{self.state.value} tick={self.tick}")
        if self.note:
            parts.append(self.note)
        body = " ; ".join(parts) if parts else "-"
        flag = (
            "" if self.ok else f" [expected {self.expect_state.value if self.expect_state else ''}]"
        )
        return f"{head} -> {body}{flag} | say: {self.say}"


@dataclass
class DemoResult:
    lines: list[TranscriptLine] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(line.ok for line in self.lines)

    def text(self) -> str:
        return "".join(f"{line.render()}\n" for line in self.lines)


def run_step(
    step: DemoStep,
    *,
    executor: ScenarioExecutor,
    previous: StepOutcome | None,
    eval_report: Path,
) -> tuple[TranscriptLine, StepOutcome | None]:
    """Execute one step; returns its transcript line and its outcome (None if no run)."""
    if step.kind == EVAL:
        line = TranscriptLine(step.t, step.label, step.say, note=eval_headline(eval_report))
        return line, None
    if step.kind == LIVE:
        line = TranscriptLine(step.t, step.label, step.say, note=LIVE_PLACEHOLDER)
        return line, None
    twist_action = step.action is ActionName.TWIST_TOGGLES
    if step.kind == ACTION and not twist_action:
        note = " ; ".join(step.pending) or OVERLAY_NOTE
        return TranscriptLine(step.t, step.label, step.say, note=note), None
    ref = step.scenario_ref if twist_action else step.run_ref
    assert ref is not None
    notes: list[str] = []
    if step.continue_run and previous is not None:
        outcome = previous
        notes.append("continued")
    else:
        scenario = resolve_scenario(ref)
        if step.kind == TWIST or twist_action:
            scenario = with_toggles(scenario, step)
        seed = scenario.seed if step.seed is None else step.seed
        outcome = headline(executor.run_scenario(scenario, seed), seed, scenario.duration_ticks)
    if step.approve:
        notes.append(APPROVE_PENDING)
    notes.extend(step.pending)
    line = TranscriptLine(
        step.t,
        step.label,
        step.say,
        seed=outcome.seed,
        state=outcome.state,
        tick=outcome.tick,
        note=" ; ".join(notes) or None,
        expect_state=step.expect_state,
    )
    return line, outcome


def run_demo(
    script: DemoScript,
    *,
    pace: Pace = Pace.REALTIME,
    total_s: float = DEFAULT_TOTAL_S,
    tail_s: float = DEFAULT_TAIL_S,
    speed: float = 1.0,
    executor: ScenarioExecutor | None = None,
    eval_report: Path = DEFAULT_EVAL_REPORT,
    emit: Callable[[str], None] = print,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> DemoResult:
    """Run every step at its scheduled offset, emitting each transcript line as it lands."""
    schedule = build_schedule(
        script.marks_s, pace=pace, total_s=total_s, tail_s=tail_s, speed=speed
    )
    runner = executor if executor is not None else RunnerV1Executor()
    result = DemoResult()
    previous: StepOutcome | None = None
    start = clock()

    def wait_until(offset: float) -> None:
        delay = offset - (clock() - start)
        if delay > 0:
            sleep(delay)

    for step, slot in zip(script.steps, schedule.steps, strict=True):
        wait_until(slot.at_s)
        line, outcome = run_step(step, executor=runner, previous=previous, eval_report=eval_report)
        if outcome is not None:
            previous = outcome
        result.lines.append(line)
        emit(line.render())
    wait_until(schedule.total_s)
    print(f"demo: elapsed {clock() - start:.1f} s wall", file=sys.stderr)
    return result


__all__ = [
    "APPROVE_PENDING",
    "DEFAULT_EVAL_REPORT",
    "EVAL_PENDING",
    "LIVE_PLACEHOLDER",
    "OVERLAY_NOTE",
    "CaseView",
    "DemoResult",
    "RunnerV1Executor",
    "ScenarioExecutor",
    "StepOutcome",
    "TranscriptLine",
    "eval_headline",
    "headline",
    "run_demo",
    "run_step",
    "with_toggles",
]
