"""WO-061: POST /api/inject (a judge describes a contact; the ladder decides)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import soundings.api_routes.inject as inject_mod
import soundings.stream.engine as engine_mod
from soundings.api import create_app
from soundings.schemas.harbor import load_harbor
from soundings.schemas.scenario import load_scenario
from soundings.stream.engine import RunEngine

REPO = Path(__file__).resolve().parents[1]
BERTH = next(a for a in load_harbor().assets if a.asset_id == "fuel_berth_2")
# DECISIONS 2026-10-17 11:48: wall-clock budgets get a 4x factor on a shared xdist/CI
# runner; a direct run (and `make test` on the demo laptop) proves the 2.0 s bound itself.
SLACK = 4.0 if os.environ.get("PYTEST_XDIST_WORKER") or os.environ.get("SOUNDINGS_CI") else 1.0
FOLLOW_S = 2.0 * SLACK
CLOSING = {"lat": round(BERTH.lat - 0.0036, 6), "lon": BERTH.lon, "behavior": "closing"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SOUNDINGS_LIVE_DIR", str(tmp_path / "live"))
    monkeypatch.setattr(engine_mod, "_ENGINE", RunEngine())
    monkeypatch.setattr(inject_mod, "_PREPARED", {})
    yield TestClient(create_app())


def _first(client: TestClient, run_id: str, kinds: set[str], t0: float) -> dict[str, float]:
    seen: dict[str, float] = {}
    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        while not kinds <= set(seen):
            e = json.loads(ws.receive_text())
            if e["type"] in kinds and e["type"] not in seen:
                seen[e["type"]] = time.monotonic() - t0
    return seen


def _wait_prepared(run_count: int = 1) -> None:
    engine = engine_mod.get_engine()
    for h in engine.list():
        assert h.source.wait_ready(300)


def test_inject_starts_within_2s(client: TestClient) -> None:
    # The judge places the pin and picks a behavior: the panel prepares that run.
    p = client.post("/api/inject/prepare", json=CLOSING)
    assert p.status_code == 200, p.text
    _wait_prepared()  # (the judge is still deciding)
    t0 = time.monotonic()
    r = client.post("/api/inject", json=CLOSING)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"run_id", "scenario_id", "path", "seed"}
    seen = _first(client, out["run_id"], {"tick", "transition"}, t0)
    assert seen["tick"] <= FOLLOW_S and seen["transition"] <= FOLLOW_S, seen
    path = Path(out["path"])
    assert path.is_file() and path.name == f"{out['scenario_id']}.yaml"
    # The written file reaches the hold / approval path with the real runner at speed 0.
    engine = engine_mod.get_engine()
    h = engine.start(load_scenario(path), out["seed"], speed=0, source="stepped")
    events = engine.run_to_end(h.run_id)
    states = {e.payload.to_state.value for e in events if e.type == "transition"}
    asked = any(e.type == "approval_request" for e in events)
    assert "HOLDING" in states or asked


def test_inject_without_prepare_follows_at_once(client: TestClient) -> None:
    t0 = time.monotonic()
    r = client.post("/api/inject", json={**CLOSING, "behavior": "loitering"})
    assert r.status_code == 200, r.text
    assert time.monotonic() - t0 <= FOLLOW_S  # the screen switches to the run at once
    seen = _first(client, r.json()["run_id"], {"run_started", "tick"}, t0)
    assert seen["run_started"] <= seen["tick"]


@pytest.mark.parametrize(
    "body",
    [
        {**CLOSING, "rung": "hold"},
        {**CLOSING, "expected": {}},
        {**CLOSING, "path": "/tmp/x.yaml"},
        {**CLOSING, "state": "HANDED_OFF"},
        {**CLOSING, "behavior": "sprint"},
        {**CLOSING, "lat": 33.60, "lon": -118.10},
    ],
)
def test_inject_rejects(client: TestClient, body: dict[str, Any]) -> None:
    r = client.post("/api/inject", json=body)
    assert r.status_code == 422, r.text
    if body.get("lat") == 33.60:
        assert r.json()["detail"] == "outside the declared security zones"
    assert client.post("/api/inject/prepare", json=body).status_code == 422


def test_too_many_live_runs(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_mod, "_ENGINE", RunEngine(max_runs=1, default_speed=1.0))
    assert client.post("/api/inject", json=CLOSING).status_code == 200
    r = client.post("/api/inject", json=CLOSING)
    assert r.status_code == 429 and r.json()["detail"] == "too many live runs"


def _cli(path: Path, seed: int) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "soundings.stream", "--scenario", str(path), "--seed", str(seed)],
        cwd=REPO,
        env={**os.environ, "MODEL_MODE": "replay"},
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


def _ticks(lines: list[str]) -> list[dict[str, Any]]:
    out = []
    for line in lines:
        e = json.loads(line)
        if e["type"] == "tick":
            out.append({"tick": e["tick"], "t_s": e["t_s"], "payload": e["payload"]})
    return out


def test_live_file_replays(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_mod, "_ENGINE", RunEngine(default_speed=0.0))
    r = client.post("/api/inject", json=CLOSING).json()
    streamed: list[str] = []
    with client.websocket_connect(f"/ws/runs/{r['run_id']}") as ws:
        while True:
            try:
                streamed.append(ws.receive_text())
            except Exception:
                break
    a = _cli(Path(r["path"]), r["seed"])
    b = _cli(Path(r["path"]), r["seed"])
    assert a == b
    assert _ticks(a) and _ticks(a) == _ticks(streamed)
