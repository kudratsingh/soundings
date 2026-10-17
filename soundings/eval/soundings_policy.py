"""The soundings policy over runner v3 (WO-051 part B).

``SoundingsPolicy(planner="fallback" | "llm", fixture_store=None, violations_log=None)``
implements WO-050's ``Policy``. One ``run(scenario, seed, sim_likelihoods=)`` runs
``ScenarioRunnerV3(scenario, seed=seed, sim_likelihoods=...)`` under the signed standing
orders and maps its ``RunResultV3`` onto a WO-050 ``PolicyRun`` in ``_from_run_result``
through ``assemble_run``. Nothing is re-decided here: every state comes from the ladder's
checkpoints (or, for a contact with no ladder run, from runner v2's transitions), every
probe from ``probe_calls``, shutdowns from ``HOLDING`` intervals via ``hold_shutdowns``.

Mapping (the counting itself is WO-050's, shared with the baselines):

* each ladder checkpoint -> one ``Transition`` (``event`` mapped onto ``PolicyEvent``);
  the investigation's non-cached probe calls -> ``PROBE_CALL`` transitions inside the
  ``INVESTIGATING`` stretch (an eyes-on probe is an interruption by WO-050's rule);
  a ``page_backup`` action -> a ``BACKUP_PAGED`` transition at its tick;
* the work stops at the handoff: checkpoints after ``HANDED_OFF`` (watch-after-handoff
  updates, which are never interruptions) are not in the eval log;
* the ``PolicyRun`` is assembled over the scenario-length sim (same contacts and
  ``track_hash`` as the baselines); ladder timers that fall in v3's tail (WO-043) stay in
  the log, and holds are closed by the ladder's own transitions;
* ``PlannerStats`` from ``planner_choices``: ``calls`` = model calls made (choices with
  source ``llm`` or ``fallback`` except a spent budget), ``fixture_hits`` = calls that found a
  reply (all but replay misses), ``guard_rejections`` = replies the guard or the re-check
  refused, ``fallbacks`` = calls that ended on the deterministic choice.

Extra knobs for the eval sweeps (``soundings.eval.sweeps``): ``stopping`` (an overridden
investigation stopping config; config/ladder.yaml is never edited) and ``toggles`` (twist
toggles added to the run). Deterministic: no clock reads; the audit log is in memory.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from soundings.canonical import round6
from soundings.eval.policies import (
    PlannerStats,
    PolicyEvent,
    PolicyName,
    PolicyRun,
    Transition,
    assemble_run,
    hold_shutdowns,
    slow_minutes,
)
from soundings.investigation.machine import StoppingConfig
from soundings.ladder.driver import LadderRun
from soundings.ladder.types import ActionKind
from soundings.runner import RunResultV3, ScenarioRunnerV3
from soundings.runner import Transition as RunnerTransition
from soundings.schemas import LadderState, LikelihoodSpec, ProbeId, Scenario, ToggleName
from soundings.schemas._base import REPO_ROOT

S = LadderState
PlannerArm = Literal["fallback", "llm"]

DEFAULT_EVAL_VIOLATIONS: Path = REPO_ROOT / "artifacts" / "eval" / "violations.md"
"""Where an eval run logs guard rejections when no path is given (never docs/log/)."""

POST_HANDOFF_STATES: frozenset[LadderState] = frozenset({S.HANDED_OFF, S.WATCH_AFTER_HANDOFF})
"""A checkpoint leaving one of these is watching after the handoff (not in the eval log)."""

NO_CALL_REASONS: frozenset[str] = frozenset({"llm_budget_spent"})
"""``fallback_reason`` values for which the LLM planner made no model call."""
MISS_REASONS: frozenset[str] = frozenset({"replay_miss"})
"""``fallback_reason`` values for a call that found no recorded reply."""

_EVENT_OF_KIND: Mapping[str, PolicyEvent] = {
    "contact_detected": PolicyEvent.CONTACT_DETECTED,
    "contact_departed": PolicyEvent.CONTACT_DEPARTED,
    "contact_persisted": PolicyEvent.SITREP_UPDATE,
    "handoff_complete": PolicyEvent.WATCH_STARTED,
}


def _policy_event(to_state: LadderState, kind: str, arg: str | None) -> PolicyEvent:
    if to_state is S.AWAITING_APPROVAL:
        return PolicyEvent.APPROVAL_REQUESTED
    if to_state is S.HANDED_OFF:
        return PolicyEvent.HANDED_TO_PERSON
    if to_state is S.LOST_VEHICLE:
        return PolicyEvent.OPERATOR_NOTICE
    if kind == "approval" and arg == "granted":
        return PolicyEvent.APPROVAL_GRANTED
    return _EVENT_OF_KIND.get(kind, PolicyEvent.STATE_CHANGE)


def planner_stats(result: RunResultV3) -> PlannerStats:
    """``PlannerStats`` from the run's planner choices (see the module docstring)."""
    calls = hits = rejections = fallbacks = 0
    for c in result.planner_choices:
        source = c.choice.source
        reason = c.choice.fallback_reason
        if source == "deterministic" or (source == "fallback" and reason in NO_CALL_REASONS):
            continue
        calls += 1
        if source == "llm":
            hits += 1
            continue
        fallbacks += 1
        if reason in MISS_REASONS or reason is None:
            continue
        hits += 1
        rejections += 1
    return PlannerStats(
        calls=calls, fixture_hits=hits, guard_rejections=rejections, fallbacks=fallbacks
    )


def _ladder_steps(
    run: LadderRun, probes: Sequence[ProbeId], probe_ticks: Sequence[int]
) -> list[tuple[int, LadderState, LadderState, PolicyEvent, str | None, ProbeId | None]]:
    """(tick, from, to, event, asset, probe) for one contact's ladder, probes inserted while
    INVESTIGATING, backup pages at their tick; nothing after the handoff."""
    asset_of_key = {
        a.idempotency_key: a.scope.asset_id
        for a in run.actions
        if a.scope is not None and a.scope.asset_id
    }
    pages = sorted(a.tick for a in run.actions if a.kind is ActionKind.PAGE_BACKUP)
    out: list[tuple[int, LadderState, LadderState, PolicyEvent, str | None, ProbeId | None]] = []
    pending = list(zip(probe_ticks, probes, strict=True))
    state: LadderState | None = None
    for cp in run.checkpoints:
        if cp.from_state in POST_HANDOFF_STATES:
            break
        if cp.from_state is S.INVESTIGATING:
            while pending and pending[0][0] <= cp.tick:
                tick, probe = pending.pop(0)
                out.append(
                    (tick, S.INVESTIGATING, S.INVESTIGATING, PolicyEvent.PROBE_CALL, None, probe)
                )
        while pages and pages[0] < cp.tick and state is not None:
            out.append((pages.pop(0), state, state, PolicyEvent.BACKUP_PAGED, None, None))
        asset = next((asset_of_key[k] for k in cp.action_keys if k in asset_of_key), None)
        event = _policy_event(cp.to_state, cp.event.kind.value, cp.event.arg)
        out.append((cp.tick, cp.from_state, cp.to_state, event, asset, None))
        state = cp.to_state
        if cp.to_state is S.HANDED_OFF:
            pages = [t for t in pages if t <= cp.tick]
    for tick, probe in pending:  # an investigation the ladder never closed
        cur = state if state is not None else S.INVESTIGATING
        out.append((tick, cur, cur, PolicyEvent.PROBE_CALL, None, probe))
    for tick in pages:
        cur = state if state is not None else S.DETECTED
        out.append((tick, cur, cur, PolicyEvent.BACKUP_PAGED, None, None))
    return out


def _v2_steps(
    ts: Iterable[RunnerTransition],
) -> list[tuple[int, LadderState, LadderState, PolicyEvent, str | None, ProbeId | None]]:
    out: list[tuple[int, LadderState, LadderState, PolicyEvent, str | None, ProbeId | None]] = []
    for t in ts:
        if t.from_state in POST_HANDOFF_STATES:
            break
        event = _policy_event(t.to_state, t.event.value, None)
        out.append((t.tick, t.from_state, t.to_state, event, None, None))
    return out


def transitions_of(result: RunResultV3) -> tuple[Transition, ...]:
    """The eval transition log of a runner v3 result (sorted by tick, contact, step)."""
    tick_s = float(result.sim.tick_s)
    v2_by_contact: dict[str, list[RunnerTransition]] = {}
    for t in result.transitions:
        v2_by_contact.setdefault(t.contact_id, []).append(t)
    case_of = dict(result.contact_cases)
    probes: dict[str, list[tuple[int, ProbeId]]] = {}
    for call in result.probe_calls:
        if call.cached or case_of.get(call.contact_id, call.case_id) != call.case_id:
            continue
        probes.setdefault(call.contact_id, []).append((call.tick, call.probe_id))
    rows: list[tuple[int, str, int, Transition]] = []
    for cid in sorted(set(v2_by_contact) | set(result.ladder)):
        if cid in result.ladder:
            calls = probes.get(cid, [])
            steps = _ladder_steps(result.ladder[cid], [p for _, p in calls], [t for t, _ in calls])
        else:
            steps = _v2_steps(v2_by_contact[cid])
        for i, (tick, frm, to, event, asset, probe) in enumerate(steps):
            rows.append(
                (
                    tick,
                    cid,
                    i,
                    Transition(
                        seq=0,
                        contact_id=cid,
                        tick=tick,
                        t_s=round6(tick * tick_s),
                        from_state=frm,
                        to_state=to,
                        event=event,
                        asset_id=asset,
                        probe_id=probe,
                    ),
                )
            )
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return tuple(t.model_copy(update={"seq": i}) for i, (_, _, _, t) in enumerate(rows))


def _asset_of(result: RunResultV3, transitions: Sequence[Transition]) -> dict[str, str]:
    """The held asset per contact: the scope on its HOLDING entry, else any scoped action
    of its ladder, else its last ``nearest_asset`` snapshot (never empty for a hold)."""
    out: dict[str, str] = {}
    for t in transitions:
        if t.to_state is S.HOLDING and t.asset_id is not None:
            out.setdefault(t.contact_id, t.asset_id)
    for t in transitions:
        if t.to_state is not S.HOLDING or t.contact_id in out:
            continue
        run = result.ladder.get(t.contact_id)
        scoped = (
            [a.scope.asset_id for a in run.actions if a.scope is not None and a.scope.asset_id]
            if run is not None
            else []
        )
        out[t.contact_id] = scoped[0] if scoped else "unknown_asset"
    return out


class SoundingsPolicy:
    """The real system over runner v3 (signed standing orders, default approver)."""

    name: PolicyName = PolicyName.SOUNDINGS

    def __init__(
        self,
        planner: PlannerArm = "fallback",
        fixture_store: Any | None = None,
        violations_log: Path | None = None,
        *,
        stopping: StoppingConfig | None = None,
        toggles: Sequence[ToggleName | str] = (),
    ) -> None:
        if planner not in ("fallback", "llm"):
            raise ValueError(f"planner must be 'fallback' or 'llm', got {planner!r}")
        self.planner: PlannerArm = planner
        self.fixture_store = fixture_store
        self.violations_log = (
            Path(violations_log) if violations_log is not None else DEFAULT_EVAL_VIOLATIONS
        )
        self.stopping = stopping
        self.toggles = tuple(sorted(ToggleName(t) for t in toggles))

    def run_result(
        self, scenario: Scenario, seed: int, *, sim_likelihoods: LikelihoodSpec | None = None
    ) -> RunResultV3:
        """The raw runner v3 result (the audit log is closed before it is returned)."""
        runner = ScenarioRunnerV3(
            scenario, seed=seed, sim_likelihoods=sim_likelihoods, stopping=self.stopping
        )
        result = runner.run(
            llm_roles=self.planner == "llm",
            violations_path=self.violations_log,
            fixture_store=self.fixture_store,
            toggles=self.toggles,
        )
        if result.audit is not None:
            result.audit.close()
        return result

    def run(
        self, scenario: Scenario, seed: int, *, sim_likelihoods: LikelihoodSpec | None = None
    ) -> PolicyRun:
        result = self.run_result(scenario, seed, sim_likelihoods=sim_likelihoods)
        return self._from_run_result(scenario, result, sim_likelihoods is not None)

    def _from_run_result(
        self, scenario: Scenario, result: RunResultV3, misspecified: bool
    ) -> PolicyRun:
        transitions = transitions_of(result)
        ext = result.sim_extended if result.sim_extended is not None else result.sim
        end_ext = round6(ext.n_ticks * ext.tick_s)
        shutdowns = hold_shutdowns(transitions, end_ext, _asset_of(result, transitions))
        return assemble_run(
            policy=self.name,
            scenario=scenario,
            sim=result.sim,
            transitions=transitions,
            shutdowns=shutdowns,
            variant="misspecified" if misspecified else "nominal",
            planner=self.planner,
            planner_stats=planner_stats(result),
            slow_min=slow_minutes(transitions, end_ext),
        )
