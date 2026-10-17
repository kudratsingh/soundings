"""WO-062P: the shot list served as JSON and the in-memory presenter log."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soundings.api import create_app
from soundings.demo import presenter
from soundings.demo.presenter import PresenterEventKind, PresenterLog
from soundings.demo.script import DemoScript
from soundings.schemas.denylist import find_denylisted

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "docs" / "demo-script.yaml"


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(presenter, "_LOG", PresenterLog())
    return TestClient(create_app())


def test_demo_script_json_matches_yaml(client: TestClient) -> None:
    r = client.get("/api/demo-script")
    assert r.status_code == 200
    body = r.json()
    script = DemoScript.load(SCRIPT)
    assert body["end"] == "3:00" and body["end_s"] == 180
    assert len(body["steps"]) == len(script.steps) == 7
    for i, (row, step) in enumerate(zip(body["steps"], script.steps, strict=True)):
        assert row["index"] == i
        assert row["t"] == step.t and row["mark_s"] == step.mark_s
        assert row["caption"] == step.caption
        assert row["speed"] == step.speed and row["speed_factor"] in (1.0, 20.0)
        assert row["kind"] == step.kind
        assert row["pending"] == list(step.pending)
    first = body["steps"][0]
    assert first["scenario"] == "S01" and first["run_ref"] == "S01"
    twist = body["steps"][4]
    assert twist["action"] == "twist_toggles" and twist["run_ref"] == "S05"
    assert twist["toggles"] == ["network_down", "approver_unreachable"]
    assert body["actions"]["end_card"] is None
    assert body["actions"]["propose_slot"] == "WO-082"
    assert body["steps"][3]["pause_for_click"] is True


def test_presenter_log_round_trip(client: TestClient) -> None:
    assert client.get("/api/presenter-log").json() == []
    a = client.post("/api/presenter-log", json={"index": 0}).json()
    b = client.post("/api/presenter-log", json={"index": 2, "kind": "step"}).json()
    c = client.post("/api/presenter-log", json={"index": 0, "kind": "reset"}).json()
    log = client.get("/api/presenter-log").json()
    assert log == [a, b, c]
    assert [e["seq"] for e in log] == [0, 1, 2]
    assert [e["kind"] for e in log] == ["step", "step", "reset"]
    assert [e["index"] for e in log] == [0, 2, 0]
    script = DemoScript.load(SCRIPT)
    assert a["caption"] == script.steps[0].caption
    assert a["t_s"] == 0.0 and a["t_s"] <= b["t_s"] <= c["t_s"]
    assert datetime.fromisoformat(a["wall"]).tzinfo is not None
    assert a["pending"] == []
    assert b["pending"] == [
        "action file_plan_qr pending WO-061",
        "action propose_slot pending WO-082",
        "action vessel_advisory pending WO-083",
    ]
    assert client.delete("/api/presenter-log").json() == {"ok": True}
    assert client.get("/api/presenter-log").json() == []


def test_presenter_log_rejects_bad_requests(client: TestClient) -> None:
    assert client.post("/api/presenter-log", json={"index": 99}).status_code == 404
    assert client.post("/api/presenter-log", json={"index": -1}).status_code == 422
    assert client.post("/api/presenter-log", json={"index": 0, "kind": "x"}).status_code == 422
    # The caption always comes from the script, never from the request.
    r = client.post("/api/presenter-log", json={"index": 0, "caption": "free text"})
    assert r.status_code == 422
    assert client.get("/api/presenter-log").json() == []


def test_presenter_log_clock_is_injected() -> None:
    ticks = iter([10.0, 10.25, 12.5])
    log = PresenterLog(monotonic=lambda: next(ticks))
    step = DemoScript.load(SCRIPT).steps[0]
    for _ in range(3):
        log.record(PresenterEventKind.STEP, 0, step)
    assert [e.t_s for e in log.entries()] == [0.0, 0.25, 2.5]
    log.clear()
    assert log.entries() == []


def test_bad_script_is_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text('steps:\n  - {t: "0:00", scenario: S99, say: "x"}\n', encoding="utf-8")
    monkeypatch.setattr(presenter, "DEFAULT_SCRIPT", bad)
    assert client.get("/api/demo-script").status_code == 500


def test_denylist_clean_over_the_presenter_surface() -> None:
    paths = [SCRIPT, REPO / "scripts" / "gen_sfx.py", REPO / "soundings" / "demo" / "presenter.py"]
    paths += [REPO / "soundings" / "api_routes" / "presenter.py"]
    paths += sorted(p for p in (REPO / "web" / "src").rglob("*") if p.is_file())
    assert len(paths) > 10
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert not find_denylisted(text), path
        assert not find_denylisted(path.name), path


def test_sfx_module_maps_the_audit_events() -> None:
    text = (REPO / "web" / "src" / "sfx.ts").read_text(encoding="utf-8")
    for state, sound in (
        ("ASKING", "hail_ping"),
        ("ADVISING_SLOW", "advisory_chime"),
        ("HOLDING", "hold_tone"),
        ("HANDED_OFF", "handoff_chime"),
    ):
        assert f"{state}: '{sound}'" in text
    assert "TWIST_SOUND: SoundName = 'twist_klaxon_lite'" in text
    for name in ("hail_ping", "advisory_chime", "hold_tone", "handoff_chime", "twist_klaxon_lite"):
        assert (REPO / "web" / "public" / "sfx" / f"{name}.wav").is_file()
