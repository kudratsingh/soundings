"""CLI: ``python -m soundings.eval --suite catalog --seeds 1,2,3,4,5 --mode replay
--out artifacts/eval`` (the Makefile line) or ``--seeds 1..5`` with no ``--out``.

Every run first writes an immutable archive ``<out>/runs/<stamp>-<key>/`` (``report.md``,
``report.json``, ``config.json``, ``manifest.json``; see ``soundings.eval.archive``), then
refreshes ``<out>/report.md`` and ``<out>/report.json`` as the latest copies. The eval
violations log is ``<out>/violations.md``, never ``docs/log/VIOLATIONS.md``. Defaults for
the seeds and the perturbation come from ``config/eval.yaml`` ``eval:``.

``--suite`` takes ``catalog``, ``twist0N`` or a family (``soundings.eval.families``);
``--suite traps`` runs the known-answer trap calibration (``soundings.eval.traps``).
``--stamp YYYYMMDDTHHMMSSZ`` fixes the archive stamp (default: the current UTC time).

Exit codes: 0 written; 2 bad arguments (unknown suite, malformed seeds, unknown mode,
bad stamp); 3 ``--mode record|live`` refused (CLAUDE.md 2.3: eval campaigns are paid
runs); 4 the archive folder for this stamp and these inputs already exists (nothing is
overwritten). A refused run prints the estimated call count and never constructs a model
client.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from soundings.canonical import canonical_json
from soundings.eval.archive import (
    ArchiveExistsError,
    StampError,
    check_stamp,
    utc_stamp,
    write_archive,
)
from soundings.eval.harness import (
    SeedsError,
    SuiteError,
    default_out,
    load_suite,
    parse_seeds,
    resolve_suite,
    run_campaign,
)
from soundings.eval.policies import EvalConfig, load_eval_config
from soundings.eval.report import render_markdown
from soundings.eval.traps import (
    TRAP_MANIFEST,
    render_trap_markdown,
    run_traps,
    trap_paths,
)
from soundings.schemas import load_ladder_config
from soundings.schemas._base import CONFIG_DIR

REPLAY = "replay"
PAID_MODES = ("record", "live")
PAID_RUN_REFUSAL = "eval campaigns are paid runs; needs `approve paid run`"
EXIT_BAD_ARGS = 2
EXIT_PAID_REFUSED = 3
EXIT_ARCHIVE_EXISTS = 4
TRAPS_SUITE = "traps"

app = typer.Typer(add_completion=False, help=__doc__)


def estimated_calls(suite: str, seeds: tuple[int, ...]) -> int:
    """contacts x seeds x ``budgets.llm_calls_per_contact`` for a suite."""
    scenarios = load_suite(resolve_suite(suite))
    per_contact = load_ladder_config().budgets.get("llm_calls_per_contact", 0)
    return sum(len(sc.contacts) for sc in scenarios) * len(seeds) * per_contact


@app.command()
def main(
    suite: Annotated[str, typer.Option("--suite", help="catalog, twist0N or a family")],
    seeds: Annotated[str | None, typer.Option("--seeds", help="1,2,3,4,5 or 1..5")] = None,
    mode: Annotated[str, typer.Option("--mode", help="replay; record and live are refused")] = (
        REPLAY
    ),
    out: Annotated[Path | None, typer.Option("--out", help="output folder")] = None,
    perturb_magnitude: Annotated[float | None, typer.Option("--perturb-magnitude")] = None,
    perturb_seed: Annotated[int | None, typer.Option("--perturb-seed")] = None,
    misspecified: Annotated[bool, typer.Option("--misspecified/--no-misspecified")] = True,
    llm_diff: Annotated[bool, typer.Option("--llm-diff/--no-llm-diff")] = True,
    stamp: Annotated[
        str | None, typer.Option("--stamp", help="archive stamp YYYYMMDDTHHMMSSZ (UTC)")
    ] = None,
) -> None:
    cfg = load_eval_config()
    try:
        run_stamp = check_stamp(stamp) if stamp is not None else utc_stamp()
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
        args: dict[str, object] = {
            "suite": suite,
            "seeds": list(seed_tuple),
            "mode": mode,
        }
        if suite == TRAPS_SUITE:
            traps = run_traps(seed_tuple, mode=mode)
            md, js, inputs_hash = (
                render_trap_markdown(traps),
                canonical_json(traps) + "\n",
                traps.inputs_sha256,
            )
            inputs = [TRAP_MANIFEST, *trap_paths()]
        else:
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
            md, js, inputs_hash = (
                render_markdown(report),
                canonical_json(report) + "\n",
                report.inputs_sha256,
            )
            inputs = list(resolve_suite(suite))
            args.update(
                perturb_magnitude=magnitude,
                perturb_seed=pseed,
                include_misspecified=misspecified,
                include_llm_diff=llm_diff,
            )
    except (SeedsError, SuiteError, StampError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_BAD_ARGS) from exc
    try:
        archived = write_archive(
            out_dir,
            stamp=run_stamp,
            suite=suite,
            seeds=seed_tuple,
            mode=mode,
            inputs_sha256=inputs_hash,
            input_paths=[*sorted(CONFIG_DIR.glob("*.yaml")), *inputs],
            config_snapshot=config_snapshot(cfg, args),
            report_md=md,
            report_json=js,
        )
    except ArchiveExistsError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ARCHIVE_EXISTS) from exc
    typer.echo(f"archived {archived.folder}")
    typer.echo(f"wrote {archived.latest_md} and {archived.latest_json}")


def config_snapshot(cfg: EvalConfig, args: dict[str, object]) -> dict[str, object]:
    """The resolved configuration a run used: CLI arguments after defaults, the eval
    config and the ladder config (stopping, budgets, costs) as loaded."""
    return {"args": args, "eval": cfg, "ladder": load_ladder_config()}


if __name__ == "__main__":
    app()
