"""``python -m soundings.demo --script docs/demo-script.yaml --mode replay`` (WO-062).

Exit codes: 0 every step ran (and met its ``expect_state``); 1 a step missed its
``expect_state``; 2 a bad script or argument; 3 ``--mode record|live`` refused before
anything is loaded (CLAUDE.md 2.3, docs/WORKFLOW.md section 7).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from soundings.demo.schedule import DEFAULT_TAIL_S, DEFAULT_TOTAL_S, Pace, build_schedule
from soundings.demo.script import DemoScript

PAID_MODES = ("record", "live")
REFUSAL = "paid runs need `approve paid run` (docs/WORKFLOW.md §7); the demo runs --mode replay"
EXIT_EXPECT = 1
EXIT_BAD_INPUT = 2
EXIT_REFUSED = 3

app = typer.Typer(add_completion=False, help="Replay the pitch sequence headless, offline.")


@app.command()
def main(
    script: Annotated[Path, typer.Option("--script", help="demo script YAML")],
    mode: Annotated[str, typer.Option("--mode", help="replay (record|live are refused)")] = (
        "replay"
    ),
    pace: Annotated[Pace, typer.Option("--pace", help="realtime | fast | video")] = Pace.REALTIME,
    total_s: Annotated[float, typer.Option("--total-s", help="video pace total")] = (
        DEFAULT_TOTAL_S
    ),
    tail_s: Annotated[float, typer.Option("--tail-s", help="eval card after the last mark")] = (
        DEFAULT_TAIL_S
    ),
    speed: Annotated[float, typer.Option("--speed", help="divides every offset")] = 1.0,
    transcript: Annotated[Path | None, typer.Option("--transcript")] = None,
    eval_report: Annotated[Path | None, typer.Option("--eval-report")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="print the schedule only")] = False,
) -> None:
    if mode in PAID_MODES:
        typer.echo(f"demo: --mode {mode} refused: {REFUSAL}", err=True)
        raise typer.Exit(EXIT_REFUSED)
    if mode != "replay":
        typer.echo(f"demo: unknown --mode {mode!r}; use replay", err=True)
        raise typer.Exit(EXIT_BAD_INPUT)
    os.environ["MODEL_MODE"] = "replay"
    try:
        demo = DemoScript.load(script)
        schedule = build_schedule(
            demo.marks_s, pace=pace, total_s=total_s, tail_s=tail_s, speed=speed
        )
    except (OSError, ValueError, ValidationError) as exc:
        typer.echo(f"demo: bad script or schedule: {exc}", err=True)
        raise typer.Exit(EXIT_BAD_INPUT) from exc
    if dry_run:
        for step, slot in zip(demo.steps, schedule.steps, strict=True):
            typer.echo(f"[{step.t}] at=+{slot.at_s:.6f}s {step.label} | say: {step.say}")
        typer.echo(f"total=+{schedule.total_s:.6f}s pace={schedule.pace.value}")
        return

    from soundings.demo.headless import DEFAULT_EVAL_REPORT, run_demo

    result = run_demo(
        demo,
        pace=pace,
        total_s=total_s,
        tail_s=tail_s,
        speed=speed,
        eval_report=eval_report if eval_report is not None else DEFAULT_EVAL_REPORT,
        emit=typer.echo,
    )
    if transcript is not None:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(result.text(), encoding="utf-8")
    if not result.ok:
        typer.echo("demo: a step missed its expect_state", err=True)
        raise typer.Exit(EXIT_EXPECT)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
