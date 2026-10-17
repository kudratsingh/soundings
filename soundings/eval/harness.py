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
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

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
    PENDING_RUNNER_V3,
    EvalReport,
    MisspecifiedSection,
    build_report,
    inputs_sha256,
    misspecified_section,
)
from soundings.schemas import (
    LadderConfig,
    LikelihoodSpec,
    Scenario,
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
    """``catalog`` -> ``catalog_paths()``; ``twist0N`` -> ``twists/0N/suite.yaml`` (a list
    of scenario paths, relative to that folder or the repo) if present, else
    ``twists/0N/scenarios/*.yaml``. Sorted; unknown or empty suites raise ``SuiteError``."""
    if name == CATALOG_SUITE:
        paths = sorted(catalog_paths())
    else:
        m = _TWIST.match(name)
        if m is None:
            raise SuiteError(f"unknown suite {name!r} (use catalog or twist0N)")
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
) -> EvalReport:
    """Run the campaign and build the report (nothing is written here).

    ``soundings_factory=None`` with ``use_default_soundings`` resolves the real
    ``SoundingsPolicy`` if its module exists; otherwise soundings is pending."""
    ladder = ladder_cfg if ladder_cfg is not None else load_ladder_config()
    ecfg = eval_cfg if eval_cfg is not None else load_eval_config()
    paths = resolve_suite(suite, twists_root=twists_root)
    scenarios = load_suite(paths)
    seed_list = tuple(sorted(set(seeds)))
    factory = soundings_factory
    if factory is None and use_default_soundings:
        factory = default_soundings_factory()

    active: dict[PolicyName, Policy] = {}
    pending: dict[PolicyName, str] = {}
    for name in PolicyName:
        if name not in policies:
            continue
        if name is PolicyName.SOUNDINGS:
            if factory is None:
                pending[name] = PENDING_RUNNER_V3
                continue
            active[name] = factory(
                planner="fallback", fixture_store=None, violations_log=violations_log
            )
        else:
            active[name] = make_policy(name, cfg=ecfg, ladder=ladder)

    runs: list[PolicyRun] = []
    for policy in active.values():
        runs.extend(run_policy(policy, scenarios, seed_list))

    mis: MisspecifiedSection | None = None
    if include_misspecified:
        real = load_likelihood_spec()
        perturbed = perturb_likelihoods(real, perturb_magnitude, perturb_seed)
        unpinned = [unpin_acoustics(sc, real) for sc in scenarios]
        nominal_runs: list[PolicyRun] = []
        mis_runs: list[PolicyRun] = []
        for policy in active.values():
            nominal_runs.extend(run_policy(policy, unpinned, seed_list))
            mis_runs.extend(run_policy(policy, unpinned, seed_list, sim_likelihoods=perturbed))
        mis = misspecified_section(
            magnitude=perturb_magnitude,
            seed=perturb_seed,
            shift=table_shift(real, perturbed),
            nominal_runs=nominal_runs,
            misspecified_runs=mis_runs,
            ladder_cfg=ladder,
        )

    llm_diff: LlmDiff | None = None
    if include_llm_diff and factory is not None and PolicyName.SOUNDINGS in active:
        llm_policy = factory(
            planner="llm", fixture_store=fixture_store, violations_log=violations_log
        )
        fallback_runs = [r for r in runs if r.policy is PolicyName.SOUNDINGS]
        llm_diff = diff_planners(fallback_runs, run_policy(llm_policy, scenarios, seed_list))

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
    return build_report(
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
    )
