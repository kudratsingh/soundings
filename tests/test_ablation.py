"""WO-093: the planner ablation entry point (WO-051 addendum item 3). Replay only."""

from __future__ import annotations

from pathlib import Path

import pytest

from soundings.eval import ablation as ab


def test_expand_scenarios_ranges_and_bt01_pending(tmp_path: Path) -> None:
    ids, pending = ab.expand_scenarios("S04-S08,BT-01", root=tmp_path)
    assert ids == ("S04", "S05", "S06", "S07", "S08") and pending == ("BT-01",)
    (tmp_path / "BT-01.yaml").write_text("id: BT-01\n", encoding="utf-8")
    ids, pending = ab.expand_scenarios("S05,S04-S05,BT-01", root=tmp_path)
    assert ids == ("S05", "S04", "BT-01") and pending == ()


def test_no_fixtures_llm_equals_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    report = ab.run_ablation(
        ("llm", "deterministic"), ("S04", "S05"), fixture_root=tmp_path, pending=("BT-01",)
    )
    for sid in ("S04", "S05"):
        llm, det = report.row("llm", sid), report.row("deterministic", sid)
        assert (llm.probes, llm.s_seen_to_handled, llm.missed_threats, llm.interruptions) == (
            det.probes,
            det.s_seen_to_handled,
            det.missed_threats,
            det.interruptions,
        )
        assert llm.llm_choices == 0 and det.probes > 0
    assert report.llm_fixture_hits == 0
    sentence = ab.ablation_sentence(report)
    assert "equals the deterministic planner" in sentence
    assert "added nothing" in sentence and "BT-01 pending" in sentence
    table = ab.ablation_table(report)
    assert "Where the LLM layer added nothing: " + sentence in table
    assert "method:" in table


def test_random_arm_is_seeded_and_guarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)  # the random arm leaves writer misses
    out = []
    for i in range(2):
        path = tmp_path / f"a{i}.json"
        assert (
            ab.main(
                ["--planners", "random,deterministic", "--scenarios", "S05", "--out", str(path)]
            )
            == 0
        )
        out.append(path.read_bytes())
    assert out[0] == out[1]
    report = ab.run_ablation(("random",), ("S05",))
    assert report.row("random", "S05").llm_choices > 0


def test_random_store_answers_only_permitted_probes() -> None:
    store = ab.RandomPlannerStore(seed=1)
    inputs = {
        "current_rung": "ask",
        "already_run": ["acoustic_hail"],
        "remaining_budget": {"probes": 2, "paid_probes": 0, "interruptions": 0},
        "posterior": {"posterior": {"benign_wildlife": 0.6, "unauthorized_uuv": 0.4}},
        "allowlist": [
            {"probe_id": "acoustic_hail", "rung_required": "ask", "cost_usd": 0, "interruption": 0},
            {"probe_id": "free_look", "rung_required": "watch", "cost_usd": 0, "interruption": 0},
            {"probe_id": "paid_look", "rung_required": "ask", "cost_usd": 5, "interruption": 0},
            {"probe_id": "eyes_on", "rung_required": "ask", "cost_usd": 0, "interruption": 1},
            {"probe_id": "too_high", "rung_required": "hold", "cost_usd": 0, "interruption": 0},
        ],
    }
    assert ab.permitted_probes(inputs) == ["free_look"]
    reply = store.load("probe_planner", inputs)
    assert reply is not None and reply["next_probe"] == "free_look"
    assert reply["hypotheses_to_discriminate"] == ["benign_wildlife", "unauthorized_uuv"]
    assert store.load("hypothesis_writer", inputs) is None
    assert store.load("probe_planner", {**inputs, "remaining_budget": {"probes": 0}}) is None


def test_sentence_reports_differences() -> None:
    rows = (
        ab.AblationRow("llm", "S05", 3, 40.0, 0, 2, 3),
        ab.AblationRow("deterministic", "S05", 4, 50.0, 0, 2, 0),
        ab.AblationRow("llm", "S07", 5, 38.0, 0, 2, 1),
        ab.AblationRow("deterministic", "S07", 5, 38.0, 0, 2, 0),
    )
    report = ab.AblationReport(("llm", "deterministic"), ("S05", "S07"), (), rows, 4)
    sentence = ab.ablation_sentence(report)
    assert "S05: probes 4 -> 3, s seen to handled 50.0 -> 40.0" in sentence
    assert "It added nothing on S07." in sentence
    same = ab.AblationReport(("llm", "deterministic"), ("S07",), (), rows[2:], 1)
    assert "added nothing measurable" in ab.ablation_sentence(same)


def test_replay_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    assert ab.main(["--mode", "record", "--scenarios", "S04"]) == ab.EXIT_REFUSED
    assert ab.main(["--mode", "live", "--scenarios", "S04"]) == ab.EXIT_REFUSED
    monkeypatch.setenv("SOUNDINGS_CI", "1")  # in CI a miss raises: the CLI reports it, exit 1
    assert ab.main(["--planners", "llm", "--scenarios", "S07"]) == 1
    with pytest.raises(ValueError, match="replay"):
        ab.run_ablation(("llm",), ("S04",), mode="record")
    with pytest.raises(ValueError, match="unknown planner"):
        ab.run_ablation(("oracle",), ("S04",))
