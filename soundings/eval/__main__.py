"""CLI: ``python -m soundings.eval --suite catalog --seeds 1,2,3,4,5 --mode replay
--out artifacts/eval`` (the Makefile line) or ``--seeds 1..5`` with no ``--out``.

Writes ``<out>/report.md`` and ``<out>/report.json``; the eval violations log is
``<out>/violations.md``, never ``docs/log/VIOLATIONS.md``. Defaults for the seeds and the
perturbation come from ``config/eval.yaml`` ``eval:``.

Exit codes: 0 written; 2 bad arguments (unknown suite, malformed seeds, unknown mode);
3 ``--mode record|live`` refused (CLAUDE.md 2.3: eval campaigns are paid runs). A refused
run prints the estimated call count and never constructs a model client.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from soundings.eval.harness import (
    SeedsError,
    SuiteError,
    default_out,
    load_suite,
    parse_seeds,
    resolve_suite,
    run_campaign,
)
from soundings.eval.policies import load_eval_config
from soundings.eval.report import write_report
from soundings.schemas import load_ladder_config

REPLAY = "replay"
PAID_MODES = ("record", "live")
PAID_RUN_REFUSAL = "eval campaigns are paid runs; needs `approve paid run`"
EXIT_BAD_ARGS = 2
EXIT_PAID_REFUSED = 3

app = typer.Typer(add_completion=False, help=__doc__)


def estimated_calls(suite: str, seeds: tuple[int, ...]) -> int:
    """contacts x seeds x ``budgets.llm_calls_per_contact`` for a suite."""
    scenarios = load_suite(resolve_suite(suite))
    per_contact = load_ladder_config().budgets.get("llm_calls_per_contact", 0)
    return sum(len(sc.contacts) for sc in scenarios) * len(seeds) * per_contact


@app.command()
def main(
    suite: Annotated[str, typer.Option("--suite", help="catalog or twist0N")],
    seeds: Annotated[str | None, typer.Option("--seeds", help="1,2,3,4,5 or 1..5")] = None,
    mode: Annotated[str, typer.Option("--mode", help="replay; record and live are refused")] = (
        REPLAY
    ),
    out: Annotated[Path | None, typer.Option("--out", help="output folder")] = None,
    perturb_magnitude: Annotated[float | None, typer.Option("--perturb-magnitude")] = None,
    perturb_seed: Annotated[int | None, typer.Option("--perturb-seed")] = None,
    misspecified: Annotated[bool, typer.Option("--misspecified/--no-misspecified")] = True,
    llm_diff: Annotated[bool, typer.Option("--llm-diff/--no-llm-diff")] = True,
) -> None:
    cfg = load_eval_config()
    try:
        seed_tuple = parse_seeds(seeds) if seeds is not None else tuple(sorted(set(cfg.eval.seeds)))
        if mode in PAID_MODES:
            n = estimated_calls(suite, seed_tuple)
            typer.echo(f"estimated model calls: {n}")
            typer.echo(PAID_RUN_REFUSAL)
            raise typer.Exit(EXIT_PAID_REFUSED)
        if mode != REPLAY:
            raise SuiteError(f"unknown --mode {mode!r} (replay, record or live)")
        os.environ["MODEL_MODE"] = REPLAY
        out_dir = out if out is not None else default_out(suite)
        magnitude = (
            perturb_magnitude if perturb_magnitude is not None else cfg.eval.perturb_magnitude
        )
        pseed = perturb_seed if perturb_seed is not None else cfg.eval.perturb_seed
        report = run_campaign(
            suite,
            seed_tuple,
            perturb_magnitude=magnitude,
            perturb_seed=pseed,
            include_misspecified=misspecified,
            include_llm_diff=llm_diff,
            violations_log=out_dir / "violations.md",
            mode=mode,
            eval_cfg=cfg,
        )
    except (SeedsError, SuiteError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_BAD_ARGS) from exc
    md_path, json_path = write_report(report, out_dir)
    typer.echo(f"wrote {md_path} and {json_path}")


if __name__ == "__main__":
    app()
