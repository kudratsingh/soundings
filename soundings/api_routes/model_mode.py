"""``GET /api/model_mode``: which model path is answering, for the judge-panel badge.

WO-087 (CLAUDE.md section 8 A4). Returns ``{mode, live_budget_total,
live_budget_remaining, last_path}``:

* ``mode``: ``MODEL_MODE`` (default ``replay``); an unknown value is reported as given,
  lower-cased, so the badge shows the misconfiguration instead of hiding it;
* ``live_budget_total``: ``LIVE_CALL_BUDGET`` (0 when unset or not a non-negative int);
* ``live_budget_remaining``: what ``artifacts/live_calls.json`` leaves of that total
  (0 when the file is corrupt); reading never writes the file;
* ``last_path``: ``live | replay | fallback`` for the most recent model call in this
  process, ``null`` before any. The badge says "model: live" only after a live reply.
"""

from __future__ import annotations

import os

from fastapi import APIRouter

from soundings.llm.client import last_path
from soundings.llm.live_budget import LiveBudget, budget_total_from_env

router = APIRouter()


def model_mode_status() -> dict[str, object]:
    """The endpoint body (also used by tests and the demo runner)."""
    mode = os.environ.get("MODEL_MODE", "replay").strip().lower() or "replay"
    try:
        total = budget_total_from_env() or 0
    except ValueError:
        total = 0
    budget = LiveBudget(total=total)
    return {
        "mode": mode,
        "live_budget_total": total,
        "live_budget_remaining": budget.remaining,
        "last_path": last_path(),
    }


@router.get("/api/model_mode")
def model_mode() -> dict[str, object]:
    return model_mode_status()


__all__ = ["model_mode", "model_mode_status", "router"]
