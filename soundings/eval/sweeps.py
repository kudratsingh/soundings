"""The owner-directive sweeps of the eval report (WO-051 addendum items 2a-2e and 3).

Each section is built from ``PolicyRun`` results the harness already ran (``jobs.py``);
nothing here runs a scenario. Every table carries its seeds and a one-line method.

* 2a ``misspecified_20``: the soundings policy with acoustic pins cleared, the sim drawing
  from tables perturbed by a factor ``exp(z * ln 1.2)`` (z ~ N(0,1), seeded: one standard
  deviation is +-20%), the scorer on config/likelihoods.yaml; missed threats, false
  shutdowns and the rung histogram, nominal vs mis-specified.
* 2b ``toggles``: every ``ToggleName`` added to every catalog scenario (``with_toggles``);
  terminal states vs ``expected.terminal_state`` and interruptions per simulated hour.
* 2c ``thresholds``: ``stopping.threat_mass_handoff`` 0.25..0.45 step 0.05 through an
  overridden stopping config (config/ladder.yaml is never edited); false shutdowns,
  missed threats and benign ground-truth contacts handed to a person; one PNG.
* 2d ``scale``: BT-01 at 40/100/200 jobs, pending until ``scenarios/BT-01.yaml`` and the
  WO-080 generator are on main (the reason is stated in the report).
* 2e ``seeds``: the catalog over seeds 1-20, every outcome metric as mean and min-max over
  the per-seed values.
* 3 ablation: ``ablation_section()`` runs WO-093's ``soundings.eval.ablation.run_ablation``
  (LLM vs deterministic vs seeded random probe order on S04-S08 and BT-01 when present) and
  the report prints its table and sentence in "Where the LLM layer added nothing"; when the
  module is absent or the run cannot complete, the section says why and that both planner
  paths are the fallback.

Deterministic: sorted iteration, 6-dp rounding, no clock reads. The PNG is written with
fixed metadata.
"""

from __future__ import annotations

import importlib
import importlib.util
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import fmean
from typing import Any

from soundings.canonical import round6
from soundings.eval.families import OPTIONAL_IDS
from soundings.eval.metrics import (
    PolicyMetrics,
    compute_metrics,
    rolled_outcomes,
)
from soundings.eval.policies import PolicyName, PolicyRun, is_threat
from soundings.schemas import LadderConfig, Rung, Scenario, ToggleName
from soundings.schemas._base import StrictModel
from soundings.schemas.geo import distance_m
from soundings.schemas.harbor import Harbor
from soundings.schemas.scenario import Toggle
from soundings.sim.scenario import SCENARIO_ROOT
from soundings.sim.toggles import IMPLEMENTED_TOGGLES

MIS20_MAGNITUDE: float = round6(math.log(1.2))
"""One standard deviation of the multiplicative factor is +-20% (``exp(0.182322)``)."""
THRESHOLDS: tuple[float, ...] = (0.25, 0.3, 0.35, 0.4, 0.45)
SEEDS_1_20: tuple[int, ...] = tuple(range(1, 21))
SCALE_JOBS: tuple[int, ...] = (40, 100, 200)
TOGGLES: tuple[ToggleName, ...] = tuple(sorted(ToggleName, key=lambda t: t.value))
NO_TOGGLE = "none"
THRESHOLD_PNG = "threshold_curve.png"
TIMING_FILE = "timing.json"
WALL_TIME_NOTE = (
    "Wall time: in timing.json beside this report (kept out of report.md and report.json so "
    "two runs stay byte-identical)."
)
SCALE_PNG = "scale_curve.png"
ABLATION_MODULE = "soundings.eval.ablation"
ABLATION_FUNCTION = "ablation_table"
BOTH_FALLBACK_SENTENCE = (
    "Planner ablation (LLM vs deterministic vs random probe order) is pending the recorded "
    "probe_planner fixtures: both planner paths are the deterministic fallback in this run, "
    "so they cannot differ."
)

MIS20_METRICS: tuple[str, ...] = (
    "missed_threats",
    "false_shutdowns",
    "watch",
    "ask",
    "slow",
    "hold",
    "hand_to_person",
    "at_or_below_ask_pct",
)
SEED_METRICS: tuple[str, ...] = (
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

MIS20_COLUMNS: tuple[str, ...] = ("policy", "metric", "nominal", "misspecified", "delta")
TOGGLE_COLUMNS: tuple[str, ...] = (
    "toggle",
    "scenario",
    "expected_terminal",
    "terminal_states",
    "matches_expected",
    "missed_threats",
    "interruptions_per_hour",
)
THRESHOLD_COLUMNS: tuple[str, ...] = (
    "threat_mass_handoff",
    "runs",
    "missed_threats",
    "false_shutdowns",
    "benign_handed_to_person",
    "at_or_below_ask_pct",
    "interruptions_per_hour",
)
SCALE_COLUMNS: tuple[str, ...] = (
    "jobs",
    "seeds",
    "wall_time_s",
    "interruptions_per_hour",
    "cleared_at_ask_pct",
)
SEED_COLUMNS: tuple[str, ...] = ("policy", "metric", "mean", "min", "max", "seeds")
ABLATION_COLUMNS: tuple[str, ...] = (
    "planner",
    "scenario",
    "probes",
    "s_seen_to_handled",
    "missed_threats",
    "interruptions",
    "llm_picks",
)

SWEEP_COLUMNS: dict[str, tuple[str, ...]] = {
    "misspecified_20": MIS20_COLUMNS,
    "toggles": TOGGLE_COLUMNS,
    "thresholds": THRESHOLD_COLUMNS,
    "scale": SCALE_COLUMNS,
    "seeds": SEED_COLUMNS,
    "ablation": ABLATION_COLUMNS,
}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Mis20Row(StrictModel):
    policy: PolicyName
    metric: str
    nominal: float | None
    misspecified: float | None
    delta: float | None


class Mis20Section(StrictModel):
    magnitude: float
    perturb_seed: int
    seeds: tuple[int, ...]
    method: str
    missed_threats_stay_zero: bool
    rows: tuple[Mis20Row, ...]


class ToggleRow(StrictModel):
    toggle: str
    scenario: str
    expected_terminal: str
    terminal_states: str
    matches_expected: str
    missed_threats: int
    interruptions_per_hour: float | None


class ToggleSection(StrictModel):
    seeds: tuple[int, ...]
    method: str
    rows: tuple[ToggleRow, ...]


class ThresholdRow(StrictModel):
    threat_mass_handoff: float
    runs: int
    missed_threats: int
    false_shutdowns: int
    benign_handed_to_person: int
    at_or_below_ask_pct: float | None
    interruptions_per_hour: float | None


class ThresholdSection(StrictModel):
    seeds: tuple[int, ...]
    method: str
    png: str
    rows: tuple[ThresholdRow, ...]


class ScaleRow(StrictModel):
    jobs: int
    seeds: str
    wall_time_s: float | None
    interruptions_per_hour: float | None
    cleared_at_ask_pct: float | None


class ScaleSection(StrictModel):
    status: str
    method: str
    png: str | None
    rows: tuple[ScaleRow, ...]


class SeedStat(StrictModel):
    policy: PolicyName
    metric: str
    mean: float | None
    min: float | None
    max: float | None
    seeds: int


class SeedSection(StrictModel):
    seeds: tuple[int, ...]
    method: str
    rows: tuple[SeedStat, ...]


class SweepsReport(StrictModel):
    misspecified_20: Mis20Section | None = None
    toggles: ToggleSection | None = None
    thresholds: ThresholdSection | None = None
    scale: ScaleSection | None = None
    seeds: SeedSection | None = None
    ablation: AblationSection | None = None


# ---------------------------------------------------------------------------
# Toggle parameters (2b)
# ---------------------------------------------------------------------------

SURGE_N = 5
TOGGLE_PARAMS_NOTE = (
    "parameters: contact_surge n=5 at duration/4; sensor_dropout on the node nearest the "
    "first contact's start at duration/3; contact_goes_quiet on the first contact at "
    "duration/3; the other toggles take no parameters; the sim implements "
    + ", ".join(sorted(t.value for t in IMPLEMENTED_TOGGLES))
    + ", the rest act only where the ladder or the probes read them"
)


def sweep_toggle(scenario: Scenario, name: ToggleName, harbor: Harbor) -> Scenario:
    """``scenario`` plus one toggle, with fixed, stated parameters where the toggle needs
    them (``TOGGLE_PARAMS_NOTE``)."""
    if any(t.name is name for t in scenario.toggles):
        return scenario
    first = scenario.contacts[0] if scenario.contacts else None
    third = scenario.duration_ticks // 3
    params: dict[str, Any] = {"name": name}
    if name is ToggleName.CONTACT_SURGE:
        params.update(n=SURGE_N, at_tick=scenario.duration_ticks // 4)
    elif name is ToggleName.SENSOR_DROPOUT and first is not None:
        node = min(
            sorted(harbor.sensor_nodes, key=lambda n: n.node_id),
            key=lambda n: distance_m(first.start.lat, first.start.lon, n.lat, n.lon),
        )
        params.update(node_id=node.node_id, at_tick=third)
    elif name is ToggleName.CONTACT_GOES_QUIET and first is not None:
        params.update(contact_id=first.id, at_tick=third)
    toggle = Toggle.model_validate(params)
    return scenario.model_copy(update={"toggles": (*scenario.toggles, toggle)})


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seeds_text(seeds: Sequence[int]) -> str:
    s = sorted(set(seeds))
    if s and s == list(range(s[0], s[-1] + 1)) and len(s) > 2:
        return f"{s[0]}..{s[-1]}"
    return ",".join(str(x) for x in s)


def misspecified_20(
    nominal: Sequence[PolicyRun],
    misspecified: Sequence[PolicyRun],
    *,
    seeds: Sequence[int],
    perturb_seed: int,
    ladder_cfg: LadderConfig,
) -> Mis20Section:
    a = compute_metrics(nominal, ladder_cfg=ladder_cfg)
    b = compute_metrics(misspecified, ladder_cfg=ladder_cfg)
    rows: list[Mis20Row] = []
    for policy in sorted(a, key=list(PolicyName).index):
        if policy not in b:
            continue
        for metric in MIS20_METRICS:
            x, y = getattr(a[policy], metric), getattr(b[policy], metric)
            d = None if x is None or y is None else round6(float(y) - float(x))
            rows.append(Mis20Row(policy=policy, metric=metric, nominal=x, misspecified=y, delta=d))
    stay = all(m.missed_threats == 0 for m in b.values())
    method = (
        f"Seeds {_seeds_text(seeds)}; soundings over the catalog with acoustic pins cleared; "
        f"sim tables x exp({MIS20_MAGNITUDE} z), z ~ N(0,1) seeded {perturb_seed} (+-20% at one "
        "sigma), renormalized; the scorer keeps config/likelihoods.yaml. Baselines ignore "
        "acoustics, so they are not rerun."
    )
    return Mis20Section(
        magnitude=MIS20_MAGNITUDE,
        perturb_seed=perturb_seed,
        seeds=tuple(sorted(set(seeds))),
        method=method,
        missed_threats_stay_zero=stay,
        rows=tuple(rows),
    )


def _terminal_counts(states: Sequence[str]) -> str:
    counts = Counter(states)
    return ", ".join(f"{k} x{n}" for k, n in sorted(counts.items()))


def _per_hour(runs: Sequence[PolicyRun]) -> float | None:
    hours = sum(r.sim_hours for r in runs)
    if hours <= 0:
        return None
    return round6(sum(r.human_interruptions for r in runs) / hours)


def toggle_section(
    runs_by_toggle: Mapping[str, Mapping[str, Sequence[PolicyRun | str]]],
    scenarios: Sequence[Scenario],
    *,
    seeds: Sequence[int],
) -> ToggleSection:
    """``runs_by_toggle[toggle][scenario_id]`` = runs (or an error string) per seed."""
    expected = {sc.id: sc.expected.terminal_state.value for sc in scenarios}
    rows: list[ToggleRow] = []
    for toggle in [NO_TOGGLE, *(t.value for t in TOGGLES)]:
        by_sc = runs_by_toggle.get(toggle)
        if by_sc is None:
            continue
        all_runs: list[PolicyRun] = []
        all_states: list[str] = []
        hits = total = missed = 0
        for sid in sorted(by_sc):
            items = by_sc[sid]
            runs = [r for r in items if isinstance(r, PolicyRun)]
            states = [
                (r.terminal_state.value if r.terminal_state is not None else "UNDETECTED")
                if isinstance(r, PolicyRun)
                else f"error ({r.split(':', 1)[0]})"
                for r in items
            ]
            n_hit = sum(1 for s in states if s == expected[sid])
            n_missed = sum(1 for r in runs if r.missed_threat)
            rows.append(
                ToggleRow(
                    toggle=toggle,
                    scenario=sid,
                    expected_terminal=expected[sid],
                    terminal_states=_terminal_counts(states),
                    matches_expected=f"{n_hit}/{len(items)}",
                    missed_threats=n_missed,
                    interruptions_per_hour=_per_hour(runs),
                )
            )
            all_runs.extend(runs)
            all_states.extend(states)
            hits += n_hit
            total += len(items)
            missed += n_missed
        rows.append(
            ToggleRow(
                toggle=toggle,
                scenario="all",
                expected_terminal="per scenario",
                terminal_states=f"{len(all_runs)} runs, {total - len(all_runs)} errors",
                matches_expected=f"{hits}/{total}",
                missed_threats=missed,
                interruptions_per_hour=_per_hour(all_runs),
            )
        )
    method = (
        f"Seeds {_seeds_text(seeds)}; soundings over the catalog with one twist toggle added "
        "per row (sim and ladder both see it; `none` is the untoggled catalog; "
        + TOGGLE_PARAMS_NOTE
        + "); matches_expected compares the first ground-truth contact's terminal state "
        "with the scenario's expected.terminal_state, which is written for the untoggled "
        "scenario; interruptions per simulated hour, WO-050 counting."
    )
    return ToggleSection(seeds=tuple(sorted(set(seeds))), method=method, rows=tuple(rows))


def _benign_handed(runs: Sequence[PolicyRun]) -> int:
    n = 0
    for r in runs:
        for g in rolled_outcomes(r):
            if g.is_false_alarm or is_threat(g.truth) or not g.detected:
                continue
            if g.max_rung is Rung.HAND_TO_PERSON:
                n += 1
    return n


def threshold_section(
    runs_by_threshold: Mapping[float, Sequence[PolicyRun]],
    *,
    seeds: Sequence[int],
    ladder_cfg: LadderConfig,
    png: str = THRESHOLD_PNG,
) -> ThresholdSection:
    rows: list[ThresholdRow] = []
    for t in sorted(runs_by_threshold):
        runs = list(runs_by_threshold[t])
        m = compute_metrics(runs, ladder_cfg=ladder_cfg).get(PolicyName.SOUNDINGS)
        rows.append(
            ThresholdRow(
                threat_mass_handoff=round6(t),
                runs=len(runs),
                missed_threats=0 if m is None else m.missed_threats,
                false_shutdowns=0 if m is None else m.false_shutdowns,
                benign_handed_to_person=_benign_handed(runs),
                at_or_below_ask_pct=None if m is None else m.at_or_below_ask_pct,
                interruptions_per_hour=None if m is None else m.interruptions_per_hour,
            )
        )
    method = (
        f"Seeds {_seeds_text(seeds)}; soundings over the catalog with "
        "stopping.threat_mass_handoff set per row through an overridden stopping config "
        "passed to the investigation (config/ladder.yaml unchanged, 0.35 in force); "
        "report only, not a tuning step. Curve: " + png + "."
    )
    return ThresholdSection(
        seeds=tuple(sorted(set(seeds))), method=method, png=png, rows=tuple(rows)
    )


def scale_pending_reason(scenario_root: Path | None = None) -> str | None:
    """Why 2d cannot run on this checkout (None when BT-01 and the generator exist)."""
    root = SCENARIO_ROOT if scenario_root is None else scenario_root
    missing = []
    if not any((root / f"{sid}.yaml").is_file() for sid in sorted(OPTIONAL_IDS)):
        missing.append("scenarios/BT-01.yaml")
    if importlib.util.find_spec("soundings.sim.generate") is None:
        missing.append("soundings.sim.generate")
    if not missing:
        return None
    return (
        "pending: "
        + " and ".join(missing)
        + " not on main yet (WO-080 part A); BT-01 at 40/100/200 jobs runs once they merge"
    )


SCALE_DEFERRED = (
    "pending: scenarios/BT-01.yaml and soundings.sim.generate landed on main (WO-080 part A) "
    "after this build's 2d gate; BT-01 at 40/100/200 jobs is a follow-up that needs WO-080 "
    "part B tick skipping to fit make eval's 5-minute budget (one 40-job day is tens of "
    "seconds to minutes of runner v3 on a shared machine)"
)


def scale_section(reason: str | None) -> ScaleSection:
    """2d. ``reason`` is ``scale_pending_reason()``; with BT-01 and the generator present the
    section is still pending (``SCALE_DEFERRED``) until the runs are wired."""
    method = (
        "BT-01 generated at 40, 100 and 200 jobs (WO-080 generator), soundings; "
        "interruptions per simulated hour and the share of filed contacts cleared at ask "
        "(wall time per size in timing.json)."
    )
    status = reason if reason is not None else SCALE_DEFERRED
    return ScaleSection(status=status, method=method, png=None, rows=())


def seed_section(
    runs: Sequence[PolicyRun], *, seeds: Sequence[int], ladder_cfg: LadderConfig
) -> SeedSection:
    per_seed: dict[int, dict[PolicyName, PolicyMetrics]] = {}
    for s in sorted(set(seeds)):
        mine = [r for r in runs if r.seed == s]
        per_seed[s] = compute_metrics(mine, ladder_cfg=ladder_cfg)
    rows: list[SeedStat] = []
    for policy in PolicyName:
        for metric in SEED_METRICS:
            vals = [
                float(getattr(per_seed[s][policy], metric))
                for s in sorted(per_seed)
                if policy in per_seed[s] and getattr(per_seed[s][policy], metric) is not None
            ]
            if not any(policy in per_seed[s] for s in per_seed):
                continue
            rows.append(
                SeedStat(
                    policy=policy,
                    metric=metric,
                    mean=round6(fmean(vals)) if vals else None,
                    min=round6(min(vals)) if vals else None,
                    max=round6(max(vals)) if vals else None,
                    seeds=len(vals),
                )
            )
    method = (
        f"Seeds {_seeds_text(seeds)}; every policy over the catalog; each metric computed per "
        "seed (as in Outcomes by policy), then the mean and min-max over seeds; `seeds` counts "
        "the seeds where the metric is defined."
    )
    return SeedSection(seeds=tuple(sorted(set(seeds))), method=method, rows=tuple(rows))


# ---------------------------------------------------------------------------
# Ablation hook (WO-093)
# ---------------------------------------------------------------------------


ABLATION_SCENARIOS = "S04-S08"
ABLATION_DEFERRED: tuple[str, ...] = ("BT-01",)
"""Listed as pending in ``make eval``: an 8-hour, 400-contact day x 3 arms is minutes of
runner v3 without WO-080 part B tick skipping; ``python -m soundings.eval.ablation
--scenarios S04-S08,BT-01`` runs it on its own."""


class AblationRowOut(StrictModel):
    planner: str
    scenario: str
    probes: int
    s_seen_to_handled: float | None
    missed_threats: int
    interruptions: int
    llm_picks: int


class AblationSection(StrictModel):
    status: str
    method: str
    sentence: str
    pending: tuple[str, ...]
    rows: tuple[AblationRowOut, ...]


def ablation_section() -> AblationSection | None:
    """WO-093's planner ablation as a report section (None when the module is absent)."""
    if importlib.util.find_spec(ABLATION_MODULE) is None:
        return None
    module = importlib.import_module(ABLATION_MODULE)
    method = str(getattr(module, "METHOD", "")).removeprefix("method: ")
    try:
        ids, pending = module.expand_scenarios(ABLATION_SCENARIOS)
        pending = tuple(pending) + ABLATION_DEFERRED
        rep = module.run_ablation(scenarios=ids, pending=pending)
        sentence = str(module.ablation_sentence(rep))
    except Exception as exc:  # e.g. a replay miss under SOUNDINGS_CI (WORKFLOW 8)
        return AblationSection(
            status=f"not run: {type(exc).__name__} ({str(exc)[:120]})",
            method=method,
            sentence=BOTH_FALLBACK_SENTENCE,
            pending=(),
            rows=(),
        )
    rows = tuple(
        AblationRowOut(
            planner=r.planner,
            scenario=r.scenario,
            probes=r.probes,
            s_seen_to_handled=r.s_seen_to_handled,
            missed_threats=r.missed_threats,
            interruptions=r.interruptions,
            llm_picks=r.llm_choices,
        )
        for r in rep.rows
    )
    return AblationSection(
        status=f"ran {len(rows)} runs; llm fixture hits {rep.llm_fixture_hits}",
        method=method,
        sentence=sentence,
        pending=tuple(rep.pending),
        rows=rows,
    )


def render_ablation(section: AblationSection) -> list[str]:
    out = ["Planner ablation (LLM vs deterministic vs random probe order):", ""]
    out += [f"Method: {section.method}. {WALL_TIME_NOTE}", "", f"Status: {section.status}", ""]
    if section.pending:
        out += [f"Pending (not on this checkout): {', '.join(section.pending)}", ""]
    if section.rows:
        out += _table(
            ABLATION_COLUMNS,
            [[_fmt(getattr(r, c)) for c in ABLATION_COLUMNS] for r in section.rows],
        )
        out.append("")
    out.append(section.sentence)
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
    return str(value)


def _table(columns: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    lines.extend("| " + " | ".join(r) + " |" for r in rows)
    return lines


def render_sweeps(sw: SweepsReport) -> list[str]:
    out: list[str] = []
    if sw.misspecified_20 is not None:
        m = sw.misspecified_20
        out += ["## Mis-specified sim at +-20%", "", f"Method: {m.method} {WALL_TIME_NOTE}", ""]
        out.append(f"- missed threats stay 0: {_fmt(m.missed_threats_stay_zero)}")
        out.append("")
        out += _table(MIS20_COLUMNS, [[_fmt(getattr(r, c)) for c in MIS20_COLUMNS] for r in m.rows])
        out.append("")
    if sw.toggles is not None:
        t = sw.toggles
        out += ["## Toggle sweep", "", f"Method: {t.method} {WALL_TIME_NOTE}", ""]
        out += _table(
            TOGGLE_COLUMNS, [[_fmt(getattr(r, c)) for c in TOGGLE_COLUMNS] for r in t.rows]
        )
        out.append("")
    if sw.thresholds is not None:
        th = sw.thresholds
        out += ["## Threshold sensitivity", "", f"Method: {th.method} {WALL_TIME_NOTE}", ""]
        out += _table(
            THRESHOLD_COLUMNS,
            [[_fmt(getattr(r, c)) for c in THRESHOLD_COLUMNS] for r in th.rows],
        )
        out.append("")
    if sw.scale is not None:
        sc = sw.scale
        out += ["## Scale", "", f"Method: {sc.method} {WALL_TIME_NOTE}", ""]
        out += [f"Status: {sc.status}", ""]
        out += _table(
            SCALE_COLUMNS, [[_fmt(getattr(r, c)) for c in SCALE_COLUMNS] for r in sc.rows]
        )
        out.append("")
    if sw.seeds is not None:
        se = sw.seeds
        out += ["## Seeds 1-20", "", f"Method: {se.method} {WALL_TIME_NOTE}", ""]
        out += _table(SEED_COLUMNS, [[_fmt(getattr(r, c)) for c in SEED_COLUMNS] for r in se.rows])
        out.append("")
    return out


def write_threshold_png(section: ThresholdSection, path: Path) -> Path:
    """False shutdowns, missed threats and benign hand-offs vs the threshold (one PNG)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    xs = [r.threat_mass_handoff for r in section.rows]
    series = (
        ("missed threats", [r.missed_threats for r in section.rows], "#2a78d6", "o"),
        ("false shutdowns", [r.false_shutdowns for r in section.rows], "#eb6834", "s"),
        (
            "benign contacts handed to a person",
            [r.benign_handed_to_person for r in section.rows],
            "#1baf7a",
            "^",
        ),
    )
    fig, ax = plt.subplots(figsize=(7.0, 4.2), dpi=120)
    fig.patch.set_facecolor("#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    # Drawn last-to-first so the first series (missed threats) stays visible when lines
    # coincide; it is dashed for the same reason.
    for i, (label, ys, color, marker) in reversed(list(enumerate(series))):
        ax.plot(
            xs,
            ys,
            color=color,
            linewidth=2,
            linestyle="--" if i == 0 else "-",
            marker=marker,
            markersize=8 if i == 0 else 7,
            label=label,
            zorder=3 + (len(series) - i),
        )
    ax.axvline(0.35, color="#b5b4ae", linewidth=1, linestyle=":")
    ax.set_xlabel("stopping.threat_mass_handoff (0.35 in force)", color="#52514e")
    ax.set_ylabel("contacts (catalog, seeds " + _seeds_text(section.seeds) + ")", color="#52514e")
    ax.set_title(
        "Threshold sensitivity (stress-test estimate, simulated)", color="#0b0b0b", fontsize=11
    )
    ax.grid(axis="y", color="#e7e6e2", linewidth=0.8)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.set_xticks(xs)
    top = max(max(ys) for _, ys, _, _ in series)
    ax.set_ylim(-0.5, top + 1.5)
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(
        handles[::-1],
        labels[::-1],
        frameon=False,
        fontsize=8,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.2),
        ncol=3,
    )
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, format="png", metadata={"Software": None})
    plt.close(fig)
    return path


SweepsReport.model_rebuild()
