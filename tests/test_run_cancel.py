"""WO-060C: Reset stops the run on stage.

A cancelled run keeps what the pacing clock already released, ends with one
``run_finished`` carrying ``reason: cancelled`` and never grows again; ``DELETE
/api/runs/{id}`` and ``POST /api/runs {replaces}`` reach that path; a run that ran to its
end is left exactly as it was (its ``run_finished`` has no ``reason``).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

import soundings.stream.stepped  # noqa: F401  (registers the stepped source)
from soundings.api import create_app
from soundings.stream import engine as engine_mod
from soundings.stream.engine import NoPendingApproval, RunEngine, UnknownRun
from soundings.stream.events import event_json
from soundings.stream.stepped import DeferredSteppedSource


@dataclass
class FakeClock:
    t: float = 0.0

    def now_s(self) -> float:
        return self.t


def _run(coro: Coroutine[Any, Any, list[Any]]) -> list[Any]:
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# --- the engine -----------------------------------------------------------------------------


def test_cancel_keeps_released_prefix_and_ends_the_stream() -> None:
    clock = FakeClock(0.0)
    engine = RunEngine(clock_factory=lambda: clock)
    h = engine.start("S01", 1, speed=20)
    clock.t = 1.0
    n = h.available()
    assert 0 < n < len(h.events)
    prefix = [event_json(e) for e in h.events[:n]]
    assert engine.cancel(h.run_id) is h and h.cancelled
    assert [event_json(e) for e in h.events[:n]] == prefix
    assert len(h.events) == n + 1
    last = h.events[-1]
    assert last.type == "run_finished" and last.payload.reason == "cancelled"
    assert last.seq == h.events[n - 1].seq + 1 and last.payload.n_events == n + 1
    assert last.payload.track_hash  # the batch run's own track hash
    assert h.finished and h.summary()["cancelled"] is True
    clock.t = 1e9  # no events after the final one, however long we wait
    assert h.available() == n + 1 and len(h.events) == n + 1
    assert h.cancel() is False  # idempotent
    with pytest.raises(NoPendingApproval):
        engine.approve(h.run_id, "req-1", "primary")


def test_finished_run_is_left_as_it_is() -> None:
    engine = RunEngine()
    h = engine.start("S01", 1, speed=0)
    before = [event_json(e) for e in h.events]
    assert h.cancel() is False and not h.cancelled
    assert [event_json(e) for e in h.events] == before
    assert h.events[-1].payload.reason is None
    assert '"reason"' not in before[-1]  # a completed stream is unchanged byte for byte
    with pytest.raises(UnknownRun):
        engine.cancel("run-9999")


def test_subscriber_gets_a_consistent_cancelled_stream() -> None:
    clock = FakeClock(0.0)
    engine = RunEngine(clock_factory=lambda: clock)
    h = engine.start("S01", 1, speed=20)
    clock.t = 2.0
    n = h.available()
    h.cancel()

    async def collect() -> list[Any]:
        return [e async for e in engine.subscribe(h.run_id)]

    got = _run(collect())
    assert [e.seq for e in got] == list(range(n + 1))
    assert got[-1].type == "run_finished" and got[-1].payload.reason == "cancelled"


def test_cancel_stops_a_lazy_v3_run() -> None:
    clock = FakeClock(0.0)
    engine = RunEngine(clock_factory=lambda: clock)
    h = engine.start("S02", 1, speed=20, source="v3")
    clock.t = 3.0
    n = h.available()
    h.cancel()
    src: Any = h.source
    assert src.exhausted and src.open_requests() == {}
    buffered = len(src.buffer)
    clock.t = 1e9
    assert src.pull(float("inf")) == 0 and len(src.buffer) == buffered
    assert h.available() == n + 1 and h.events[-1].payload.reason == "cancelled"
    assert engine.run_to_end(h.run_id) == h.events


def test_cancel_stepped_run_thread_exits() -> None:
    engine = RunEngine()
    h = engine.start("S02", 1, speed=20, source="stepped")
    src = h.source
    assert isinstance(src, DeferredSteppedSource)
    assert src.wait_ready(120)
    t0 = time.monotonic()
    h.cancel()
    assert src.join(2.0) and time.monotonic() - t0 < 2.0
    assert src.exhausted and h.events[-1].payload.reason == "cancelled"
    final = len(h.events)
    time.sleep(0.3)
    assert h.available() == final == len(h.events)


def test_cancel_while_building_discards_the_build() -> None:
    engine = RunEngine()
    h = engine.start("S02", 1, speed=20, source="stepped")
    src = h.source
    assert isinstance(src, DeferredSteppedSource) and not src.ready
    h.cancel()
    assert [e.type for e in h.events] == ["run_finished"]
    assert h.events[0].seq == 0 and h.events[0].payload.reason == "cancelled"
    t0 = time.monotonic()
    assert src.pull(float("inf")) == 0 and time.monotonic() - t0 < 1.0  # nothing waits
    assert src.join(120)  # runner v3's precompute returns, then the thread exits
    assert not src.ready and src.exhausted
    assert len(h.events) == 1 and h.available() == 1


def test_start_with_replaces_cancels_the_old_run_first() -> None:
    clock = FakeClock(0.0)
    engine = RunEngine(clock_factory=lambda: clock)
    a = engine.start("S01", 1, speed=1)
    b = engine.start("S01", 1, speed=1, replaces=a.run_id)
    assert a.cancelled and a.finished and not b.cancelled
    assert [h.run_id for h in engine.list() if not h.finished] == [b.run_id]
    c = engine.start("S01", 1, speed=1, replaces="run-0999-S01-s1")  # unknown: no harm
    assert not b.cancelled and not c.cancelled


# --- the API --------------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(engine_mod, "_ENGINE", RunEngine())
    return TestClient(create_app())


def _drain(client: TestClient, url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with client.websocket_connect(url) as ws:
        while True:
            try:
                out.append(json.loads(ws.receive_text()))
            except Exception:
                break
    return out


def test_delete_unknown_is_404(client: TestClient) -> None:
    assert client.delete("/api/runs/nope").status_code == 404


def test_delete_running_then_idempotent(client: TestClient) -> None:
    run_id = client.post("/api/runs", json={"scenario": "S01", "seed": 1, "speed": 1}).json()[
        "run_id"
    ]
    r = client.delete(f"/api/runs/{run_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["finished"] is True and body["cancelled"] is True
    events = _drain(client, f"/ws/runs/{run_id}")
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["reason"] == "cancelled"
    again = client.delete(f"/api/runs/{run_id}")
    assert again.status_code == 200 and again.json() == body


def test_delete_finished_is_idempotent(client: TestClient) -> None:
    run_id = client.post("/api/runs", json={"scenario": "S01", "seed": 1, "speed": 0}).json()[
        "run_id"
    ]
    r = client.delete(f"/api/runs/{run_id}")
    assert r.status_code == 200
    assert r.json()["finished"] is True and r.json()["cancelled"] is False
    events = _drain(client, f"/ws/runs/{run_id}")
    assert "reason" not in events[-1]["payload"]


def test_post_replaces_leaves_one_unfinished_run(client: TestClient) -> None:
    first = client.post("/api/runs", json={"scenario": "S01", "seed": 1, "speed": 1}).json()
    second = client.post(
        "/api/runs",
        json={"scenario": "S01", "seed": 1, "speed": 1, "replaces": first["run_id"]},
    )
    assert second.status_code == 200
    listed = {r["run_id"]: r for r in client.get("/api/runs").json()}
    assert listed[first["run_id"]]["finished"] and listed[first["run_id"]]["cancelled"]
    assert [rid for rid, r in listed.items() if not r["finished"]] == [second.json()["run_id"]]
    old = _drain(client, f"/ws/runs/{first['run_id']}")
    assert old[-1]["payload"]["reason"] == "cancelled"
    unknown = client.post(
        "/api/runs", json={"scenario": "S01", "seed": 1, "speed": 1, "replaces": "run-x"}
    )
    assert unknown.status_code == 200
