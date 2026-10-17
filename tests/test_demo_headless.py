"""WO-062 Part A: headless demo execution, offline, deterministic, replay only."""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from soundings.demo.__main__ import EXIT_REFUSED, app
from soundings.demo.headless import (
    EVAL_PENDING,
    LIVE_PLACEHOLDER,
    CaseView,
    RunnerV1Executor,
    eval_headline,
    headline,
    run_demo,
    with_toggles,
)
from soundings.demo.schedule import Pace
from soundings.demo.script import DemoScript, DemoStep
from soundings.schemas.enums import LadderState, ToggleName
from soundings.schemas.scenario import Scenario
from soundings.sim.scenario import resolve_scenario

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "docs" / "demo-script.yaml"
S = LadderState


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Any socket connect to a non-loopback address raises."""
    real = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
            raise AssertionError(f"network call to {host!r} during the demo")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield


def _two_step(tmp_path: Path) -> Path:
    path = tmp_path / "two.yaml"
    path.write_text(
        "steps:\n"
        '  - {t: "0:00", scenario: S01, expect_state: CLEARED_AT_ASK, say: "Filed survey."}\n'
        '  - {t: "0:20", eval: true, say: "Three policies."}\n',
        encoding="utf-8",
    )
    return path


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, tuple[str, ...]]] = []

    def run_scenario(self, scenario: Scenario, seed: int) -> Sequence[CaseView]:
        self.calls.append((scenario.id, seed, tuple(t.name.value for t in scenario.toggles)))
        return [CaseView("c-0001#1", S.CLEARED_AT_ASK, 9, True)]


def test_two_step_script_completes_offline(
    tmp_path: Path, offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_MODE", "replay")
    out = tmp_path / "t.txt"
    res = CliRunner().invoke(
        app,
        [
            "--script",
            str(_two_step(tmp_path)),
            "--mode",
            "replay",
            "--pace",
            "fast",
            "--transcript",
            str(out),
            "--eval-report",
            str(tmp_path / "none.json"),
        ],
    )
    assert res.exit_code == 0, res.output
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("[0:00] S01 seed=1 -> CLEARED_AT_ASK tick=")
    assert lines[0].endswith("| say: Filed survey.")
    assert lines[1] == f"[0:20] eval -> {EVAL_PENDING} | say: Three policies."


def test_full_script_fast_offline(tmp_path: Path, offline: None) -> None:
    script = DemoScript.load(SCRIPT)
    seen: list[str] = []
    result = run_demo(script, pace=Pace.FAST, eval_report=tmp_path / "none.json", emit=seen.append)
    assert result.ok
    assert seen == [line.render() for line in result.lines]
    says = [s.say for s in script.steps]
    assert [line.say for line in result.lines] == says
    assert [line.mark for line in result.lines] == [s.t for s in script.steps]
    assert result.lines[0].state is S.CLEARED_AT_ASK
    assert result.lines[1].state is S.LOST_VEHICLE
    assert result.lines[4].note == LIVE_PLACEHOLDER
    assert result.lines[5].label == "twist S05+network_down"
    assert result.lines[5].state is not None
    assert result.lines[6].note == EVAL_PENDING
    pattern = re.compile(r"^\[\d+:\d\d\] \S.* -> .+ \| say: .+$")
    assert all(pattern.match(line.render()) for line in result.lines)


def test_steps_route_to_the_executor() -> None:
    rec = _Recorder()
    script = DemoScript.load(SCRIPT)
    result = run_demo(script, pace=Pace.FAST, executor=rec, eval_report=Path("/nonexistent"))
    # S01, S02, S05 once (the second S05 continues the run), the twist on S05.
    assert [(c[0], c[1]) for c in rec.calls] == [("S01", 1), ("S02", 3), ("S05", 11), ("S05", 11)]
    assert "network_down" in rec.calls[3][2]
    assert "network_down" not in rec.calls[2][2]
    assert result.lines[3].note is not None and "continued" in result.lines[3].note


def test_expect_state_mismatch_fails(tmp_path: Path) -> None:
    script = DemoScript.model_validate(
        {"steps": ({"t": "0:00", "scenario": "S01", "say": "x", "expect_state": "HANDED_OFF"},)}
    )
    result = run_demo(script, pace=Pace.FAST, executor=_Recorder(), eval_report=tmp_path / "n")
    assert not result.ok
    assert "[expected HANDED_OFF]" in result.lines[0].render()


def test_paced_waits_follow_the_schedule(tmp_path: Path) -> None:
    now = [0.0]
    waits: list[float] = []

    def sleep(d: float) -> None:
        waits.append(round(d, 6))
        now[0] += d

    script = DemoScript.load(_two_step(tmp_path))
    run_demo(
        script,
        pace=Pace.REALTIME,
        tail_s=5.0,
        speed=2.0,
        executor=_Recorder(),
        eval_report=tmp_path / "n",
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert waits == [10.0, 2.5]


def test_seed_override_and_toggles() -> None:
    step = DemoStep.model_validate(
        {
            "t": "0:00",
            "scenario": "twist",
            "scenario_ref": "S05",
            "toggles": ("network_down",),
            "say": "x",
        }
    )
    base = resolve_scenario("S05")
    twisted = with_toggles(base, step)
    assert ToggleName.NETWORK_DOWN in {t.name for t in twisted.toggles}
    assert with_toggles(twisted, step) is twisted
    rec = _Recorder()
    seeded = DemoScript.model_validate(
        {"steps": ({"t": "0:00", "scenario": "S01", "seed": 4, "say": "x"},)}
    )
    run_demo(seeded, pace=Pace.FAST, executor=rec, eval_report=Path("/nonexistent"))
    assert rec.calls[0][1] == 4


def test_headline_prefers_ground_truth_and_rank() -> None:
    cases = [
        CaseView("c-0001#1", S.CLEARED_AT_ASK, 30, True),
        CaseView("c-0002#1", S.INVESTIGATING, 40, False),
        CaseView("c-0003#1", S.LOST_VEHICLE, 90, True),
        CaseView("c-0004#1", S.LOST_VEHICLE, 70, True),
    ]
    out = headline(cases, seed=3)
    assert (out.state, out.tick, out.seed) == (S.LOST_VEHICLE, 90, 3)
    assert dict(out.cases) == {"CLEARED_AT_ASK": 1, "INVESTIGATING": 1, "LOST_VEHICLE": 2}
    only_fa = headline([CaseView("c-0002#1", S.INVESTIGATING, 40, False)], seed=1)
    assert only_fa.state is S.INVESTIGATING


def test_eval_headline(tmp_path: Path) -> None:
    assert eval_headline(tmp_path / "missing.json") == EVAL_PENDING
    p = tmp_path / "report.json"
    p.write_text(json.dumps({"headline": "7 of 9 at or below ask"}), encoding="utf-8")
    assert eval_headline(p) == "eval: 7 of 9 at or below ask"
    p.write_text(json.dumps({"policies": {"soundings": {}, "bang": {}, "human_queue": {}}}))
    assert eval_headline(p) == "eval: policies bang, human_queue, soundings"


def test_runner_v1_executor_s01() -> None:
    views = RunnerV1Executor().run_scenario(resolve_scenario("S01"), 1)
    assert headline(views, seed=1).state is S.CLEARED_AT_ASK


def test_transcript_deterministic(tmp_path: Path, offline: None) -> None:
    outs = []
    for name in ("a.txt", "b.txt"):
        path = tmp_path / name
        res = CliRunner().invoke(
            app,
            [
                "--script",
                str(SCRIPT),
                "--mode",
                "replay",
                "--pace",
                "fast",
                "--transcript",
                str(path),
                "--eval-report",
                str(tmp_path / "none.json"),
            ],
        )
        assert res.exit_code == 0, res.output
        outs.append(path.read_text(encoding="utf-8"))
    assert outs[0] == outs[1]
    assert "elapsed" not in outs[0]


@pytest.mark.parametrize("mode", ["live", "record"])
def test_live_mode_refused(mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from soundings.llm import client as llm_client

    constructed: list[int] = []
    real_init = llm_client.ModelClient.__init__

    def spy(self: Any, *a: Any, **kw: Any) -> None:
        constructed.append(1)
        real_init(self, *a, **kw)

    monkeypatch.setattr(llm_client.ModelClient, "__init__", spy)
    res = CliRunner().invoke(app, ["--script", str(SCRIPT), "--mode", mode])
    assert res.exit_code == EXIT_REFUSED == 3
    assert "approve paid run" in res.output
    assert constructed == []


def test_unknown_mode_and_bad_script(tmp_path: Path) -> None:
    assert CliRunner().invoke(app, ["--script", str(SCRIPT), "--mode", "x"]).exit_code == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text('steps:\n  - {t: "0:00", scenario: S99, say: "x"}\n', encoding="utf-8")
    assert CliRunner().invoke(app, ["--script", str(bad), "--mode", "replay"]).exit_code == 2


def test_dry_run_prints_schedule_without_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **kw: Any) -> None:
        raise AssertionError("dry run must not execute steps")

    monkeypatch.setattr("soundings.demo.headless.run_demo", boom)
    res = CliRunner().invoke(
        app, ["--script", str(SCRIPT), "--mode", "replay", "--dry-run", "--pace", "video"]
    )
    assert res.exit_code == 0, res.output
    lines = res.output.strip().splitlines()
    assert len(lines) == 8
    assert lines[0].startswith("[0:00] at=+0.000000s S01")
    assert lines[-1] == "total=+170.000000s pace=video"


def test_makefile_demo_line_unchanged() -> None:
    text = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "uv run python -m soundings.demo --script docs/demo-script.yaml --mode replay" in text


def test_demo_never_imports_the_model_layer() -> None:
    for path in sorted((REPO / "soundings" / "demo").glob("*.py")):
        assert "soundings.llm" not in path.read_text(encoding="utf-8"), path.name
