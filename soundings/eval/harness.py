"""Eval campaign: suite x seeds x policies, nominal and mis-specified (WO-051).

``run_campaign`` runs every requested policy over the suite's scenarios and seeds,
optionally the mis-specified variant (acoustic pins cleared, sim drawing from
``perturb_likelihoods``, scorer unchanged) and the LLM-planner arm, then builds the
``EvalReport``. Policies come from WO-050 (``make_policy``); the soundings policy comes
from a ``SoundingsFactory``. Until ``soundings.eval.soundings_policy`` exists (WO-051 part
B, over runner v3) the soundings policy is listed as ``pending runner v3``.

Deterministic: explicit seeds, no clock reads, scenarios and seeds in sorted order.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from soundings.canonical import round6
from soundings.eval.families import FAMILIES, FamilyError, family_paths
from soundings.eval.jobs import Job, JobError, JobResult, PolicySpec, run_jobs
from soundings.eval.metrics import LlmDiff, diff_planners
from soundings.eval.perturb import perturb_likelihoods, table_shift, unpin_acoustics
from soundings.eval.policies import (
    POLICIES,
    SOUNDINGS_POLICY_MODULE,
    EvalConfig,
    Policy,
    PolicyName,
    PolicyRun,
    load_eval_config,
    make_policy,
)
from soundings.eval.report import (
    NO_LLM_EFFECT_SENTENCE,
    PENDING_RUNNER_V3,
    EvalReport,
    LlmSection,
    MisspecifiedSection,
    build_report,
    inputs_sha256,
    misspecified_section,
)
from soundings.eval.sweeps import (
    MIS20_MAGNITUDE,
    NO_TOGGLE,
    SEEDS_1_20,
    THRESHOLDS,
    TOGGLES,
    SweepsReport,
    ablation_section,
    misspecified_20,
    scale_pending_reason,
    scale_section,
    seed_section,
    sweep_toggle,
    threshold_section,
    toggle_section,
)
from soundings.schemas import (
    LadderConfig,
    LikelihoodSpec,
    Scenario,
    load_harbor,
    load_ladder_config,
    load_likelihood_spec,
)
from soundings.schemas._base import REPO_ROOT
from soundings.sim.scenario import UnknownScenarioError, catalog_paths, resolve_scenario

TWISTS_ROOT: Path = REPO_ROOT / "twists"
DEFAULT_OUT = Path("artifacts/eval")
CATALOG_SUITE = "catalog"
_TWIST = re.compile(r"^twist(\d\d)$")
_INT = re.compile(r"^\d+$")
_RANGE = re.compile(r"^(\d+)\.\.(\d+)$")

PlannerArm = Literal["fallback", "llm"]


class SuiteError(ValueError):
    """An unknown or empty suite (the CLI exits 2)."""


class SeedsError(ValueError):
    """A malformed ``--seeds`` value (the CLI exits 2)."""


class SoundingsFactory(Protocol):
    """Builds the soundings policy for one planner arm (WO-051 part B wires
    ``soundings.eval.soundings_policy.SoundingsPolicy``)."""

    def __call__(
        self,
        *,
        planner: PlannerArm,
        fixture_store: Any | None,
        violations_log: Path | None,
    ) -> Policy: ...


def _make_soundings(
    *, planner: PlannerArm, fixture_store: Any | None, violations_log: Path | None
) -> Policy:
    return make_policy(
        PolicyName.SOUNDINGS,
        planner=planner,
        fixture_store=fixture_store,
        violations_log=violations_log,
    )


def default_soundings_factory() -> SoundingsFactory | None:
    """The real factory once ``soundings.eval.soundings_policy`` exists, else None."""
    if importlib.util.find_spec(SOUNDINGS_POLICY_MODULE) is None:
        return None
    return _make_soundings


# ---------------------------------------------------------------------------
# Suites and seeds
# ---------------------------------------------------------------------------


def parse_seeds(text: str) -> tuple[int, ...]:
    """``1,2,3,4,5``, ``1..5`` or a mix such as ``5,1..4`` -> sorted unique seeds."""
    seeds: set[int] = set()
    parts = [p.strip() for p in text.split(",")]
    if not parts or any(not p for p in parts):
        raise SeedsError(f"bad --seeds {text!r}")
    for part in parts:
        if _INT.match(part):
            seeds.add(int(part))
            continue
        m = _RANGE.match(part)
        if m is None:
            raise SeedsError(f"bad --seeds item {part!r} (use 1,2,3 or 1..5)")
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi < lo:
            raise SeedsError(f"bad --seeds range {part!r}")
        seeds.update(range(lo, hi + 1))
    return tuple(sorted(seeds))


def _suite_entry(entry: Any, suite_dir: Path) -> Path:
    if not isinstance(entry, str) or not entry:
        raise SuiteError(f"suite entries must be scenario paths, got {entry!r}")
    local = suite_dir / entry
    if local.is_file():
        return local
    repo = REPO_ROOT / entry
    if repo.is_file():
        return repo
    raise SuiteError(f"suite scenario {entry!r} not found")


def resolve_suite(name: str, *, twists_root: Path | None = None) -> list[Path]:
    """``catalog`` -> ``catalog_paths()``; a family name (``soundings.eval.families``:
    tuesday, harbor_night, lost_vehicle, busy, traps) -> that family's files;
    ``twist0N`` -> ``twists/0N/suite.yaml`` (a list of scenario paths, relative to that
    folder or the repo) if present, else ``twists/0N/scenarios/*.yaml``. Sorted; unknown
    or empty suites raise ``SuiteError``."""
    if name == CATALOG_SUITE:
        paths = sorted(catalog_paths())
    elif name in FAMILIES:
        try:
            paths = family_paths(name)
        except FamilyError as exc:
            raise SuiteError(str(exc)) from exc
    else:
        m = _TWIST.match(name)
        if m is None:
            raise SuiteError(
                f"unknown suite {name!r} (use catalog, twist0N or a family: {', '.join(FAMILIES)})"
            )
        root = TWISTS_ROOT if twists_root is None else Path(twists_root)
        folder = root / m.group(1)
        suite_file = folder / "suite.yaml"
        if suite_file.is_file():
            doc = yaml.safe_load(suite_file.read_text(encoding="utf-8"))
            if not isinstance(doc, list):
                raise SuiteError(f"{name}: suite.yaml must be a list of scenario paths")
            paths = sorted({_suite_entry(e, folder) for e in doc})
        else:
            paths = sorted(folder.glob("scenarios/*.yaml"))
    if not paths:
        raise SuiteError(f"suite {name!r} has no scenarios")
    return paths


def default_out(suite: str) -> Path:
    """``artifacts/eval`` for the catalog, ``artifacts/eval/<suite>`` otherwise."""
    return DEFAULT_OUT if suite == CATALOG_SUITE else DEFAULT_OUT / suite


def load_suite(paths: Sequence[Path]) -> list[Scenario]:
    try:
        return [resolve_scenario(p) for p in paths]
    except UnknownScenarioError as exc:
        raise SuiteError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------


def run_policy(
    policy: Policy,
    scenarios: Sequence[Scenario],
    seeds: Sequence[int],
    *,
    sim_likelihoods: LikelihoodSpec | None = None,
) -> list[PolicyRun]:
    return [
        policy.run(sc, seed, sim_likelihoods=sim_likelihoods)
        for sc in sorted(scenarios, key=lambda s: s.id)
        for seed in sorted(seeds)
    ]


def _spec(name: PolicyName, *, planner: str = "fallback", **kw: Any) -> PolicySpec:
    if name is PolicyName.SOUNDINGS:
        return PolicySpec(name=name, planner="llm" if planner == "llm" else "fallback", **kw)
    return PolicySpec(name=name)


def _ok(result: JobResult) -> PolicyRun:
    if isinstance(result, JobError):
        raise RuntimeError(f"eval run {result.key} failed: {result.error}")
    return result


def run_campaign(
    suite: str,
    seeds: Sequence[int],
    *,
    policies: Sequence[PolicyName] = POLICIES,
    perturb_magnitude: float,
    perturb_seed: int,
    include_misspecified: bool = True,
    include_llm_diff: bool = True,
    fixture_store: Any | None = None,
    violations_log: Path | None = None,
    mode: str = "replay",
    soundings_factory: SoundingsFactory | None = None,
    use_default_soundings: bool = True,
    twists_root: Path | None = None,
    ladder_cfg: LadderConfig | None = None,
    eval_cfg: EvalConfig | None = None,
    include_sweeps: bool = False,
    sweep_seeds: Sequence[int] = SEEDS_1_20,
    workers: int | None = None,
) -> EvalReport:
    """Run the campaign and build the report (nothing is written here).

    ``soundings_factory=None`` with ``use_default_soundings`` resolves the real
    ``SoundingsPolicy`` if its module exists; otherwise soundings is pending. With the
    default factory every run is a picklable job (``soundings.eval.jobs``) and runs in a
    process pool of ``workers`` (default ``SOUNDINGS_EVAL_WORKERS`` or ``min(cpus, 8)``);
    an injected factory or a ``fixture_store`` runs those soundings jobs in this process.
    ``include_sweeps`` adds the addendum sections (``soundings.eval.sweeps``): 2a +-20%,
    2b toggles, 2c thresholds, 2d scale (pending without BT-01), 2e ``sweep_seeds``."""
    ladder = ladder_cfg if ladder_cfg is not None else load_ladder_config()
    ecfg = eval_cfg if eval_cfg is not None else load_eval_config()
    paths = resolve_suite(suite, twists_root=twists_root)
    scenarios = sorted(load_suite(paths), key=lambda s: s.id)
    seed_list = tuple(sorted(set(seeds)))
    factory = soundings_factory
    default_factory = factory is None and use_default_soundings
    if default_factory:
        factory = default_soundings_factory()
    vlog = None if violations_log is None else str(violations_log)

    pending: dict[PolicyName, str] = {}
    active: list[PolicyName] = []
    injected: dict[str, Policy] = {}
    for name in PolicyName:
        if name not in policies:
            continue
        if name is PolicyName.SOUNDINGS and factory is None:
            pending[name] = PENDING_RUNNER_V3
            continue
        active.append(name)
    soundings_on = PolicyName.SOUNDINGS in active
    if soundings_on and not default_factory and factory is not None:
        injected["fallback"] = factory(
            planner="fallback", fixture_store=None, violations_log=violations_log
        )
    if soundings_on and include_llm_diff and factory is not None:
        if fixture_store is not None or not default_factory:
            injected["llm"] = factory(
                planner="llm", fixture_store=fixture_store, violations_log=violations_log
            )

    def job(
        name: PolicyName,
        sc: Scenario,
        seed: int,
        *,
        planner: str = "fallback",
        sc_key: str | None = None,
        lk: LikelihoodSpec | None = None,
        lk_key: str = "config",
        threshold: float | None = None,
        tolerate: bool = False,
    ) -> Job:
        spec = _spec(name, planner=planner, threat_mass_handoff=threshold, violations_log=vlog)
        direct = None
        if name is PolicyName.SOUNDINGS and threshold is None:
            direct = injected.get(planner)
        return Job(
            spec=spec,
            scenario=sc,
            scenario_key=sc.id if sc_key is None else sc_key,
            seed=seed,
            likelihoods=lk,
            likelihoods_key=lk_key,
            policy=direct,
            tolerate_errors=tolerate,
        )

    jobs: list[Job] = []
    main = [job(n, sc, s) for n in active for sc in scenarios for s in seed_list]
    jobs += main

    real = load_likelihood_spec()
    unpinned = [unpin_acoustics(sc, real) for sc in scenarios]
    mis_jobs: tuple[list[Job], list[Job]] = ([], [])
    perturbed: LikelihoodSpec | None = None
    if include_misspecified:
        perturbed = perturb_likelihoods(real, perturb_magnitude, perturb_seed)
        pkey = f"perturbed:{round6(perturb_magnitude)}:{perturb_seed}"
        for n in active:
            for sc in unpinned:
                for s in seed_list:
                    mis_jobs[0].append(job(n, sc, s, sc_key=f"{sc.id}+unpinned"))
                    mis_jobs[1].append(
                        job(n, sc, s, sc_key=f"{sc.id}+unpinned", lk=perturbed, lk_key=pkey)
                    )
        jobs += mis_jobs[0] + mis_jobs[1]

    llm_jobs: list[Job] = []
    if include_llm_diff and soundings_on:
        llm_jobs = [
            job(PolicyName.SOUNDINGS, sc, s, planner="llm", tolerate=True)
            for sc in scenarios
            for s in seed_list
        ]
        jobs += llm_jobs

    sweep_jobs: dict[str, list[tuple[Any, Job]]] = {}
    stopping_default = load_ladder_config().stopping.get("threat_mass_handoff")
    if include_sweeps and soundings_on:
        snd = PolicyName.SOUNDINGS
        harbor = load_harbor()
        p20 = perturb_likelihoods(real, MIS20_MAGNITUDE, perturb_seed)
        k20 = f"perturbed:{MIS20_MAGNITUDE}:{perturb_seed}"
        sweep_jobs["mis20_nominal"] = [
            (None, job(snd, sc, s, sc_key=f"{sc.id}+unpinned"))
            for sc in unpinned
            for s in seed_list
        ]
        sweep_jobs["mis20"] = [
            (None, job(snd, sc, s, sc_key=f"{sc.id}+unpinned", lk=p20, lk_key=k20, tolerate=True))
            for sc in unpinned
            for s in seed_list
        ]
        sweep_jobs["toggles"] = [
            (
                (t.value, sc.id),
                job(
                    snd,
                    sweep_toggle(sc, t, harbor),
                    s,
                    sc_key=f"{sc.id}+{t.value}",
                    tolerate=True,
                ),
            )
            for t in TOGGLES
            for sc in scenarios
            for s in seed_list
        ]
        sweep_jobs["thresholds"] = [
            (
                t,
                job(
                    snd,
                    sc,
                    s,
                    threshold=None
                    if stopping_default is not None and float(t) == float(stopping_default)
                    else t,
                    tolerate=True,
                ),
            )
            for t in THRESHOLDS
            for sc in scenarios
            for s in seed_list
        ]
    if include_sweeps and active:
        sweep_jobs["seeds"] = [
            (None, job(n, sc, s)) for n in active for sc in scenarios for s in sweep_seeds
        ]
    for items in sweep_jobs.values():
        jobs += [j for _, j in items]

    results = run_jobs(jobs, ecfg=ecfg, ladder=ladder, workers=workers)
    runs = [_ok(results[j.key]) for j in main]

    mis: MisspecifiedSection | None = None
    if include_misspecified and perturbed is not None:
        mis = misspecified_section(
            magnitude=perturb_magnitude,
            seed=perturb_seed,
            shift=table_shift(real, perturbed),
            nominal_runs=[_ok(results[j.key]) for j in mis_jobs[0]],
            misspecified_runs=[_ok(results[j.key]) for j in mis_jobs[1]],
            ladder_cfg=ladder,
        )

    llm_diff: LlmDiff | None = None
    llm_error: str | None = None
    if llm_jobs:
        llm_results = [results[j.key] for j in llm_jobs]
        errors = sorted({r.error for r in llm_results if isinstance(r, JobError)})
        if errors:
            llm_error = errors[0]
        else:
            fallback_runs = [r for r in runs if r.policy is PolicyName.SOUNDINGS]
            llm_diff = diff_planners(fallback_runs, [_ok(r) for r in llm_results])

    sweeps: SweepsReport | None = None
    if include_sweeps:
        sweeps = _build_sweeps(
            sweep_jobs,
            results,
            scenarios=scenarios,
            seed_list=seed_list,
            sweep_seeds=sweep_seeds,
            perturb_seed=perturb_seed,
            main_soundings=[r for r in runs if r.policy is PolicyName.SOUNDINGS],
            ladder=ladder,
        )

    args = {
        "suite": suite,
        "seeds": list(seed_list),
        "mode": mode,
        "policies": [p.value for p in PolicyName if p in policies],
        "perturb_magnitude": perturb_magnitude,
        "perturb_seed": perturb_seed,
        "include_misspecified": include_misspecified,
        "include_llm_diff": include_llm_diff,
    }
    if include_sweeps:
        args["sweep_seeds"] = sorted(set(sweep_seeds))
    report = build_report(
        suite=suite,
        seeds=seed_list,
        mode=mode,
        inputs_hash=inputs_sha256(paths, args),
        scenarios=scenarios,
        runs=runs,
        ladder_cfg=ladder,
        eval_cfg=ecfg,
        policies_pending=pending,
        misspecified=mis,
        llm_diff=llm_diff,
        sweeps=sweeps,
    )
    if llm_error is not None:
        status = (
            "LLM-planner arm not run: "
            + llm_error
            + " (under SOUNDINGS_CI a replay miss is an error, WORKFLOW 8)."
        )
        report = report.model_copy(
            update={"llm": LlmSection(status=status, diff=None, sentence=NO_LLM_EFFECT_SENTENCE)}
        )
    return report


def _build_sweeps(
    sweep_jobs: Mapping[str, Sequence[tuple[Any, Job]]],
    results: Mapping[tuple[str, str, int, str], JobResult],
    *,
    scenarios: Sequence[Scenario],
    seed_list: Sequence[int],
    sweep_seeds: Sequence[int],
    perturb_seed: int,
    main_soundings: Sequence[PolicyRun],
    ladder: LadderConfig,
) -> SweepsReport:
    def runs_of(name: str) -> list[PolicyRun]:
        return [
            r for _, j in sweep_jobs.get(name, ()) if isinstance(r := results[j.key], PolicyRun)
        ]

    out = SweepsReport()
    if "mis20" in sweep_jobs:
        out = out.model_copy(
            update={
                "misspecified_20": misspecified_20(
                    runs_of("mis20_nominal"),
                    runs_of("mis20"),
                    seeds=seed_list,
                    perturb_seed=perturb_seed,
                    ladder_cfg=ladder,
                )
            }
        )
    if "toggles" in sweep_jobs:
        by_toggle: dict[str, dict[str, list[PolicyRun | str]]] = {NO_TOGGLE: {}}
        for r in main_soundings:
            by_toggle[NO_TOGGLE].setdefault(r.scenario_id, []).append(r)
        for (toggle, sid), j in sweep_jobs["toggles"]:
            res = results[j.key]
            item: PolicyRun | str = res.error if isinstance(res, JobError) else res
            by_toggle.setdefault(toggle, {}).setdefault(sid, []).append(item)
        out = out.model_copy(
            update={"toggles": toggle_section(by_toggle, scenarios, seeds=seed_list)}
        )
    if "thresholds" in sweep_jobs:
        by_t: dict[float, list[PolicyRun]] = {}
        for t, j in sweep_jobs["thresholds"]:
            res = results[j.key]
            if isinstance(res, PolicyRun):
                by_t.setdefault(float(t), []).append(res)
        out = out.model_copy(
            update={"thresholds": threshold_section(by_t, seeds=seed_list, ladder_cfg=ladder)}
        )
        out = out.model_copy(update={"scale": scale_section(scale_pending_reason())})
    out = out.model_copy(update={"ablation": ablation_section()})
    if "seeds" in sweep_jobs:
        out = out.model_copy(
            update={"seeds": seed_section(runs_of("seeds"), seeds=sweep_seeds, ladder_cfg=ladder)}
        )
    return out
