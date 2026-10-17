from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from soundings.llm.client import (
    FixtureStore,
    ModelClient,
    ModelMode,
    ReplayMiss,
    canonical_json,
    fixture_key,
    model_name_from_env,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LLM_DIR = REPO_ROOT / "soundings" / "llm"


def test_canonical_json_shape() -> None:
    assert canonical_json({"b": 1, "a": [1.0, "x"]}) == '{"a":[1.0,"x"],"b":1}'
    assert canonical_json({"s": "café"}) == '{"s":"caf\\u00e9"}'
    assert canonical_json({"z": -0.0000001}) == '{"z":0.0}'


def test_key_stable_across_dict_ordering() -> None:
    a = {"contact": "C-001", "probe": "registry", "range_m": 410.0}
    b = {"range_m": 410.0, "probe": "registry", "contact": "C-001"}
    assert fixture_key("probe_planner", a) == fixture_key("probe_planner", b)


def test_key_stable_under_float_noise_below_6dp() -> None:
    a = {"p": 0.1 + 0.2, "nested": {"x": 1.0000004}}
    b = {"p": 0.3, "nested": {"x": 1.0}}
    assert fixture_key("probe_planner", a) == fixture_key("probe_planner", b)


def test_key_changes_with_role_and_meaningful_inputs() -> None:
    inputs = {"p": 0.3}
    assert fixture_key("probe_planner", inputs) != fixture_key("sitrep_writer", inputs)
    assert fixture_key("probe_planner", inputs) != fixture_key("probe_planner", {"p": 0.300001})
    assert len(fixture_key("probe_planner", inputs)) == 64


def test_mode_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_MODE", raising=False)
    assert ModelMode.from_env() is ModelMode.REPLAY
    monkeypatch.setenv("MODEL_MODE", "record")
    assert ModelMode.from_env() is ModelMode.RECORD
    monkeypatch.setenv("MODEL_MODE", "bogus")
    with pytest.raises(ValueError):
        ModelMode.from_env()


def test_session_is_replay() -> None:
    assert ModelMode.from_env() is ModelMode.REPLAY


def test_model_name_only_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_NAME", raising=False)
    assert model_name_from_env() is None
    monkeypatch.setenv("MODEL_NAME", "some-model")
    assert model_name_from_env() == "some-model"


def test_store_roundtrip(tmp_path: Path) -> None:
    store = FixtureStore(tmp_path)
    inputs = {"q": 1.5, "a": "x"}
    assert store.load("hypothesis_writer", inputs) is None
    path = store.save("hypothesis_writer", inputs, {"hypotheses": ["h1"]})
    assert (
        path == tmp_path / "hypothesis_writer" / f"{fixture_key('hypothesis_writer', inputs)}.json"
    )
    assert not list(tmp_path.rglob("*.tmp"))
    assert store.load("hypothesis_writer", {"a": "x", "q": 1.5000000001}) == {"hypotheses": ["h1"]}


def test_replay_hit_returns_fixture(tmp_path: Path) -> None:
    store = FixtureStore(tmp_path)
    store.save("sitrep_writer", {"c": 1}, {"text": "ok"})
    client = ModelClient(mode=ModelMode.REPLAY, store=store)
    assert client.call("sitrep_writer", {"c": 1}) == {"text": "ok"}


def test_replay_miss_raises_in_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOUNDINGS_CI", "1")
    client = ModelClient(mode=ModelMode.REPLAY, store=FixtureStore(tmp_path))
    with pytest.raises(ReplayMiss):
        client.call("probe_planner", {"x": 1})


def test_replay_miss_returns_none_outside_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    client = ModelClient(mode=ModelMode.REPLAY, store=FixtureStore(tmp_path))
    with caplog.at_level("WARNING"):
        assert client.call("probe_planner", {"x": 1}) is None
    assert "replay miss" in caplog.text


@pytest.mark.parametrize("mode", [ModelMode.RECORD, ModelMode.LIVE])
def test_record_and_live_not_implemented(tmp_path: Path, mode: ModelMode) -> None:
    client = ModelClient(mode=mode, store=FixtureStore(tmp_path))
    with pytest.raises(NotImplementedError, match="no live calls in Phase 0"):
        client.call("probe_planner", {"x": 1})


def test_invalid_role_rejected(tmp_path: Path) -> None:
    client = ModelClient(mode=ModelMode.REPLAY, store=FixtureStore(tmp_path))
    with pytest.raises(ValueError):
        client.call("../escape", {})


def test_no_hardcoded_model_name_in_llm_source() -> None:
    for path in LLM_DIR.rglob("*.py"):
        assert "claude-" not in path.read_text(encoding="utf-8").lower(), path


def test_vendor_sdk_not_imported_at_import_time() -> None:
    code = "import sys, soundings.llm.client; print('anthropic' in sys.modules)"
    res = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert res.stdout.strip() == "False"
