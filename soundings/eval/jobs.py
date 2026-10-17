"""Eval jobs: one (policy, scenario, seed, sim tables) run each, deduplicated and run in a
process pool (WO-051 part B).

A ``Job`` names its policy by a picklable ``PolicySpec`` (or carries a ready ``Policy``
object, which always runs in this process: an injected factory or a fixture store need
not pickle). ``run_jobs`` runs every distinct job once, in a pool of ``workers`` spawned
processes when that is more than 1, and returns the results in the jobs' own order
(``Executor.map`` keeps order), so the output never depends on scheduling. Each run is
seeded by its own scenario seed; nothing reads the clock. ``eval_workers()`` reads
``SOUNDINGS_EVAL_WORKERS`` (default ``min(cpu_count, 8)``; ``1`` runs in process).

A soundings job that raises inside a sweep is returned as a ``JobError`` (deterministic:
same inputs, same error) so one toggle cannot sink the report; the campaign's main runs
re-raise it.
"""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Literal

from soundings.eval.policies import (
    EvalConfig,
    Policy,
    PolicyName,
    PolicyRun,
    make_policy,
)
from soundings.investigation.machine import StoppingConfig, load_stopping_config
from soundings.schemas import LadderConfig, LikelihoodSpec, Scenario

WORKERS_ENV = "SOUNDINGS_EVAL_WORKERS"
MAX_DEFAULT_WORKERS = 8


@dataclass(frozen=True)
class PolicySpec:
    """A policy by name; soundings options apply to ``PolicyName.SOUNDINGS`` only."""

    name: PolicyName
    planner: Literal["fallback", "llm", "none"] = "none"
    threat_mass_handoff: float | None = None
    violations_log: str | None = None

    @property
    def key(self) -> str:
        return f"{self.name.value}/{self.planner}/{self.threat_mass_handoff}"


@dataclass(frozen=True)
class Job:
    spec: PolicySpec
    scenario: Scenario
    scenario_key: str
    """Distinguishes a scenario variant (``S05``, ``S05+unpinned``, ``S05+fog_no_camera``)."""
    seed: int
    likelihoods: LikelihoodSpec | None = None
    likelihoods_key: str = "config"
    policy: Policy | None = field(default=None, compare=False)
    tolerate_errors: bool = False

    @property
    def key(self) -> tuple[str, str, int, str]:
        return (self.spec.key, self.scenario_key, self.seed, self.likelihoods_key)


@dataclass(frozen=True)
class JobError:
    key: tuple[str, str, int, str]
    error: str


JobResult = PolicyRun | JobError


def eval_workers() -> int:
    raw = os.environ.get(WORKERS_ENV, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(1, min(os.cpu_count() or 1, MAX_DEFAULT_WORKERS))


def stopping_with(threshold: float | None) -> StoppingConfig | None:
    """config/ladder.yaml's stopping block with ``threat_mass_handoff`` overridden (the
    file is never edited)."""
    if threshold is None:
        return None
    return load_stopping_config().model_copy(update={"threat_mass_handoff": float(threshold)})


def build_policy(spec: PolicySpec, ecfg: EvalConfig, ladder: LadderConfig) -> Policy:
    if spec.name is PolicyName.SOUNDINGS:
        from soundings.eval.soundings_policy import SoundingsPolicy

        planner: Literal["fallback", "llm"] = "llm" if spec.planner == "llm" else "fallback"
        return SoundingsPolicy(
            planner=planner,
            fixture_store=None,
            violations_log=None if spec.violations_log is None else Path(spec.violations_log),
            stopping=stopping_with(spec.threat_mass_handoff),
        )
    return make_policy(spec.name, cfg=ecfg, ladder=ladder)


def run_job(job: Job, ecfg: EvalConfig, ladder: LadderConfig) -> JobResult:
    policy = job.policy if job.policy is not None else build_policy(job.spec, ecfg, ladder)
    try:
        return policy.run(job.scenario, job.seed, sim_likelihoods=job.likelihoods)
    except Exception as exc:  # deterministic: same inputs, same error
        if not job.tolerate_errors:
            raise
        return JobError(key=job.key, error=f"{type(exc).__name__}: {exc}"[:200])


def run_jobs(
    jobs: Sequence[Job],
    *,
    ecfg: EvalConfig,
    ladder: LadderConfig,
    workers: int | None = None,
) -> dict[tuple[str, str, int, str], JobResult]:
    """Every distinct job once; results keyed by ``Job.key`` (first job wins a key)."""
    unique: dict[tuple[str, str, int, str], Job] = {}
    for job in jobs:
        unique.setdefault(job.key, job)
    keys = sorted(unique)
    local = [k for k in keys if unique[k].policy is not None]
    remote = [k for k in keys if unique[k].policy is None]
    n = eval_workers() if workers is None else max(1, workers)
    out: dict[tuple[str, str, int, str], JobResult] = {}
    fn = partial(run_job, ecfg=ecfg, ladder=ladder)
    if n > 1 and len(remote) > 1:
        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=min(n, len(remote)), mp_context=ctx) as pool:
            for k, res in zip(remote, pool.map(fn, [unique[k] for k in remote]), strict=True):
                out[k] = res
    else:
        for k in remote:
            out[k] = fn(unique[k])
    for k in local:
        out[k] = fn(unique[k])
    return out
