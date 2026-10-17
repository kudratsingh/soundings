# --- WO-060B begin: stage live wiring ---
"""Stage live-client wiring for the run engine (WO-060 part B; WO-087 open item).

Which model client a runner v3 run gets:

* ``MODEL_MODE=live`` **and** ``LIVE_CALL_BUDGET`` set (the owner's
  ``approve paid run stage live budget N`` sets both): the client from
  ``soundings.llm.client.live_client_from_env()``. It is passed into the investigation kit
  (``LlmPlanner``, ``HypothesisWriter``) and into ``HandoffService(writer_mode="auto")``;
  every refusal, timeout or spent budget falls back to the deterministic choice or the
  template SITREP (WO-087), so a run never stops for a model.
* otherwise, with ``use_llm`` on the run: a replay client (recorded fixtures; a miss falls
  back outside CI).
* otherwise: no client at all (deterministic planner, template SITREP, zero model calls).

Stage enable: the first live client a server process builds archives the previous
``artifacts/live_calls.json`` to ``live_calls.<stamp>.json`` (DECISIONS 2026-10-17, WO-060B)
so the stage budget starts fresh; later runs in the same process share that one budget
file. The archive is never deleted: the spend of every earlier stage session stays on disk.

Tests inject a stub transport with ``set_stage_transport``; nothing here reaches the
network on its own (the vendor transport is only what ``live_client_from_env`` wires).
"""

from __future__ import annotations

import datetime as dt
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from soundings.llm.client import (
    ModelClient,
    ModelMode,
    Transport,
    live_client_from_env,
)
from soundings.llm.live_budget import LIVE_CALL_BUDGET_ENV, live_calls_path

ModelPath = Literal["off", "replay", "live"]

_lock = threading.Lock()
_enabled = False
_transport: Transport | None = None


def stage_live_requested() -> bool:
    """``MODEL_MODE=live`` with ``LIVE_CALL_BUDGET`` set (non-empty)."""
    mode = os.environ.get("MODEL_MODE", "replay").strip().lower()
    return mode == ModelMode.LIVE.value and bool(os.environ.get(LIVE_CALL_BUDGET_ENV, "").strip())


def set_stage_transport(transport: Transport | None) -> None:
    """Replace the vendor transport on stage clients (tests: a stub; ``None`` restores)."""
    global _transport
    _transport = transport


def reset_stage() -> None:
    """Forget that the stage was enabled (tests)."""
    global _enabled
    with _lock:
        _enabled = False


def archive_live_calls(path: Path | None = None, *, stamp: str | None = None) -> Path | None:
    """Move ``live_calls.json`` aside as ``live_calls.<stamp>.json``; ``None`` if absent.

    ``stamp`` defaults to the local wall time ``YYYYmmdd-HHMMSS`` (stage bookkeeping, not
    sim code); an existing archive of the same stamp gets a ``-N`` suffix, never overwritten.
    """
    src = Path(path) if path is not None else live_calls_path()
    if not src.exists():
        return None
    label = stamp or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = src.name.removesuffix(".json")
    dst = src.with_name(f"{stem}.{label}.json")
    n = 1
    while dst.exists():
        dst = src.with_name(f"{stem}.{label}-{n}.json")
        n += 1
    os.replace(src, dst)
    return dst


def enable_stage() -> bool:
    """Archive the previous budget file once per process; True on the first call."""
    global _enabled
    with _lock:
        if _enabled:
            return False
        archive_live_calls()
        _enabled = True
        return True


@dataclass(frozen=True)
class RunClient:
    """What one run is given: the client (or none) and which path it is."""

    client: ModelClient | None
    path: ModelPath

    @property
    def llm_roles(self) -> bool:
        return self.client is not None


def client_for_run(use_llm: bool) -> RunClient:
    """The client of one runner v3 run (see the module docstring)."""
    if stage_live_requested():
        enable_stage()
        client = live_client_from_env()
        if _transport is not None and client.mode is ModelMode.LIVE:
            client.transport = _transport
        return RunClient(client, "live")
    if use_llm:
        return RunClient(ModelClient(mode=ModelMode.REPLAY), "replay")
    return RunClient(None, "off")


__all__ = [
    "ModelPath",
    "RunClient",
    "archive_live_calls",
    "client_for_run",
    "enable_stage",
    "reset_stage",
    "set_stage_transport",
    "stage_live_requested",
]

# --- WO-060B end ---
