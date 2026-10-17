"""WO-087: ``GET /api/model_mode`` for the judge-panel badge (no network, no live call)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soundings.api import create_app
from soundings.llm.client import FixtureStore, ModelClient, ModelMode, reset_last_path
from soundings.llm.live_budget import LiveBudget
from soundings.llm.roles import ROLE_PROBE_PLANNER
from tests.support.live_runs import STUB_MODEL, ScriptedTransport

KEYS = {"mode", "live_budget_total", "live_budget_remaining", "last_path"}


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    monkeypatch.delenv("LIVE_CALL_BUDGET", raising=False)
    monkeypatch.setenv("MODEL_MODE", "replay")
    counter = tmp_path / "artifacts" / "live_calls.json"
    monkeypatch.setenv("SOUNDINGS_LIVE_CALLS_PATH", str(counter))
    reset_last_path()
    return counter


def _get(tmp_path: Path) -> dict[str, object]:
    resp = TestClient(create_app(web_dist=tmp_path / "no-dist")).get("/api/model_mode")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict) and set(body) == KEYS
    return body


def test_default_is_replay_with_no_budget(tmp_path: Path, _env: Path) -> None:
    assert _get(tmp_path) == {
        "mode": "replay",
        "live_budget_total": 0,
        "live_budget_remaining": 0,
        "last_path": None,
    }
    assert not _env.exists()  # reading never creates the counter


def test_last_path_after_a_fallback(tmp_path: Path) -> None:
    client = ModelClient(mode=ModelMode.REPLAY, store=FixtureStore(tmp_path / "none"))
    assert client.call(ROLE_PROBE_PLANNER, {"x": 1}) is None  # replay miss -> fallback
    body = _get(tmp_path)
    assert body["mode"] == "replay" and body["last_path"] == "fallback"


def test_remaining_budget_from_the_persisted_counter(
    tmp_path: Path, _env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIVE_CALL_BUDGET", "40")
    budget = LiveBudget(_env, total=40)
    transport = ScriptedTransport(lambda role, inputs: {"ok": True})
    client = ModelClient(
        mode=ModelMode.LIVE, model_name=STUB_MODEL, live_budget=budget, transport=transport
    )
    for i in range(3):
        assert client.call(ROLE_PROBE_PLANNER, {"x": i}) == {"ok": True}
    body = _get(tmp_path)
    assert body["live_budget_total"] == 40
    assert body["live_budget_remaining"] == 37
    assert body["last_path"] == "live"
    assert json.loads(_env.read_text(encoding="utf-8"))["spent"] == 3


def test_live_mode_reported_and_corrupt_counter_shows_zero(
    tmp_path: Path, _env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_MODE", "live")
    monkeypatch.setenv("LIVE_CALL_BUDGET", "40")
    _env.parent.mkdir(parents=True)
    _env.write_text("{broken", encoding="utf-8")
    body = _get(tmp_path)
    assert body["mode"] == "live"
    assert (body["live_budget_total"], body["live_budget_remaining"]) == (40, 0)


def test_malformed_budget_reports_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_CALL_BUDGET", "lots")
    body = _get(tmp_path)
    assert (body["live_budget_total"], body["live_budget_remaining"]) == (0, 0)
