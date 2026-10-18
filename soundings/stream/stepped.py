"""The ``stepped`` run source (WO-061): runner v3 stepped per paced tick, a person approves.

``SteppedSource`` is WO-060 part B's ``V3Source`` with a ``UiApprover`` (WO-043's
``ManualApprover``, an ``ApproverPort``) on the ladder: an approval request waits for the
watch officer's button. ``approve(request_id, approver)`` mints a WO-041 token for exactly
the open request's scope (``ladder_adapter.grant``, approver ``primary``, channel ``ui``)
and hands it to the ladder through ``deliver_approval``; ``decline`` delivers a person's
no. The ladder's guard decides on the next tick (CLAUDE.md 2.2): the button only asks.

Registered with ``register_source("stepped", ...)``. Pacing is WO-060's engine (1x while
any contact is AWAITING_APPROVAL), and the stream is the v3 stream event for event.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from soundings.canonical import round6
from soundings.ladder.approval import token_hash
from soundings.runner import ManualApprover
from soundings.schemas.enums import ToggleName
from soundings.schemas.scenario import Scenario
from soundings.sim.scenario import resolve_scenario
from soundings.stream.engine import NoPendingApproval, SourceRequest, register_source
from soundings.stream.events import (
    ApprovalRequestPayload,
    ApprovalResultEvent,
    ApprovalResultPayload,
    ErrorEvent,
    ErrorPayload,
    StreamEvent,
)
from soundings.stream.ladder_adapter import UI_APPROVER, grant
from soundings.stream.ledger import BluntCounter, LedgerAnnotator
from soundings.stream.v3 import V3Source

SOURCE_NAME = "stepped"


class UiApprover(ManualApprover):
    """The approval UI's ``ApproverPort``: replies posted through the endpoint are queued
    and the ladder polls them on its next tick (WO-043 ``ManualApprover``)."""


class AlreadyAnswered(LookupError):
    """A second answer to the same request (HTTP 409)."""


class SteppedSource(V3Source):
    """Runner v3 with a person on the approval path."""

    name = SOURCE_NAME

    def __init__(self, req: Any, *, audit_db: str | Path | None = None, **kw: Any) -> None:
        super().__init__(req, manual=True, audit_db=audit_db, approver=UiApprover(), **kw)
        self._answered: set[str] = set()

    # --- what is open ----------------------------------------------------------------------

    def now_s(self) -> float:
        """Sim time of the last stepped tick (a token is issued no later than the next)."""
        return round6(max(self._tick, 0) * self.tick_s)

    def open_request(self, request_id: str) -> ApprovalRequestPayload:
        """The open request with that id; ``NoPendingApproval`` or ``AlreadyAnswered``."""
        if request_id in self._answered:
            raise AlreadyAnswered(f"request {request_id!r} was already answered")
        open_ids = {rid for ids in self._open.values() for rid in ids}
        if request_id not in open_ids:
            raise NoPendingApproval(f"no open approval request {request_id!r}")
        for e in reversed(self.buffer):
            if e.type == "approval_request" and e.payload.request_id == request_id:
                payload: ApprovalRequestPayload = e.payload
                return payload
        raise NoPendingApproval(f"no open approval request {request_id!r}")  # pragma: no cover

    def mark_answered(self, request_id: str) -> None:
        """Record that a person answered ``request_id`` (a second answer is refused)."""
        self._answered.add(request_id)

    # --- a person's answer -----------------------------------------------------------------

    def approve(self, request_id: str, approver: str) -> ApprovalResultEvent:
        """Grant the open request: a token for exactly its scope goes to the ladder.

        Returns the person's answer as delivered (``token_hash`` of the token); the ladder's
        own ``approval_result`` follows in the stream once its guard has checked the token.
        ``approver`` is the person's display name; the token's approver id is ``primary``."""
        request = self.open_request(request_id)
        token = grant(request, UI_APPROVER, self.now_s())
        self.deliver_approval(token.model_dump(mode="json"))
        self._answered.add(request_id)
        return self._answer(request_id, "granted", token_hash(token))

    def decline(self, request_id: str, approver: str) -> ApprovalResultEvent:
        """A person's no; the ladder takes it on the next tick."""
        request = self.open_request(request_id)
        self.rs.approve(request.contact_id, "denied")
        self._answered.add(request_id)
        return self._answer(request_id, "denied", None)

    def _answer(self, request_id: str, outcome: str, th: str | None) -> ApprovalResultEvent:
        return ApprovalResultEvent.model_validate(
            {
                "run_id": self.req.run_id,
                "seq": max(0, self._seq - 1),
                "tick": max(self._tick, 0),
                "t_s": self.now_s(),
                "payload": ApprovalResultPayload(
                    request_id=request_id,
                    outcome=outcome,  # type: ignore[arg-type]
                    token_hash=th,
                ),
            }
        )


def stepped_stream_events(
    scenario: Scenario | str,
    seed: int | None = None,
    *,
    run_id: str = "cli",
    toggles: Sequence[ToggleName] = (),
    approve_first: bool = False,
) -> list[StreamEvent]:
    """The unpaced stream of one stepped run. ``approve_first`` grants every approval
    request at the tick it is asked (a person who answers at once); otherwise each request
    times out as nobody answers. Deterministic (CLI ``--source stepped``)."""
    sc = resolve_scenario(scenario) if isinstance(scenario, str) else scenario
    req = SourceRequest(
        scenario=sc,
        seed=sc.seed if seed is None else seed,
        run_id=run_id,
        speed=0,
        toggles=tuple(toggles),
    )
    src = SteppedSource(req)
    src.annotate(
        LedgerAnnotator(BluntCounter(sc, req.seed).start(), expected_track_hash=src.track_hash)
    )
    while not src.exhausted:
        src.pull(round6((src._tick + 1) * src.tick_s))
        if approve_first:
            for rids in src.open_requests().values():
                for rid in rids:
                    if rid not in src._answered and rid.endswith("-primary"):
                        src.approve(rid, "Watch Officer")
    return src.buffer


class DeferredSteppedSource:
    """A ``SteppedSource`` built on a worker thread, so a run starts at once (WO-061).

    Runner v3 precomputes the sim and the investigation before its first tick (seconds on
    a loaded laptop). The engine gets this handle immediately: the run id exists, the
    screen switches to it, and the stream begins when the build is done. The engine's
    pacing clock restarts at that moment (``on_ready``), so nothing is released in a burst.
    The stream itself is the ``SteppedSource`` stream, event for event (CLAUDE.md 2.4)."""

    name = SOURCE_NAME
    lazy = True

    def __init__(
        self, req: Any, *, background: bool = True, audit_db: str | Path | None = None
    ) -> None:
        self.req = req
        self.audit_db = audit_db
        self.buffer: list[StreamEvent] = []
        self._inner: SteppedSource | None = None
        self._annotator: LedgerAnnotator | None = None
        self._hooks: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._failed = False
        self._cancelled = threading.Event()  # WO-060C
        self._thread: threading.Thread | None = None  # WO-060C
        if background:
            self._thread = threading.Thread(
                target=self._build, name=f"stepped-{req.run_id}", daemon=True
            )
            self._thread.start()
        else:
            self._build()

    def _build(self) -> None:
        try:
            inner = SteppedSource(self.req, audit_db=self.audit_db)
        except Exception as exc:  # the run reports the failure instead of hanging
            with self._lock:
                self.buffer.append(
                    ErrorEvent(
                        run_id=self.req.run_id,
                        seq=0,
                        tick=0,
                        t_s=0.0,
                        payload=ErrorPayload(message=f"run failed to start: {exc}"),
                    )
                )
                self._failed = True
            self._ready.set()
            return
        with self._lock:
            # --- WO-060C begin: a run cancelled while building is discarded, not started ---
            if self._cancelled.is_set():
                inner.cancel()
                self._failed = True
                self._ready.set()
                return
            # --- WO-060C end ---
            if self._annotator is not None:
                self._annotator.expected_track_hash = inner.track_hash
                inner.annotate(self._annotator)
            for hook in self._hooks:
                hook()
            self.buffer.extend(inner.buffer)
            inner.buffer = self.buffer
            self._inner = inner
        self._ready.set()

    # --- the engine's lazy-source protocol ----------------------------------------------

    def on_ready(self, hook: Callable[[], None]) -> None:
        """Call ``hook`` once the build is done (now, if it already is)."""
        with self._lock:
            if self._inner is None and not self._failed:
                self._hooks.append(hook)
                return
        hook()

    # --- WO-060C begin ---
    def cancel(self) -> None:
        """Stop the run: a finished build is stopped (``V3Source.cancel``), a build still in
        progress is discarded as soon as runner v3's precompute returns, and nothing waits
        on it (an unpaced pull returns at once)."""
        with self._lock:
            self._cancelled.set()
            inner = self._inner
        if inner is not None:
            inner.cancel()
        self._ready.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the build thread; True once it has exited (or there never was one)."""
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    # --- WO-060C end ---

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    @property
    def ready(self) -> bool:
        return self._inner is not None

    @property
    def inner(self) -> SteppedSource:
        if self._inner is None:
            raise NoPendingApproval("the run is still starting; nothing is pending yet")
        return self._inner

    @property
    def exhausted(self) -> bool:
        if self._failed or self._cancelled.is_set():  # WO-060C
            return True
        return self._inner is not None and self._inner.exhausted

    @property
    def track_hash(self) -> str | None:
        return None if self._inner is None else self._inner.track_hash

    def annotate(self, annotator: LedgerAnnotator) -> None:
        with self._lock:
            if self._inner is None:
                self._annotator = annotator
                return
        annotator.expected_track_hash = self._inner.track_hash
        self._inner.annotate(annotator)

    def pull(self, until_t_s: float) -> int:
        """Step the built run; an unpaced pull (``inf``) waits for the build instead of
        spinning (the engine's speed-0 fill loops until the stream is exhausted)."""
        if self._inner is None and until_t_s == math.inf and not self._cancelled.is_set():
            self._ready.wait()
        inner = self._inner
        return 0 if inner is None else inner.pull(until_t_s)

    def events(self) -> list[StreamEvent]:
        self._ready.wait()
        if self._inner is not None:
            self._inner.events()
        return self.buffer

    # --- approvals (delegated) -------------------------------------------------------------

    def approve(self, request_id: str, approver: str) -> ApprovalResultEvent:
        return self.inner.approve(request_id, approver)

    def decline(self, request_id: str, approver: str) -> ApprovalResultEvent:
        return self.inner.decline(request_id, approver)

    def deliver_approval(self, token: Mapping[str, Any]) -> str:
        return self.inner.deliver_approval(token)


def stepped_of(src: object) -> SteppedSource | None:
    """The ``SteppedSource`` behind a run's source (None when it has no person approving)."""
    if isinstance(src, SteppedSource):
        return src
    if isinstance(src, DeferredSteppedSource):
        return src.inner
    return None


def stepped_source(req: Any) -> DeferredSteppedSource:
    """The registered ``stepped`` factory (built on a worker thread)."""
    return DeferredSteppedSource(req)


register_source(SOURCE_NAME, stepped_source)

__all__ = [
    "SOURCE_NAME",
    "AlreadyAnswered",
    "DeferredSteppedSource",
    "SteppedSource",
    "UiApprover",
    "stepped_of",
    "stepped_source",
    "stepped_stream_events",
]
