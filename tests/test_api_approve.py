"""WO-061: POST /api/runs/{run_id}/approve (the button only asks; the guard decides)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import soundings.stream.engine as engine_mod
from soundings.api import create_app
from soundings.ladder.approval import issue_token
from soundings.ladder.audit import AuditLog
from soundings.ladder.types import ActionScope
from soundings.schemas.enums import LadderState
from soundings.stream.engine import RunEngine
from soundings.stream.stepped import DeferredSteppedSource
from soundings.stream.v3 import TokenRefused

REPO = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now_s(self) -> float:
        return self.t


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    clock = FakeClock()
    db = tmp_path / "audit.db"
    engine = RunEngine(clock_factory=lambda: clock)
    monkeypatch.setattr(engine_mod, "_ENGINE", engine)
    monkeypatch.setitem(
        engine_mod._SOURCES, "stepped", lambda r: DeferredSteppedSource(r, audit_db=db)
    )
    yield {"client": TestClient(create_app()), "engine": engine, "clock": clock, "db": db}


def _start_s05(env: dict[str, Any]) -> tuple[str, Any]:
    r = env["client"].post("/api/runs", json={"scenario": "S05", "seed": 11, "source": "stepped"})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    h = env["engine"].get(run_id)
    assert h.source.wait_ready(120)
    while True:
        env["clock"].t += 5.0
        h.available()
        req = next((e.payload for e in h.events if e.type == "approval_request"), None)
        if req is not None:
            return run_id, req
        assert not h.exhausted


def _states(env: dict[str, Any], run_id: str, cid: str) -> list[LadderState]:
    h = env["engine"].get(run_id)
    return [
        e.payload.to_state
        for e in h.events[: h.available()]
        if e.type == "transition" and e.payload.contact_id == cid
    ]


def test_unknown_run_is_404(env: dict[str, Any]) -> None:
    r = env["client"].post("/api/runs/run-9999-S05-s1/approve", json={"request_id": "x"})
    assert r.status_code == 404


@pytest.mark.parametrize("extra", ["rung", "scope", "action"])
def test_body_cannot_steer(env: dict[str, Any], extra: str) -> None:
    body = {"request_id": "apr-c-0004-1-primary", extra: "hold"}
    r = env["client"].post("/api/runs/whatever/approve", json=body)
    assert r.status_code == 422


def test_approve_flow(env: dict[str, Any]) -> None:
    c = env["client"]
    run_id, req = _start_s05(env)
    assert c.post(f"/api/runs/{run_id}/approve", json={"request_id": "apr-nope"}).status_code == 409
    r = c.post(f"/api/runs/{run_id}/approve", json={"request_id": req.request_id})
    assert r.status_code == 200, r.text
    granted = r.json()
    assert granted["contact_id"] == req.contact_id and len(granted["token_hash"]) == 64
    again = c.post(f"/api/runs/{run_id}/approve", json={"request_id": req.request_id})
    assert again.status_code == 409
    env["clock"].t = 1e9
    h = env["engine"].get(run_id)
    h.available()
    assert h.finished
    assert LadderState.HANDED_OFF in _states(env, run_id, req.contact_id)
    results = [e.payload for e in h.events if e.type == "approval_result"]
    assert [(r.outcome, r.token_hash) for r in results] == [("granted", granted["token_hash"])]
    log = AuditLog(env["db"])
    try:
        assert any(granted["token_hash"] in row.payload_json for row in log.rows())
        assert log.verify().ok
    finally:
        log.close()
    out = subprocess.run(
        [sys.executable, "-m", "soundings.audit", "verify", "--db", str(env["db"])],
        cwd=REPO,
        env={**os.environ, "MODEL_MODE": "replay"},
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr


def test_no_pending_request_on_a_simulated_run(env: dict[str, Any]) -> None:
    r = env["client"].post("/api/runs", json={"scenario": "S05", "seed": 11, "source": "v3"})
    run_id = r.json()["run_id"]
    a = env["client"].post(f"/api/runs/{run_id}/approve", json={"request_id": "apr-x"})
    assert a.status_code == 409


def test_scope_mismatch_blocked(env: dict[str, Any]) -> None:
    run_id, req = _start_s05(env)
    h = env["engine"].get(run_id)
    src = h.source.inner
    wide = issue_token(
        contact_id=req.contact_id,
        scope=ActionScope.model_validate({**req.scope, "max_minutes": 600}),
        approver="primary",
        channel="ui",
        issued_at=src.now_s(),
        expires_at=req.expires_t_s,
    ).model_dump(mode="json")
    with pytest.raises(TokenRefused):  # the hook pre-checks with the guard's own verifier
        env["engine"].deliver_approval(run_id, wide)
    # Past the pre-check, straight to the ladder: its guard blocks the widened token.
    src.rs.approve(req.contact_id, "granted", token=wide)
    env["clock"].t += 4.0  # two ticks at 1x while awaiting approval
    h.available()
    assert _states(env, run_id, req.contact_id)[-1] is LadderState.AWAITING_APPROVAL
    assert not [e for e in h.events if e.type == "approval_result"]


def test_decline_route(env: dict[str, Any]) -> None:
    run_id, req = _start_s05(env)
    c = env["client"]
    r = c.post(f"/api/runs/{run_id}/decline", json={"request_id": req.request_id})
    assert r.status_code == 200 and r.json()["outcome"] == "denied"
    assert (
        c.post(f"/api/runs/{run_id}/approve", json={"request_id": req.request_id}).status_code
        == 409
    )
