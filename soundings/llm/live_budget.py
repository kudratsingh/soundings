"""The stage live-call budget: a hard cap on hosted-model calls, persisted across clients.

WO-087 (CLAUDE.md 2.3, section 8 A4). ``MODEL_MODE=live`` spends one unit of a
``LiveBudget`` before every transport call and refuses the call once nothing is left; the
caller then gets its deterministic fallback. The count lives in
``artifacts/live_calls.json`` (``{"total": n, "spent": k}``), written atomically through a
temporary sibling and ``os.replace``, so a restarted server or a fresh ``ModelClient``
resumes the same count instead of starting over.

A file that cannot be read as that shape (truncated, edited, wrong types) counts as
**exhausted**, never as fresh: a broken counter must not hand out a new budget. The
broken file is left in place for a person to inspect; nothing overwrites it.

``total`` comes from the caller (the owner's approval phrase sets ``LIVE_CALL_BUDGET``);
``spent`` comes from the file. Nothing here reads a clock or the network.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Final

__all__ = [
    "DEFAULT_LIVE_CALLS_PATH",
    "LIVE_CALLS_PATH_ENV",
    "LIVE_CALL_BUDGET_ENV",
    "LiveBudget",
    "budget_total_from_env",
    "live_calls_path",
]

REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent
DEFAULT_LIVE_CALLS_PATH: Final = REPO_ROOT / "artifacts" / "live_calls.json"
LIVE_CALLS_PATH_ENV: Final = "SOUNDINGS_LIVE_CALLS_PATH"
LIVE_CALL_BUDGET_ENV: Final = "LIVE_CALL_BUDGET"


def live_calls_path() -> Path:
    """``SOUNDINGS_LIVE_CALLS_PATH`` when set (non-empty), else ``artifacts/live_calls.json``."""
    override = os.environ.get(LIVE_CALLS_PATH_ENV, "").strip()
    return Path(override) if override else DEFAULT_LIVE_CALLS_PATH


def budget_total_from_env() -> int | None:
    """``LIVE_CALL_BUDGET`` as a non-negative int; ``None`` when unset or empty.

    A value that is not a non-negative integer raises ``ValueError`` (a typo must not
    become an uncapped or a surprise budget).
    """
    raw = os.environ.get(LIVE_CALL_BUDGET_ENV, "").strip()
    if not raw:
        return None
    try:
        total = int(raw)
    except ValueError:
        raise ValueError(f"{LIVE_CALL_BUDGET_ENV}={raw!r} is not an integer") from None
    if total < 0:
        raise ValueError(f"{LIVE_CALL_BUDGET_ENV}={total} is negative")
    return total


class LiveBudget:
    """At most ``total`` live calls, counted in a JSON file shared by every client.

    ``spend()`` takes one unit and persists it before the caller makes the call, so a
    call that times out or fails still counts (it may have been billed). It returns
    ``False`` and changes nothing once the budget is spent or the file is corrupt.
    """

    def __init__(self, path: Path | None = None, total: int = 0) -> None:
        if total < 0:
            raise ValueError("total must be >= 0")
        self.path = Path(path) if path is not None else live_calls_path()
        self.total = int(total)
        self._lock = threading.Lock()
        self.corrupt = False
        self._spent = self._restore()

    @classmethod
    def from_env(cls, path: Path | None = None) -> LiveBudget | None:
        """A budget of ``LIVE_CALL_BUDGET`` calls, or ``None`` when the variable is unset."""
        total = budget_total_from_env()
        return None if total is None else cls(path, total)

    def _restore(self) -> int:
        if not self.path.exists():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            spent = data["spent"]
            if not isinstance(spent, int) or isinstance(spent, bool) or spent < 0:
                raise ValueError("spent is not a non-negative integer")
        except (OSError, ValueError, KeyError, TypeError):
            self.corrupt = True
            return self.total
        return spent

    @property
    def spent(self) -> int:
        return self._spent

    @property
    def remaining(self) -> int:
        """Calls left; 0 when the counter file is corrupt."""
        if self.corrupt:
            return 0
        return max(0, self.total - self._spent)

    @property
    def exhausted(self) -> bool:
        return self.remaining == 0

    def spend(self) -> bool:
        """Take one call from the budget and persist it; ``False`` when none is left."""
        with self._lock:
            if self.exhausted:
                return False
            self._spent += 1
            self._persist()
            return True

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "total": self.total,
            "spent": self._spent,
            "remaining": self.remaining,
            "corrupt": self.corrupt,
        }

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"spent": self._spent, "total": self.total}, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
