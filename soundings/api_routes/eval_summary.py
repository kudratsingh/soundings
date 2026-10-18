"""``GET /api/eval/summary`` (WO-062C): the three-policy outcome rows of the eval report.

Read-only. Serves ``artifacts/eval/report.json`` (written by ``make eval``) as one row per
policy with the six numbers the presenter's evidence card shows. Every number is a
stress-test estimate over the simulated catalog (CLAUDE.md 2.6), and the payload says so.
A missing report is a 404 whose body tells the reader to run ``make eval``; a report that
is not JSON or has no ``outcomes`` is a 500 with the reason. Nothing here runs the eval,
calls a model, or feeds anything back into the ladder.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from soundings.schemas._base import REPO_ROOT

REPORT_PATH = REPO_ROOT / "artifacts" / "eval" / "report.json"
LABEL = "stress-test estimate"
POLICIES = ("soundings", "bang", "human_queue")
POLICY_LABELS = {
    "soundings": "Soundings (the ladder)",
    "bang": "close the berth (blunt policy)",
    "human_queue": "send everything to a person",
}
METRICS = (
    "at_or_below_ask_pct",
    "missed_threats",
    "false_shutdowns",
    "median_s_seen_to_handled",
    "interruptions_per_hour",
    "shutdown_minutes",
)
MISSING_DETAIL = "no eval report at artifacts/eval/report.json: run make eval"

router = APIRouter(tags=["eval"])


def get_report_path() -> Path:
    """Where the report lives (tests override this dependency)."""
    return REPORT_PATH


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    """The summary payload from a parsed report (raises ``ValueError`` when malformed)."""
    outcomes = report.get("outcomes")
    if not isinstance(outcomes, dict):
        raise ValueError("eval report has no `outcomes` table")
    rows = []
    for policy in POLICIES:
        row = outcomes.get(policy)
        if not isinstance(row, dict):
            continue
        out: dict[str, Any] = {"policy": policy, "label": POLICY_LABELS[policy]}
        out.update({m: _number(row.get(m)) for m in METRICS})
        rows.append(out)
    if not rows:
        raise ValueError("eval report `outcomes` holds none of the three policies")
    return {
        "label": LABEL,
        "report_label": report.get("label"),
        "mode": report.get("mode"),
        "suite": report.get("suite"),
        "seeds": report.get("seeds"),
        "scenarios": report.get("scenarios"),
        "inputs_sha256": report.get("inputs_sha256"),
        "metrics": list(METRICS),
        "rows": rows,
    }


@router.get("/api/eval/summary")
def eval_summary(path: Annotated[Path, Depends(get_report_path)]) -> Any:
    if not path.is_file():
        return JSONResponse(
            status_code=404, content={"detail": MISSING_DETAIL, "hint": "make eval"}
        )
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("eval report is not a JSON object")
        return summarize(report)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        return JSONResponse(status_code=500, content={"detail": f"eval report unreadable: {exc}"})
