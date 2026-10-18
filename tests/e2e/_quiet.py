"""WO-060E helpers for the browser smokes: the screen folds every case but the focus behind
one `others · n` row, so a smoke that needs another case's row opens that row first."""

from __future__ import annotations

import time
from typing import Any

OTHERS_FOLDED = "[data-testid=others-row][aria-expanded=false]"


def open_others(page: Any) -> None:
    """Open the `others` row if it is folded (it stays open for the rest of the run)."""
    folded = page.locator(OTHERS_FOLDED)
    if folded.count():
        folded.first.click()


def reveal(page: Any, selector: str, timeout_ms: int = 180_000) -> Any:
    """Wait until `selector` matches a case row, opening the `others` row whenever it is
    folded (a new run folds it again). Returns the locator."""
    loc = page.locator(selector)
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if loc.count():
            return loc
        open_others(page)
        page.wait_for_timeout(250)
    loc.first.wait_for(timeout=1)  # raise the usual timeout error
    return loc
