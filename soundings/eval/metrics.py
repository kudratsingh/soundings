"""Per-policy eval metrics over WO-050 ``PolicyRun`` lists (WO-051).

Every number is computed from the WO-050 counting rules (``outcome_for_gt`` rolls a
ground-truth contact up over its observed ids); nothing is re-decided here.

* Denominator (``gt_contacts``): ground-truth contacts of every run, sim false alarms
  excluded (S03 contributes two divers), detected or not.
* ``at_or_below_ask``: a ground-truth contact counts iff its rolled-up terminal state is in
  ``AT_OR_BELOW_ASK_TERMINALS`` and its max rung is ``watch`` or ``ask`` (DECISIONS
  2026-10-16 23:05).
* Rung histogram: max rung per detected ground-truth contact (summed
  ``PolicyRun.rung_histogram``).
* ``missed_threats``: ground-truth contacts ``outcome_for_gt`` marks missed.
* ``false_shutdowns``: contacts (ground truth or sim false alarm) whose shutdown time was
  caused by a benign contact; the false-alarm share is also in ``FalseAlarmMetrics``.
* ``median_s_seen_to_handled``: over handled ground-truth contacts only; ``unhandled``
  counts the ones never handled inside the run (undetected included).
* ``interruptions_per_hour``: summed ``human_interruptions`` over summed ``sim_hours``.
* ``shutdown_usd``: ``shutdown_minutes x costs.shutdown_usd_per_min_default`` (0 until a
  mentor-validated figure exists).

Determinism: pure functions over their inputs; floats leave through ``round6``.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence

from soundings.canonical import round6
from soundings.eval.policies import (
    GtOutcome,
    PlannerStats,
    PolicyName,
    PolicyRun,
    outcome_for_gt,
)
from soundings.schemas import LadderConfig, LadderState, ProbeId, Rung
from soundings.schemas._base import StrictModel

S = LadderState

AT_OR_BELOW_ASK_TERMINALS: frozenset[LadderState] = frozenset(
    {S.CLEARED_AT_ASK, S.RESOLVED_BENIGN, S.HAZARD_NOTICE, S.LOST_VEHICLE}
)
AT_OR_BELOW_ASK_RUNGS: frozenset[Rung] = frozenset({Rung.WATCH, Rung.ASK})
SHUTDOWN_USD_KEY = "shutdown_usd_per_min_default"


def at_or_below_ask(gt: GtOutcome) -> bool:
    """The 70% rule for one rolled-up ground-truth contact (never true for a false alarm)."""
    return (
        not gt.is_false_alarm
        and gt.terminal_state in AT_OR_BELOW_ASK_TERMINALS
        and gt.max_rung in AT_OR_BELOW_ASK_RUNGS
    )


def median_seconds(values: Iterable[float | None]) -> tuple[float | None, int]:
    """Median of the handled values and the count of ``None`` (unhandled) beside it."""
    vals = list(values)
    handled = [v for v in vals if v is not None]
    unhandled = len(vals) - len(handled)
    if not handled:
        return None, unhandled
    return round6(float(statistics.median(handled))), unhandled


def rolled_outcomes(run: PolicyRun) -> list[GtOutcome]:
    """Every ground-truth contact of the run rolled up, in gt id order."""
    return [outcome_for_gt(run, g.gt_id) for g in sorted(run.gt_contacts, key=lambda g: g.gt_id)]


class PolicyMetrics(StrictModel):
    """One row of ``## Outcomes by policy`` (field order is the column order)."""

    policy: PolicyName
    runs: int
    gt_contacts: int
    at_or_below_ask: int
    at_or_below_ask_pct: float
    watch: int
    ask: int
    slow: int
    hold: int
    hand_to_person: int
    missed_threats: int
    false_shutdowns: int
    median_s_seen_to_handled: float | None
    unhandled: int
    interruptions: int
    sim_hours: float
    interruptions_per_hour: float
    shutdown_minutes: float
    shutdown_usd: float
    probes_used: int


class FalseAlarmMetrics(StrictModel):
    """One row of ``## Sim false alarms by policy``: kept out of the denominator."""

    policy: PolicyName
    false_alarms: int
    detected: int
    interruptions: int
    false_shutdowns: int
    shutdown_minutes: float
    probes_used: int


def shutdown_usd_per_min(ladder_cfg: LadderConfig) -> float:
    return float(ladder_cfg.costs.get(SHUTDOWN_USD_KEY, 0.0))


def policy_metrics(
    policy: PolicyName, runs: Sequence[PolicyRun], *, ladder_cfg: LadderConfig
) -> PolicyMetrics:
    """Aggregate one policy's runs (all ``run.policy`` must equal ``policy``)."""
    if any(r.policy is not policy for r in runs):
        raise ValueError(f"runs for {policy} include another policy")
    gts = [o for r in runs for o in rolled_outcomes(r) if not o.is_false_alarm]
    fa = [o for r in runs for o in rolled_outcomes(r) if o.is_false_alarm]
    hist = {rung: sum(r.rung_histogram.get(rung, 0) for r in runs) for rung in Rung}
    counted = sum(1 for o in gts if at_or_below_ask(o))
    median, unhandled = median_seconds(o.seconds_seen_to_handled for o in gts)
    interruptions = sum(r.human_interruptions for r in runs)
    hours = round6(sum(r.sim_hours for r in runs))
    minutes = round6(sum(r.shutdown_minutes for r in runs))
    return PolicyMetrics(
        policy=policy,
        runs=len(runs),
        gt_contacts=len(gts),
        at_or_below_ask=counted,
        at_or_below_ask_pct=round6(100.0 * counted / len(gts)) if gts else 0.0,
        watch=hist[Rung.WATCH],
        ask=hist[Rung.ASK],
        slow=hist[Rung.SLOW],
        hold=hist[Rung.HOLD],
        hand_to_person=hist[Rung.HAND_TO_PERSON],
        missed_threats=sum(1 for o in gts if o.missed),
        false_shutdowns=sum(1 for o in gts + fa if o.false_shutdown),
        median_s_seen_to_handled=median,
        unhandled=unhandled,
        interruptions=interruptions,
        sim_hours=hours,
        interruptions_per_hour=round6(interruptions / hours) if hours > 0 else 0.0,
        shutdown_minutes=minutes,
        shutdown_usd=round6(minutes * shutdown_usd_per_min(ladder_cfg)),
        probes_used=sum(r.probes_used for r in runs),
    )


def false_alarm_metrics(policy: PolicyName, runs: Sequence[PolicyRun]) -> FalseAlarmMetrics:
    fa = [o for r in runs for o in rolled_outcomes(r) if o.is_false_alarm]
    return FalseAlarmMetrics(
        policy=policy,
        false_alarms=len(fa),
        detected=sum(1 for o in fa if o.detected),
        interruptions=sum(o.interruptions for o in fa),
        false_shutdowns=sum(1 for o in fa if o.false_shutdown),
        shutdown_minutes=round6(sum(o.shutdown_minutes for o in fa)),
        probes_used=sum(o.probes_used for o in fa),
    )


def _by_policy(runs: Iterable[PolicyRun]) -> dict[PolicyName, list[PolicyRun]]:
    grouped: dict[PolicyName, list[PolicyRun]] = {}
    for r in runs:
        grouped.setdefault(r.policy, []).append(r)
    return {p: grouped[p] for p in PolicyName if p in grouped}


def compute_metrics(
    runs: Iterable[PolicyRun], *, ladder_cfg: LadderConfig
) -> dict[PolicyName, PolicyMetrics]:
    """Per-policy aggregates, keyed in ``PolicyName`` member order."""
    return {p: policy_metrics(p, rs, ladder_cfg=ladder_cfg) for p, rs in _by_policy(runs).items()}


def compute_false_alarms(runs: Iterable[PolicyRun]) -> dict[PolicyName, FalseAlarmMetrics]:
    return {p: false_alarm_metrics(p, rs) for p, rs in _by_policy(runs).items()}


# ---------------------------------------------------------------------------
# LLM planner vs fallback planner
# ---------------------------------------------------------------------------


class PlannerDifference(StrictModel):
    """One ground-truth contact whose outcome differs between the two planner arms."""

    scenario_id: str
    seed: int
    gt_id: str
    fields: tuple[str, ...]
    fallback_terminal: LadderState | None
    llm_terminal: LadderState | None
    fallback_max_rung: Rung | None
    llm_max_rung: Rung | None
    fallback_probes: tuple[ProbeId, ...]
    llm_probes: tuple[ProbeId, ...]
    fallback_s_seen_to_handled: float | None
    llm_s_seen_to_handled: float | None
    verdict: str


class LlmDiff(StrictModel):
    """Summed planner stats of the LLM arm and every differing ground-truth contact."""

    stats: PlannerStats
    runs_compared: int
    contacts_compared: int
    differences: tuple[PlannerDifference, ...]


DIFF_FIELDS: tuple[str, ...] = (
    "terminal_state",
    "max_rung",
    "probe_sequence",
    "probes_used",
    "seconds_seen_to_handled",
)


def _rung_pos(rung: Rung | None) -> int:
    return -1 if rung is None else tuple(Rung).index(rung)


def _verdict(fb: GtOutcome, llm: GtOutcome) -> str:
    """Whether the LLM arm helped: same terminal state and fewer probes, a lower rung or a
    faster outcome, with nothing worse."""
    if fb.terminal_state != llm.terminal_state:
        return "different terminal state; not counted as help"
    better: list[str] = []
    worse: list[str] = []
    for name, f, g in (
        ("probes", fb.probes_used, llm.probes_used),
        ("rung", _rung_pos(fb.max_rung), _rung_pos(llm.max_rung)),
    ):
        if g < f:
            better.append(f"fewer {name}" if name == "probes" else "lower rung")
        elif g > f:
            worse.append(f"more {name}" if name == "probes" else "higher rung")
    fs, ls = fb.seconds_seen_to_handled, llm.seconds_seen_to_handled
    if fs is not None and ls is not None:
        if ls < fs:
            better.append("faster outcome")
        elif ls > fs:
            worse.append("slower outcome")
    if better and not worse:
        return "helped: " + ", ".join(better)
    if worse and not better:
        return "did not help: " + ", ".join(worse)
    if better and worse:
        return "mixed: " + ", ".join(better + worse)
    return "no effect: same probes count, rung and timing"


def _sum_stats(runs: Iterable[PolicyRun]) -> PlannerStats:
    calls = hits = rejections = fallbacks = 0
    for r in runs:
        if r.planner_stats is None:
            continue
        calls += r.planner_stats.calls
        hits += r.planner_stats.fixture_hits
        rejections += r.planner_stats.guard_rejections
        fallbacks += r.planner_stats.fallbacks
    return PlannerStats(
        calls=calls, fixture_hits=hits, guard_rejections=rejections, fallbacks=fallbacks
    )


def diff_planners(fallback_runs: Iterable[PolicyRun], llm_runs: Iterable[PolicyRun]) -> LlmDiff:
    """Match runs by (scenario, seed, gt contact) and list every contact whose terminal
    state, max rung, probe sequence, probes used or seconds seen to handled differ."""
    llm_list = list(llm_runs)
    fb_index = {(r.scenario_id, r.seed): r for r in fallback_runs}
    llm_index = {(r.scenario_id, r.seed): r for r in llm_list}
    diffs: list[PlannerDifference] = []
    runs_compared = contacts_compared = 0
    for key in sorted(set(fb_index) & set(llm_index)):
        fb_run, llm_run = fb_index[key], llm_index[key]
        runs_compared += 1
        fb_out = {o.gt_id: o for o in rolled_outcomes(fb_run)}
        llm_out = {o.gt_id: o for o in rolled_outcomes(llm_run)}
        for gt_id in sorted(set(fb_out) & set(llm_out)):
            contacts_compared += 1
            fb, llm = fb_out[gt_id], llm_out[gt_id]
            changed = tuple(f for f in DIFF_FIELDS if getattr(fb, f) != getattr(llm, f))
            if not changed:
                continue
            diffs.append(
                PlannerDifference(
                    scenario_id=key[0],
                    seed=key[1],
                    gt_id=gt_id,
                    fields=changed,
                    fallback_terminal=fb.terminal_state,
                    llm_terminal=llm.terminal_state,
                    fallback_max_rung=fb.max_rung,
                    llm_max_rung=llm.max_rung,
                    fallback_probes=fb.probe_sequence,
                    llm_probes=llm.probe_sequence,
                    fallback_s_seen_to_handled=fb.seconds_seen_to_handled,
                    llm_s_seen_to_handled=llm.seconds_seen_to_handled,
                    verdict=_verdict(fb, llm),
                )
            )
    return LlmDiff(
        stats=_sum_stats(llm_list),
        runs_compared=runs_compared,
        contacts_compared=contacts_compared,
        differences=tuple(diffs),
    )
