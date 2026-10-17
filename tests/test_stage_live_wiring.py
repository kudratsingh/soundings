"""WO-060 part B: stage live wiring. With ``MODEL_MODE=live`` and ``LIVE_CALL_BUDGET`` the
run engine builds the client with ``live_client_from_env()`` and hands it to the
investigation kit and the SITREP writer; the badge endpoint reports ``live`` after a live
reply and ``fallback`` once the budget is spent; enabling the stage archives the previous
budget file. A stub transport stands in for the network (no hosted model is called)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from soundings.api_routes.model_mode import model_mode_status
from soundings.llm.client import reset_last_path
from soundings.sim.scenario import resolve_scenario
from soundings.stream import stage
from soundings.stream.engine import SourceRequest
from soundings.stream.v3 import V3Source


class StubTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, *, model: str, role: str, system: str | None, inputs: Any) -> Any:
        self.calls.append(role)
        return {"stub": True}  # not a valid role reply: the guard falls back, the path is live


@pytest.fixture
def live_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, StubTransport]:
    calls = tmp_path / "live_calls.json"
    monkeypatch.setenv("MODEL_MODE", "live")
    monkeypatch.setenv("MODEL_NAME", "stub-model")
    monkeypatch.setenv("SOUNDINGS_LIVE_CALLS_PATH", str(calls))
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    transport = StubTransport()
    stage.reset_stage()
    stage.set_stage_transport(transport)
    reset_last_path()
    yield calls, transport
    stage.set_stage_transport(None)
    stage.reset_stage()
    reset_last_path()


def _run(tmp_path: Path, scenario: str = "S05", seed: int = 11) -> V3Source:
    sc = resolve_scenario(scenario)
    req = SourceRequest(scenario=sc, seed=seed, run_id="stage-test", speed=0)
    src = V3Source(req, violations_path=tmp_path / "violations.jsonl")
    src.events()
    return src


def test_replay_by_default_has_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_CALL_BUDGET", raising=False)
    monkeypatch.setenv("MODEL_MODE", "replay")
    assert not stage.stage_live_requested()
    assert stage.client_for_run(False).client is None
    rc = stage.client_for_run(True)
    assert rc.path == "replay" and rc.client is not None and rc.client.transport is None


def test_live_budget_unset_is_not_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_MODE", "live")
    monkeypatch.delenv("LIVE_CALL_BUDGET", raising=False)
    assert not stage.stage_live_requested()
    assert stage.client_for_run(False).path == "off"


def test_badge_live_after_a_live_reply(
    live_env: tuple[Path, StubTransport], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, transport = live_env
    monkeypatch.setenv("LIVE_CALL_BUDGET", "40")
    calls.write_text(json.dumps({"total": 40, "spent": 40}), encoding="utf-8")  # spent stage
    assert model_mode_status()["last_path"] is None
    src = _run(tmp_path)
    archived = sorted(p.name for p in tmp_path.glob("live_calls.*.json"))
    assert len(archived) == 1  # the previous budget file was archived on stage enable
    assert json.loads((tmp_path / archived[0]).read_text())["spent"] == 40
    assert src.buffer[0].payload.model_path == "live"
    assert transport.calls, "the stub transport must have been called"
    assert set(transport.calls) <= {"probe_planner", "hypothesis_writer", "sitrep_writer"}
    status = model_mode_status()
    assert status["mode"] == "live" and status["last_path"] == "live"
    assert status["live_budget_remaining"] == 40 - len(transport.calls)
    # A second run in the same process shares the budget (no second archive).
    _run(tmp_path, "S01", 1)
    assert len(list(tmp_path.glob("live_calls.*.json"))) == 1


def test_badge_fallback_after_budget_exhaustion(
    live_env: tuple[Path, StubTransport], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _calls, transport = live_env
    monkeypatch.setenv("LIVE_CALL_BUDGET", "1")
    src = _run(tmp_path)
    assert len(transport.calls) == 1
    status = model_mode_status()
    assert status["last_path"] == "fallback" and status["live_budget_remaining"] == 0
    # The run still ends: every refused call fell back to the deterministic path.
    assert src.buffer[-1].type == "run_finished"


def test_archive_never_overwrites(tmp_path: Path) -> None:
    f = tmp_path / "live_calls.json"
    f.write_text("{}", encoding="utf-8")
    a = stage.archive_live_calls(f, stamp="x")
    f.write_text("{}", encoding="utf-8")
    b = stage.archive_live_calls(f, stamp="x")
    assert a is not None and b is not None and a != b and not f.exists()
    assert stage.archive_live_calls(f, stamp="x") is None
