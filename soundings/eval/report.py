"""The eval report: ``report.json`` (canonical JSON) and ``report.md`` (WO-051).

``docs/DECK-INPUTS.md`` pastes the Markdown tables, so their headers are the fixed
``REPORT_COLUMNS`` constants (``tests/test_eval.py::test_columns_stable``). The report
carries no timestamps, hostnames or absolute paths; its header names an
``inputs_sha256`` over ``config/*.yaml``, the suite's scenario files and the CLI
arguments, so two runs on the same inputs are byte-identical.

Sections, in order: header, ``## Outcomes by policy``, ``## Sim false alarms by policy``,
``## Per scenario``, ``## Mis-specified world``, ``## Where the LLM layer added nothing``,
``## Assumptions``. A policy that cannot run yet (the soundings policy until runner v3)
is listed with its reason in every table instead of numbers.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from soundings.canonical import canonical_json, round6
from soundings.eval.metrics import (
    FalseAlarmMetrics,
    LlmDiff,
    PolicyMetrics,
    compute_false_alarms,
    compute_metrics,
)
from soundings.eval.perturb import TableShift
from soundings.eval.policies import EvalConfig, PolicyName, PolicyRun
from soundings.eval.sweeps import (
    BOTH_FALLBACK_SENTENCE,
    SWEEP_COLUMNS,
    WALL_TIME_NOTE,
    SweepsReport,
    render_ablation,
    render_sweeps,
)
from soundings.schemas import LadderConfig, LadderState, Rung, Scenario
from soundings.schemas._base import CONFIG_DIR, REPO_ROOT, StrictModel

REPORT_TITLE = "# Soundings eval report"
ESTIMATE_LABEL = "stress-test estimate"
PENDING_RUNNER_V3 = "pending runner v3"

OUTCOME_COLUMNS: tuple[str, ...] = (
    "policy",
    "gt_contacts",
    "at_or_below_ask_pct",
    "watch",
    "ask",
    "slow",
    "hold",
    "hand_to_person",
    "missed_threats",
    "false_shutdowns",
    "median_s_seen_to_handled",
    "unhandled",
    "interruptions_per_hour",
    "shutdown_minutes",
    "shutdown_usd",
    "probes_used",
)
FALSE_ALARM_COLUMNS: tuple[str, ...] = (
    "policy",
    "false_alarms",
    "detected",
    "interruptions",
    "false_shutdowns",
    "shutdown_minutes",
    "probes_used",
)
PER_SCENARIO_COLUMNS: tuple[str, ...] = (
    "scenario",
    "policy",
    "expected_terminal",
    "terminal_states",
    "matches_expected",
    "missed",
    "false_shutdown",
    "shutdown_minutes",
    "interruptions",
)
MISSPECIFIED_COLUMNS: tuple[str, ...] = ("policy", "metric", "nominal", "misspecified", "delta")
MISSPECIFIED_CHANGE_COLUMNS: tuple[str, ...] = (
    "scenario",
    "seed",
    "policy",
    "nominal_terminal",
    "misspecified_terminal",
)
LLM_DIFF_COLUMNS: tuple[str, ...] = (
    "scenario",
    "seed",
    "gt_contact",
    "fields",
    "fallback_probes",
    "llm_probes",
    "fallback_terminal",
    "llm_terminal",
    "verdict",
)

# --- WO-051 part B begin ---
PER_CONTACT_COLUMNS: tuple[str, ...] = (
    "scenario",
    "seed",
    "policy",
    "contacts",
    "watch",
    "ask",
    "slow",
    "hold",
    "hand_to_person",
)
"""Per observed contact (sim false alarms included): a ``Counter`` of
``ContactOutcome.max_rung`` per run, the number WO-086's rung ledger compares against."""
# --- WO-051 part B end ---

REPORT_COLUMNS: dict[str, tuple[str, ...]] = {
    "outcomes": OUTCOME_COLUMNS,
    "false_alarms": FALSE_ALARM_COLUMNS,
    "per_scenario": PER_SCENARIO_COLUMNS,
    "per_contact": PER_CONTACT_COLUMNS,
    "misspecified": MISSPECIFIED_COLUMNS,
    "misspecified_changes": MISSPECIFIED_CHANGE_COLUMNS,
    **SWEEP_COLUMNS,
    "llm_diff": LLM_DIFF_COLUMNS,
}

MISSPECIFIED_METRICS: tuple[str, ...] = (
    "at_or_below_ask_pct",
    "missed_threats",
    "false_shutdowns",
    "median_s_seen_to_handled",
    "unhandled",
    "interruptions_per_hour",
    "shutdown_minutes",
    "probes_used",
)

NO_LLM_EFFECT_SENTENCE = (
    "No recorded probe_planner fixtures were used in this run; every LLM-planner call fell "
    "back to the deterministic planner, so outcomes are identical by construction. The LLM "
    "layer added nothing measurable here."
)
SAME_PROBES_SENTENCE = "LLM planner chose the same probes as the fallback on every run."
SHARED_TABLES_NOTE = (
    "The simulator and the scorer share config/likelihoods.yaml: the sim samples acoustics "
    "from the tables the scorer reads, so nominal results flatter the scorer. The "
    "mis-specified section draws the sim's acoustics from perturbed tables while the scorer "
    "keeps the real ones."
)
USD_LABEL = (
    "$0: no mentor-validated figure yet (`config/ladder.yaml` "
    "`costs.shutdown_usd_per_min_default = 0`)"
)
HUMAN_QUEUE_SHUTDOWN_NOTE = (
    "human_queue shutdown-minutes are shown as 0 because the person's decisions are not simulated."
)
INTERRUPTION_NOTE = (
    "Interruptions: the eval counts every person-facing event (approval request, handoff, "
    "backup page, eyes-on look or person re-check; WO-050 `count_interruptions`), while a "
    "scenario's `max_human_interruptions` counts approval requests and backup pages "
    "(WO-040 `ActionLedger.interruptions`)."
)
MISSPECIFIED_REFERENCE_NOTE = (
    "Both columns run the suite with acoustic pins cleared (`unpin_acoustics`) so the sim "
    "samples acoustics from a table: `nominal` uses config/likelihoods.yaml, `misspecified` "
    "the perturbed copy. The scorer reads config/likelihoods.yaml in both."
)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class ScenarioRow(StrictModel):
    scenario: str
    policy: PolicyName
    expected_terminal: LadderState
    terminal_states: str
    matches_expected: str
    missed: int
    false_shutdown: int
    shutdown_minutes: float
    interruptions: int


class MetricDelta(StrictModel):
    policy: PolicyName
    metric: str
    nominal: float | None
    misspecified: float | None
    delta: float | None


class TerminalChange(StrictModel):
    scenario: str
    seed: int
    policy: PolicyName
    nominal_terminal: LadderState | None
    misspecified_terminal: LadderState | None


class MisspecifiedSection(StrictModel):
    magnitude: float
    seed: int
    table_shift: TableShift
    nominal: dict[PolicyName, PolicyMetrics]
    misspecified: dict[PolicyName, PolicyMetrics]
    deltas: tuple[MetricDelta, ...]
    terminal_changes: tuple[TerminalChange, ...]


class LlmSection(StrictModel):
    status: str
    diff: LlmDiff | None
    sentence: str


class ContactLedgerRow(StrictModel):
    scenario: str
    seed: int
    policy: PolicyName
    contacts: int
    watch: int
    ask: int
    slow: int
    hold: int
    hand_to_person: int


def per_contact_rows(runs: Sequence[PolicyRun]) -> tuple[ContactLedgerRow, ...]:
    """One row per run: observed contacts by max rung (WO-086's ledger numbers)."""
    order = list(PolicyName)
    rows: list[ContactLedgerRow] = []
    for r in sorted(runs, key=lambda r: (r.scenario_id, r.seed, order.index(r.policy))):
        counts = Counter(c.max_rung for c in r.contacts)
        rows.append(
            ContactLedgerRow(
                scenario=r.scenario_id,
                seed=r.seed,
                policy=r.policy,
                contacts=len(r.contacts),
                watch=counts.get(Rung.WATCH, 0),
                ask=counts.get(Rung.ASK, 0),
                slow=counts.get(Rung.SLOW, 0),
                hold=counts.get(Rung.HOLD, 0),
                hand_to_person=counts.get(Rung.HAND_TO_PERSON, 0),
            )
        )
    return tuple(rows)


class Assumption(StrictModel):
    name: str
    text: str


class EvalReport(StrictModel):
    suite: str
    seeds: tuple[int, ...]
    mode: str
    inputs_sha256: str
    label: str
    policies_run: tuple[PolicyName, ...]
    policies_pending: dict[PolicyName, str]
    scenarios: tuple[str, ...]
    outcomes: dict[PolicyName, PolicyMetrics]
    false_alarms: dict[PolicyName, FalseAlarmMetrics]
    per_scenario: tuple[ScenarioRow, ...]
    misspecified: MisspecifiedSection | None
    llm: LlmSection
    assumptions: tuple[Assumption, ...]
    # --- WO-051 part B begin ---
    methods: dict[str, str] = {}
    per_contact: tuple[ContactLedgerRow, ...] = ()
    sweeps: SweepsReport | None = None
    # --- WO-051 part B end ---


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def inputs_sha256(
    scenario_paths: Sequence[Path], args: Mapping[str, Any], *, config_dir: Path = CONFIG_DIR
) -> str:
    """sha256 over ``config/*.yaml``, the suite's scenario files and the CLI arguments
    (paths are hashed by repo-relative name, never absolute)."""
    h = hashlib.sha256()
    for path in sorted(config_dir.glob("*.yaml")):
        h.update(f"config/{path.name}\n".encode())
        h.update(path.read_bytes())
    for path in sorted(scenario_paths, key=_rel):
        h.update(f"{_rel(path)}\n".encode())
        h.update(path.read_bytes())
    h.update(canonical_json(dict(args)).encode())
    return h.hexdigest()


def _terminal_counts(states: Iterable[LadderState | None]) -> str:
    counts = Counter("UNDETECTED" if s is None else s.value for s in states)
    return ", ".join(f"{name} x{n}" for name, n in sorted(counts.items()))


def per_scenario_rows(
    scenarios: Sequence[Scenario], runs: Sequence[PolicyRun]
) -> tuple[ScenarioRow, ...]:
    rows: list[ScenarioRow] = []
    for sc in sorted(scenarios, key=lambda s: s.id):
        # --- WO-061 begin: a judge-injected live scenario (L-) has no expected terminal ---
        if sc.expected.terminal_state is None:
            continue
        # --- WO-061 end ---
        for policy in PolicyName:
            mine = sorted(
                (r for r in runs if r.scenario_id == sc.id and r.policy is policy),
                key=lambda r: r.seed,
            )
            if not mine:
                continue
            hits = sum(1 for r in mine if r.terminal_state == sc.expected.terminal_state)
            rows.append(
                ScenarioRow(
                    scenario=sc.id,
                    policy=policy,
                    expected_terminal=sc.expected.terminal_state,
                    terminal_states=_terminal_counts(r.terminal_state for r in mine),
                    matches_expected=f"{hits}/{len(mine)}",
                    missed=sum(1 for r in mine if r.missed_threat),
                    false_shutdown=sum(1 for r in mine if r.false_shutdown),
                    shutdown_minutes=round6(sum(r.shutdown_minutes for r in mine)),
                    interruptions=sum(r.human_interruptions for r in mine),
                )
            )
    return tuple(rows)


def _delta(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round6(b - a)


def misspecified_section(
    *,
    magnitude: float,
    seed: int,
    shift: TableShift,
    nominal_runs: Sequence[PolicyRun],
    misspecified_runs: Sequence[PolicyRun],
    ladder_cfg: LadderConfig,
) -> MisspecifiedSection:
    nominal = compute_metrics(nominal_runs, ladder_cfg=ladder_cfg)
    mis = compute_metrics(misspecified_runs, ladder_cfg=ladder_cfg)
    deltas: list[MetricDelta] = []
    for policy in nominal:
        if policy not in mis:
            continue
        for metric in MISSPECIFIED_METRICS:
            a = getattr(nominal[policy], metric)
            b = getattr(mis[policy], metric)
            deltas.append(
                MetricDelta(
                    policy=policy, metric=metric, nominal=a, misspecified=b, delta=_delta(a, b)
                )
            )
    mis_index = {(r.scenario_id, r.seed, r.policy): r for r in misspecified_runs}
    changes = [
        TerminalChange(
            scenario=r.scenario_id,
            seed=r.seed,
            policy=r.policy,
            nominal_terminal=r.terminal_state,
            misspecified_terminal=m.terminal_state,
        )
        for r in nominal_runs
        if (m := mis_index.get((r.scenario_id, r.seed, r.policy))) is not None
        and m.terminal_state != r.terminal_state
    ]
    changes.sort(key=lambda c: (c.scenario, c.seed, list(PolicyName).index(c.policy)))
    return MisspecifiedSection(
        magnitude=round6(magnitude),
        seed=seed,
        table_shift=shift,
        nominal=nominal,
        misspecified=mis,
        deltas=tuple(deltas),
        terminal_changes=tuple(changes),
    )


def llm_section(diff: LlmDiff | None, *, pending: str | None = None) -> LlmSection:
    """The 'Where the LLM layer added nothing' content, computed from planner stats."""
    if diff is None:
        status = (
            f"soundings policy {pending}: no planner runs in this report, so there is no "
            "LLM-planner arm to compare and no fixture was used."
            if pending
            else "LLM-planner comparison not run (--no-llm-diff)."
        )
        return LlmSection(status=status, diff=None, sentence=NO_LLM_EFFECT_SENTENCE)
    if diff.stats.fixture_hits == 0:
        sentence = NO_LLM_EFFECT_SENTENCE
    elif not diff.differences:
        sentence = SAME_PROBES_SENTENCE
    else:
        sentence = (
            f"The LLM planner changed {len(diff.differences)} of {diff.contacts_compared} "
            "ground-truth contact outcomes; each difference and whether it helped is listed "
            "below. Terminal states still come from the stopping rule."
        )
    status = f"compared {diff.runs_compared} runs, {diff.contacts_compared} ground-truth contacts."
    return LlmSection(status=status, diff=diff, sentence=sentence)


def assumptions(eval_cfg: EvalConfig) -> tuple[Assumption, ...]:
    out = [
        Assumption(name="shared_tables", text=SHARED_TABLES_NOTE),
        Assumption(name="baselines", text=eval_cfg.baselines.assumption_note),
    ]
    named = dict(sorted(eval_cfg.baselines.assumptions.items()))
    named.setdefault("human_queue_shutdown", HUMAN_QUEUE_SHUTDOWN_NOTE)
    out.extend(Assumption(name=name, text=text) for name, text in named.items())
    out.extend(
        [
            Assumption(
                name="human_review_s",
                text=f"{eval_cfg.baselines.human_review_s:g} s per request (assumption).",
            ),
            Assumption(
                name="berth_reopen_after_s",
                text=(
                    f"{eval_cfg.baselines.berth_reopen_after_s:g} s after the last contact "
                    "(assumption)."
                ),
            ),
            Assumption(name="shutdown_usd", text=USD_LABEL),
            Assumption(name="interruption_definition", text=INTERRUPTION_NOTE),
        ]
    )
    return tuple(out)


def build_report(
    *,
    suite: str,
    seeds: Sequence[int],
    mode: str,
    inputs_hash: str,
    scenarios: Sequence[Scenario],
    runs: Sequence[PolicyRun],
    ladder_cfg: LadderConfig,
    eval_cfg: EvalConfig,
    policies_pending: Mapping[PolicyName, str] | None = None,
    misspecified: MisspecifiedSection | None = None,
    llm_diff: LlmDiff | None = None,
    sweeps: SweepsReport | None = None,
) -> EvalReport:
    pending = dict(policies_pending or {})
    seeds_text = ", ".join(str(s) for s in sorted(set(seeds)))
    outcomes = compute_metrics(runs, ladder_cfg=ladder_cfg)
    soundings_pending = pending.get(PolicyName.SOUNDINGS)
    return EvalReport(
        suite=suite,
        seeds=tuple(sorted(set(seeds))),
        mode=mode,
        inputs_sha256=inputs_hash,
        label=ESTIMATE_LABEL,
        policies_run=tuple(outcomes),
        policies_pending={p: pending[p] for p in PolicyName if p in pending},
        scenarios=tuple(sorted(s.id for s in scenarios)),
        outcomes=outcomes,
        false_alarms=compute_false_alarms(runs),
        per_scenario=per_scenario_rows(scenarios, runs),
        misspecified=misspecified,
        llm=llm_section(llm_diff, pending=soundings_pending if llm_diff is None else None),
        assumptions=assumptions(eval_cfg),
        methods=table_methods(seeds_text, misspecified is not None),
        per_contact=per_contact_rows(runs),
        sweeps=sweeps,
    )


def table_methods(seeds_text: str, misspecified: bool) -> dict[str, str]:
    """The one-line method under each core table (every table names its seeds)."""
    out = {
        "outcomes": (
            f"Seeds {seeds_text}; every policy over the suite on the same seeded sim; "
            "soundings = runner v3 (signed standing orders, default approver, deterministic "
            "planner); WO-050 counting rules for every policy."
        ),
        "false_alarms": f"Seeds {seeds_text}; observed contacts the sim generated as false alarms.",
        "per_scenario": (
            f"Seeds {seeds_text}; the first ground-truth contact's rolled-up terminal state per "
            "seed against the scenario's expected.terminal_state."
        ),
        "per_contact": (
            f"Seeds {seeds_text}; every observed contact of each run (false alarms included) "
            "counted by its max rung, the numbers the UI rung ledger shows (WO-086)."
        ),
        "llm": (
            f"Seeds {seeds_text}; soundings with the LLM planner (replay only) vs the "
            "deterministic planner, matched by scenario, seed and ground-truth contact."
        ),
    }
    if misspecified:
        out["misspecified"] = (
            f"Seeds {seeds_text}; every policy, acoustic pins cleared, sim tables perturbed "
            "(magnitude and seed below), scorer unchanged."
        )
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.2f}"
    if hasattr(value, "value"):
        return str(value.value)
    if isinstance(value, tuple):
        return " > ".join(_fmt(v) for v in value) if value else "none"
    return str(value)


def _table(columns: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _pending_row(policy: PolicyName, columns: Sequence[str], reason: str) -> list[str]:
    return [policy.value] + [reason] * (len(columns) - 1)


def _metric_rows(report: EvalReport) -> list[list[str]]:
    rows: list[list[str]] = []
    for policy in PolicyName:
        if policy in report.outcomes:
            m = report.outcomes[policy]
            rows.append([_fmt(getattr(m, c)) for c in OUTCOME_COLUMNS])
        elif policy in report.policies_pending:
            rows.append(_pending_row(policy, OUTCOME_COLUMNS, report.policies_pending[policy]))
    return rows


def _method(report: EvalReport, key: str) -> list[str]:
    text = report.methods.get(key)
    return [] if text is None else [f"Method: {text} {WALL_TIME_NOTE}", ""]


def render_markdown(report: EvalReport) -> str:
    out: list[str] = [REPORT_TITLE, ""]
    out.append(
        f"Label: {ESTIMATE_LABEL}. Simulated harbor, demonstration tables; not operational figures."
    )
    out.append("")
    out.append(f"- suite: {report.suite}")
    out.append(f"- seeds: {', '.join(str(s) for s in report.seeds)}")
    out.append(f"- mode: {report.mode}")
    out.append(f"- scenarios: {', '.join(report.scenarios)}")
    out.append(f"- inputs_sha256: {report.inputs_sha256}")
    run_names = ", ".join(p.value for p in report.policies_run) or "none"
    out.append(f"- policies run: {run_names}")
    for policy, reason in report.policies_pending.items():
        out.append(f"- {policy.value}: {reason}")
    out.append("")

    out.append("## Outcomes by policy")
    out.append("")
    out.append(
        "at_or_below_ask_pct counts ground-truth contacts whose terminal state is "
        "CLEARED_AT_ASK, RESOLVED_BENIGN, HAZARD_NOTICE or LOST_VEHICLE at max rung watch or "
        "ask; sim false alarms are not in gt_contacts. watch..hand_to_person is the max rung "
        "per detected ground-truth contact. shutdown_usd: " + USD_LABEL + "."
    )
    out.append("")
    out.extend(_method(report, "outcomes"))
    out.extend(_table(OUTCOME_COLUMNS, _metric_rows(report)))
    out.append("")

    out.append("## Sim false alarms by policy")
    out.append("")
    out.extend(_method(report, "false_alarms"))
    fa_rows: list[list[str]] = []
    for policy in PolicyName:
        if policy in report.false_alarms:
            fa = report.false_alarms[policy]
            fa_rows.append([_fmt(getattr(fa, c)) for c in FALSE_ALARM_COLUMNS])
        elif policy in report.policies_pending:
            fa_rows.append(
                _pending_row(policy, FALSE_ALARM_COLUMNS, report.policies_pending[policy])
            )
    out.extend(_table(FALSE_ALARM_COLUMNS, fa_rows))
    out.append("")

    out.append("## Per scenario")
    out.append("")
    out.append(
        "terminal_states is the first ground-truth contact's rolled-up terminal state per seed."
    )
    out.append("")
    out.extend(_method(report, "per_scenario"))
    out.extend(
        _table(
            PER_SCENARIO_COLUMNS,
            ([_fmt(getattr(r, c)) for c in PER_SCENARIO_COLUMNS] for r in report.per_scenario),
        )
    )
    out.append("")

    out.append("## Per observed contact")
    out.append("")
    out.extend(_method(report, "per_contact"))
    out.extend(
        _table(
            PER_CONTACT_COLUMNS,
            ([_fmt(getattr(r, c)) for c in PER_CONTACT_COLUMNS] for r in report.per_contact),
        )
    )
    out.append("")

    out.append("## Mis-specified world")
    out.append("")
    mis = report.misspecified
    if mis is None:
        out.append("Not run (--no-misspecified).")
    else:
        out.append(f"- perturbation magnitude: {mis.magnitude:.6f}")
        out.append(f"- perturbation seed: {mis.seed}")
        shift = mis.table_shift
        out.append(
            f"- table shift: max {shift.max_abs:.6f}, mean {shift.mean_abs:.6f} "
            f"over {shift.cells} cells"
        )
        out.append("")
        out.append(MISSPECIFIED_REFERENCE_NOTE)
        out.append("")
        out.extend(_method(report, "misspecified"))
        out.extend(
            _table(
                MISSPECIFIED_COLUMNS,
                ([_fmt(getattr(d, c)) for c in MISSPECIFIED_COLUMNS] for d in mis.deltas),
            )
        )
        for policy, reason in report.policies_pending.items():
            out.append("| " + " | ".join(_pending_row(policy, MISSPECIFIED_COLUMNS, reason)) + " |")
        out.append("")
        out.append("Scenarios whose terminal state changed:")
        out.append("")
        if mis.terminal_changes:
            out.extend(
                _table(
                    MISSPECIFIED_CHANGE_COLUMNS,
                    (
                        [_fmt(getattr(c, col)) for col in MISSPECIFIED_CHANGE_COLUMNS]
                        for c in mis.terminal_changes
                    ),
                )
            )
        else:
            out.append("None.")
    out.append("")

    if report.sweeps is not None:
        out.extend(render_sweeps(report.sweeps))

    out.append("## Where the LLM layer added nothing")
    out.append("")
    out.extend(_method(report, "llm"))
    llm = report.llm
    out.append(f"Status: {llm.status}")
    out.append("")
    stats = llm.diff.stats if llm.diff is not None else None
    out.append(f"- calls: {stats.calls if stats else 0}")
    out.append(f"- fixture_hits: {stats.fixture_hits if stats else 0}")
    out.append(f"- guard_rejections: {stats.guard_rejections if stats else 0}")
    out.append(f"- fallbacks: {stats.fallbacks if stats else 0}")
    out.append(f"- differing contacts: {len(llm.diff.differences) if llm.diff else 0}")
    out.append("")
    out.append(llm.sentence)
    ablation = report.sweeps.ablation if report.sweeps is not None else None
    if ablation is not None:
        out.append("")
        out.extend(render_ablation(ablation))
    elif llm.diff is None or llm.diff.stats.fixture_hits == 0:
        out.append("")
        out.append(BOTH_FALLBACK_SENTENCE)
    if llm.diff is not None and llm.diff.differences:
        out.append("")
        out.extend(
            _table(
                LLM_DIFF_COLUMNS,
                (
                    [
                        d.scenario_id,
                        str(d.seed),
                        d.gt_id,
                        ", ".join(d.fields),
                        _fmt(d.fallback_probes),
                        _fmt(d.llm_probes),
                        _fmt(d.fallback_terminal),
                        _fmt(d.llm_terminal),
                        d.verdict,
                    ]
                    for d in llm.diff.differences
                ),
            )
        )
    out.append("")

    out.append("## Assumptions")
    out.append("")
    for a in report.assumptions:
        out.append(f"- {a.name}: {' '.join(a.text.split())}")
    out.append("")
    return "\n".join(out)


def write_report(report: EvalReport, out_dir: str | Path) -> tuple[Path, Path]:
    """Write ``report.md`` (``\\n`` endings) and ``report.json`` (canonical JSON)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path, json_path = out / "report.md", out / "report.json"
    md_path.write_bytes(render_markdown(report).encode("utf-8"))
    json_path.write_bytes((canonical_json(report) + "\n").encode("utf-8"))
    return md_path, json_path
