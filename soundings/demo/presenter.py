"""The presenter log (WO-062P): what presenter mode showed, and when, kept in memory.

Presenter mode (``/?presenter=1``) posts one entry each time it lands on a step, resumes
from a ``pause_for_click``, or resets to step 0; an entry for a step whose action or beats
no merged WO ships yet carries the ``action ... pending WO-0xx`` notes. The server stamps
every entry, so the WO-062 Part B recording can compare caption times with the yaml marks:
``t_s`` is monotonic seconds since the first entry after a clear, ``wall`` is local ISO
time for the human reading the log. Captions and notes come from the script, never from
the request, so the log holds the owner's words only.

``script_payload`` is the JSON form of ``docs/demo-script.yaml`` that ``GET
/api/demo-script`` serves to the presenter overlay.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from soundings.demo.script import ACTIONS, DemoScript, DemoStep
from soundings.sim.scenario import REPO_ROOT

DEFAULT_SCRIPT = REPO_ROOT / "docs" / "demo-script.yaml"


class PresenterEventKind(StrEnum):
    STEP = "step"
    RESUME = "resume"
    RESET = "reset"


@dataclass(frozen=True)
class PresenterEntry:
    seq: int
    t_s: float
    wall: str
    kind: str
    index: int
    caption: str
    pending: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["pending"] = list(self.pending)
        return data


class PresenterLog:
    """A thread-safe, in-memory, append-only list of presenter entries."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._monotonic = monotonic
        self._wall = wall
        self._lock = threading.Lock()
        self._entries: list[PresenterEntry] = []
        self._t0: float | None = None

    def record(self, kind: PresenterEventKind, index: int, step: DemoStep) -> PresenterEntry:
        with self._lock:
            now = self._monotonic()
            if self._t0 is None:
                self._t0 = now
            entry = PresenterEntry(
                seq=len(self._entries),
                t_s=round(now - self._t0, 3),
                wall=self._wall().isoformat(timespec="milliseconds"),
                kind=kind.value,
                index=index,
                caption=step.caption or "",
                pending=step.pending,
            )
            self._entries.append(entry)
            return entry

    def entries(self) -> list[PresenterEntry]:
        with self._lock:
            return list(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._t0 = None


_LOG = PresenterLog()


def get_presenter_log() -> PresenterLog:
    """The app-scoped presenter log."""
    return _LOG


def step_payload(index: int, step: DemoStep) -> dict[str, Any]:
    """One step as the presenter overlay reads it (yaml fields plus derived ones)."""
    data = step.model_dump(mode="json", exclude_none=True)
    data.update(
        index=index,
        mark_s=step.mark_s,
        kind=step.kind,
        label=step.label,
        speed_factor=step.speed_factor,
        run_ref=step.scenario_ref if step.scenario_ref is not None else step.run_ref,
        pending=list(step.pending),
    )
    return data


def script_payload(script: DemoScript) -> dict[str, Any]:
    """The whole script as JSON for ``GET /api/demo-script``."""
    return {
        "end": script.end,
        "end_s": script.end_s,
        "steps": [step_payload(i, s) for i, s in enumerate(script.steps)],
        "actions": {a.value: ACTIONS[a] for a in ACTIONS},
    }


def load_script(path: Path = DEFAULT_SCRIPT) -> DemoScript:
    return DemoScript.load(path)


__all__ = [
    "DEFAULT_SCRIPT",
    "PresenterEntry",
    "PresenterEventKind",
    "PresenterLog",
    "get_presenter_log",
    "load_script",
    "script_payload",
    "step_payload",
]
