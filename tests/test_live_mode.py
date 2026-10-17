"""WO-087: bounded live mode (CLAUDE.md 2.2, 2.3, 2.4, 2.6; section 8 A4).

Every "live" call here goes to a stub transport in this file or ``tests/support/live_runs``;
nothing reaches a network and no fixture is written. The budget file always lives under
``tmp_path`` (``SOUNDINGS_LIVE_CALLS_PATH``), never in ``artifacts/``.

Exit tests: ``-k budget``, ``-k timeout``, ``-k error``, ``-k persisted``, ``-k ci_refuses``,
``-k guard_unchanged``, ``-k determinism``. The audit path rows are
``tests/test_audit.py -k llm_path``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from soundings.llm.client import (
    DEFAULT_TIMEOUT_S,
    REASON_BUDGET_EXHAUSTED,
    REASON_TIMEOUT,
    FixtureStore,
    LiveCallRefused,
    LlmCallRecord,
    ModelClient,
    ModelMode,
    TransportReply,
    estimate_tokens,
    last_path,
    live_client_from_env,
    reset_last_path,
)
from soundings.llm.live_budget import LiveBudget, budget_total_from_env
from soundings.llm.planner import LlmPlanner
from soundings.llm.roles import LLM_BUDGET_SPENT, ROLE_HYPOTHESIS_WRITER, ROLE_PROBE_PLANNER
from soundings.llm.sitrep_writer import SitrepPath
from tests.llm_cases import FIXTURE_ROOT, PLANNER_CASES, TABLES, make_table, threat_report
from tests.support.handoff_runs import REJECTION_CONTACT
from tests.support.live_runs import (
    STUB_MODEL,
    ExplodingTransport,
    FailingTransport,
    RecordingLlmPlanner,
    ScriptedTransport,
    SleepyTransport,
    handoff_s05,
    investigate,
)

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "paid-run-gate.py"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No CI flag (the live path is under test), no ambient budget, a tmp counter file."""
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    monkeypatch.delenv("LIVE_CALL_BUDGET", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.setenv("MODEL_MODE", "replay")
    counter = tmp_path / "artifacts" / "live_calls.json"
    monkeypatch.setenv("SOUNDINGS_LIVE_CALLS_PATH", str(counter))
    reset_last_path()
    return counter


def _counter(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "live_calls.json"


def _live(
    transport: Any, budget: LiveBudget | None, *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> ModelClient:
    return ModelClient(
        mode=ModelMode.LIVE,
        store=FixtureStore(Path("/nonexistent-fixture-root")),
        model_name=STUB_MODEL,
        live_budget=budget,
        timeout_s=timeout_s,
        transport=transport,
    )


def _replay_empty(tmp_path: Path) -> ModelClient:
    return ModelClient(mode=ModelMode.REPLAY, store=FixtureStore(tmp_path / "no-fixtures"))


def _spent(path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    spent = data["spent"]
    assert isinstance(spent, int)
    return spent


# --- budget: the hard cap, mid-investigation --------------------------------------------------


def test_budget_exhaustion_mid_investigation_falls_back_to_replay_outcome(tmp_path: Path) -> None:
    replay = investigate("S05", _replay_empty(tmp_path), tmp_path / "replay-violations.md")

    slot: list[RecordingLlmPlanner] = []
    transport = ScriptedTransport(lambda role, inputs: slot[0].deterministic_reply(role, inputs))
    budget = LiveBudget(_counter(tmp_path), total=2)
    client = _live(transport, budget)
    live = investigate("S05", client, tmp_path / "live-violations.md", planner_slot=slot)

    # the hard cap: two transport calls, then nothing reaches the transport
    assert len(transport.requests) == 2
    assert [c.path for c in client.call_log] == ["live", "live"] + ["fallback"] * (
        len(client.call_log) - 2
    )
    assert len(client.call_log) >= 3
    third = live.planner.choices[2]
    assert third.source == "fallback" and third.fallback_reason == REASON_BUDGET_EXHAUSTED
    assert [c.source for c in live.planner.choices[:2]] == ["llm", "llm"]
    # later decisions stop at the live cap, or at WO-032's per-contact call limit first
    assert {c.fallback_reason for c in live.planner.choices[2:]} <= {
        REASON_BUDGET_EXHAUSTED,
        LLM_BUDGET_SPENT,
    }
    # the fallback is the deterministic choice, so the investigation ends as replay does
    assert [c.probe_id for c in live.planner.choices] == [
        c.probe_id for c in replay.planner.choices
    ]
    assert live.result.state is replay.result.state
    assert live.result.recommendation is not None and replay.result.recommendation is not None
    assert live.result.recommendation.outcome is replay.result.recommendation.outcome
    assert live.result.recommendation.rule_id is replay.result.recommendation.rule_id
    assert live.result.recommendation.recommended_rung is (
        replay.result.recommendation.recommended_rung
    )
    assert live.result.events == replay.result.events
    # the persisted counter shows exactly the two spent calls
    assert _spent(_counter(tmp_path)) == 2
    assert LiveBudget(_counter(tmp_path), total=2).exhausted
    assert not (tmp_path / "live-violations.md").exists()


def test_budget_zero_makes_no_transport_call(tmp_path: Path) -> None:
    client = _live(ExplodingTransport(), LiveBudget(_counter(tmp_path), total=0))
    assert client.call(ROLE_PROBE_PLANNER, {"x": 1}) is None
    record = client.last_call
    assert record is not None
    assert (record.path, record.reason, record.latency_ms) == (
        "fallback",
        REASON_BUDGET_EXHAUSTED,
        0.0,
    )
    assert not _counter(tmp_path).exists()  # nothing spent, nothing written


def test_budget_from_env_caps_a_client_built_without_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIVE_CALL_BUDGET", "1")
    transport = ScriptedTransport(lambda role, inputs: {"ok": 1})
    client = ModelClient(mode=ModelMode.LIVE, model_name=STUB_MODEL, transport=transport)
    assert client.live_budget is not None and client.live_budget.total == 1
    assert client.call(ROLE_HYPOTHESIS_WRITER, {"a": 1}) == {"ok": 1}
    assert client.call(ROLE_HYPOTHESIS_WRITER, {"a": 2}) is None
    assert len(transport.requests) == 1
    assert _spent(_counter(tmp_path)) == 1


@pytest.mark.parametrize("raw", ["-1", "forty", "1.5"])
def test_budget_env_rejects_a_malformed_value(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_CALL_BUDGET", raw)
    with pytest.raises(ValueError, match="LIVE_CALL_BUDGET"):
        budget_total_from_env()


def test_budget_default_mode_stays_replay() -> None:
    assert ModelMode.from_env() is ModelMode.REPLAY
    client = live_client_from_env()
    assert client.mode is ModelMode.REPLAY and client.transport is None
    assert client.live_budget is None


# --- timeout ---------------------------------------------------------------------------------


def test_timeout_falls_back_within_6_5_s(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    transport = SleepyTransport(seconds=7.0)
    client = _live(transport, LiveBudget(_counter(tmp_path), total=5))
    assert client.timeout_s == 6.0
    caplog.set_level(logging.INFO, logger="soundings.llm.client")
    started = time.perf_counter()
    try:
        out = client.call(ROLE_PROBE_PLANNER, {"x": 1})
    finally:
        elapsed = time.perf_counter() - started
        transport.release()
    assert out is None
    assert 6.0 <= elapsed < 6.5
    assert transport.requests == 1
    record = client.last_call
    assert record is not None and record.path == "fallback" and record.reason == REASON_TIMEOUT
    assert record.latency_ms >= 6000.0
    assert any("path=fallback reason=timeout" in r.getMessage() for r in caplog.records)
    assert _spent(_counter(tmp_path)) == 1  # a timed-out call still counts against the cap


def test_timeout_short_limit_is_honored(tmp_path: Path) -> None:
    transport = SleepyTransport(seconds=5.0)
    client = _live(transport, LiveBudget(_counter(tmp_path), total=5), timeout_s=0.2)
    started = time.perf_counter()
    try:
        assert client.call(ROLE_PROBE_PLANNER, {"x": 1}) is None
    finally:
        transport.release()
    assert time.perf_counter() - started < 1.0
    assert client.last_path == "fallback"


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        ModelClient(mode=ModelMode.REPLAY, timeout_s=0.0)


# --- errors ----------------------------------------------------------------------------------


class _VendorOutage(Exception):
    pass


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("boom"),
        ValueError("bad json"),
        KeyError("content"),
        ConnectionError("reset"),
        TimeoutError("socket"),
        json.JSONDecodeError("Expecting value", "", 0),
        _VendorOutage("503"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_error_falls_back_and_logs_reason(
    error: BaseException, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    transport = FailingTransport(error)
    client = _live(transport, LiveBudget(_counter(tmp_path), total=3))
    caplog.set_level(logging.INFO, logger="soundings.llm.client")
    assert client.call(ROLE_PROBE_PLANNER, {"x": 1}) is None  # nothing propagates
    reason = f"error:{type(error).__name__}"
    record = client.last_call
    assert record is not None and (record.path, record.reason) == ("fallback", reason)
    assert transport.requests == 1
    assert any(f"path=fallback reason={reason}" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("reply", [["a list"], "text", 7, None])
def test_error_non_object_reply_falls_back(reply: Any, tmp_path: Path) -> None:
    client = _live(
        ScriptedTransport(lambda role, inputs: reply), LiveBudget(_counter(tmp_path), total=3)
    )
    assert client.call(ROLE_PROBE_PLANNER, {"x": 1}) is None
    assert client.last_call is not None and client.last_call.reason == "error:ValueError"


def test_error_in_every_decision_still_completes_the_investigation(tmp_path: Path) -> None:
    replay = investigate("S05", _replay_empty(tmp_path), tmp_path / "r.md")
    client = _live(FailingTransport(RuntimeError("down")), LiveBudget(_counter(tmp_path), 40))
    live = investigate("S05", client, tmp_path / "l.md")
    assert live.result.events == replay.result.events
    reasons = {c.fallback_reason for c in live.planner.choices}
    assert "error:RuntimeError" in reasons and reasons <= {"error:RuntimeError", LLM_BUDGET_SPENT}


# --- persisted counter -----------------------------------------------------------------------


def test_persisted_counter_survives_a_new_client(tmp_path: Path) -> None:
    path = _counter(tmp_path)
    first = _live(ScriptedTransport(lambda r, i: {"n": 1}), LiveBudget(path, total=3))
    assert first.call(ROLE_PROBE_PLANNER, {"x": 1}) == {"n": 1}
    assert first.call(ROLE_PROBE_PLANNER, {"x": 2}) == {"n": 1}
    assert json.loads(path.read_text(encoding="utf-8")) == {"spent": 2, "total": 3}

    transport = ScriptedTransport(lambda r, i: {"n": 2})
    second = _live(transport, LiveBudget(path, total=3))
    assert second.live_budget is not None and second.live_budget.remaining == 1
    assert second.call(ROLE_PROBE_PLANNER, {"x": 3}) == {"n": 2}
    assert second.call(ROLE_PROBE_PLANNER, {"x": 4}) is None
    assert len(transport.requests) == 1
    assert _spent(path) == 3
    # the write is atomic: no temporary sibling is left behind
    assert sorted(p.name for p in path.parent.iterdir()) == ["live_calls.json"]


def test_persisted_counter_restores_from_env_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _counter(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"spent": 39, "total": 40}), encoding="utf-8")
    monkeypatch.setenv("LIVE_CALL_BUDGET", "40")
    budget = LiveBudget.from_env()
    assert budget is not None and budget.path == path
    assert (budget.total, budget.spent, budget.remaining) == (40, 39, 1)


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        "",
        '{"spent": -1, "total": 3}',
        '{"spent": "1", "total": 3}',
        '{"spent": true, "total": 3}',
        '{"total": 3}',
        "[1, 2]",
        '{"spent": 1',
    ],
)
def test_persisted_corrupt_file_counts_as_exhausted(body: str, tmp_path: Path) -> None:
    path = _counter(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    budget = LiveBudget(path, total=40)
    assert budget.corrupt and budget.exhausted and budget.remaining == 0
    client = _live(ExplodingTransport(), budget)
    assert client.call(ROLE_PROBE_PLANNER, {"x": 1}) is None
    assert client.last_call is not None and client.last_call.reason == REASON_BUDGET_EXHAUSTED
    assert path.read_text(encoding="utf-8") == body  # left for a person to inspect


# --- CI refuses ---------------------------------------------------------------------------------


def test_ci_refuses_live_before_any_transport_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOUNDINGS_CI", "1")
    budget = LiveBudget(_counter(tmp_path), total=40)
    client = _live(ExplodingTransport(), budget)
    with pytest.raises(LiveCallRefused, match="SOUNDINGS_CI"):
        client.call(ROLE_PROBE_PLANNER, {"x": 1})
    assert budget.spent == 0 and not _counter(tmp_path).exists()
    assert client.call_log == []


def test_ci_refuses_live_client_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOUNDINGS_CI", "1")
    monkeypatch.setenv("MODEL_MODE", "live")
    monkeypatch.setenv("LIVE_CALL_BUDGET", "40")
    with pytest.raises(LiveCallRefused, match="SOUNDINGS_CI"):
        live_client_from_env()


def test_ci_refuses_live_without_a_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_MODE", "live")
    with pytest.raises(LiveCallRefused, match="LIVE_CALL_BUDGET"):
        live_client_from_env()
    monkeypatch.setenv("MODEL_MODE", "record")
    with pytest.raises(LiveCallRefused, match="record"):
        live_client_from_env()


def test_ci_refuses_live_client_from_env_wires_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_MODE", "live")
    monkeypatch.setenv("LIVE_CALL_BUDGET", "40")
    client = live_client_from_env()  # constructing makes no call
    assert client.mode is ModelMode.LIVE and client.transport is not None
    assert client.live_budget is not None and client.live_budget.total == 40
    assert client.live_budget.path == _counter(tmp_path)
    assert client.timeout_s == 6.0


def _gate(
    tmp_path: Path, typed: str, *, ci: bool, extra: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "SOUNDINGS_CI"}
    env |= {"SOUNDINGS_USD_PER_MTOK_IN": "1", "SOUNDINGS_USD_PER_MTOK_OUT": "1"}
    if ci:
        env["SOUNDINGS_CI"] = "1"
    args = ["--role", "stage_live", "--calls", "40", "--in-tokens", "2500", "--out-tokens", "300"]
    return subprocess.run(
        [sys.executable, str(GATE), *args, *extra],
        input=typed + "\n",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        check=False,
    )


def test_ci_refuses_gate_script(tmp_path: Path) -> None:
    proc = _gate(tmp_path, "approve paid run stage live budget 40", ci=True)
    assert proc.returncode == 2
    assert "forbidden in CI" in proc.stderr
    assert not (tmp_path / "docs").exists()


def test_ci_refuses_gate_stage_live_wording(tmp_path: Path) -> None:
    proc = _gate(tmp_path, "approve paid run stage live budget 40", ci=False)
    assert proc.returncode == 0, proc.stderr
    assert "Type exactly:  approve paid run stage live budget 40" in proc.stdout
    assert "MODEL_MODE=live LIVE_CALL_BUDGET=40" in proc.stdout
    assert "6 s per call" in proc.stdout
    log = (tmp_path / "docs" / "log" / "PAID-RUNS.md").read_text(encoding="utf-8")
    assert "| stage_live | 40 |" in log and "stage live budget 40" in log

    (tmp_path / "w").mkdir()
    wrong = _gate(tmp_path / "w", "approve paid run", ci=False)
    assert wrong.returncode == 1
    assert not (tmp_path / "w" / "docs").exists()


# --- guard unchanged -------------------------------------------------------------------------


@pytest.mark.parametrize("case_name", ["names_rung", "names_tool_call", "names_approval"])
def test_guard_unchanged_live_reply_rejected_like_a_fixture(case_name: str, tmp_path: Path) -> None:
    case = PLANNER_CASES[case_name]
    fixtures = FixtureStore(FIXTURE_ROOT)
    recorded = fixtures.load(ROLE_PROBE_PLANNER, case.inputs())
    assert recorded is not None

    def choose(client: ModelClient, log: Path) -> Any:
        return LlmPlanner(client, violations_path=log).choose(
            case.state(),
            TABLES,
            table=make_table(),
            report=threat_report(),
            asset_context=case.asset_context,
            when=f"t/{case_name}",
        )

    replay_log, live_log = tmp_path / "replay.md", tmp_path / "live.md"
    from_fixture = choose(ModelClient(mode=ModelMode.REPLAY, store=fixtures), replay_log)
    transport = ScriptedTransport(lambda role, inputs: dict(recorded))
    client = _live(transport, LiveBudget(_counter(tmp_path), total=5))
    from_live = choose(client, live_log)

    assert len(transport.requests) == 1 and client.last_path == "live"
    assert case.expected is not None
    assert from_fixture.fallback_reason == case.expected.value
    assert from_live == from_fixture  # same rejection, same deterministic fallback
    assert live_log.read_text(encoding="utf-8") == replay_log.read_text(encoding="utf-8")


def test_guard_unchanged_live_sitrep_rung_change_rejected(tmp_path: Path) -> None:
    fixtures = FixtureStore(FIXTURE_ROOT)

    def answer(role: str, inputs: Any) -> TransportReply:
        out = fixtures.load(role, inputs)
        assert out is not None
        return TransportReply(out, tokens_in=1000, tokens_out=200)

    replay = handoff_s05(
        tmp_path / "replay",
        ModelClient(mode=ModelMode.REPLAY, store=fixtures),
        cid=REJECTION_CONTACT,
    )
    client = _live(ScriptedTransport(answer), LiveBudget(_counter(tmp_path), total=5))
    live = handoff_s05(tmp_path / "live", client, cid=REJECTION_CONTACT)
    (final_replay,) = [p for p in replay.service.packets if p.status.value == "final"]
    (final_live,) = [p for p in live.service.packets if p.status.value == "final"]
    assert final_live.path is SitrepPath.FALLBACK_AFTER_REJECTION
    assert final_live.path is final_replay.path
    assert final_live.violation == final_replay.violation == "recommended_rung_mismatch"
    assert final_live.markdown == final_replay.markdown
    assert final_live.sitrep.recommended_rung == final_replay.sitrep.recommended_rung


# --- call log --------------------------------------------------------------------------------


def test_call_log_records_role_tokens_latency_path(tmp_path: Path) -> None:
    transport = ScriptedTransport(lambda r, i: TransportReply({"k": "v"}, 321, 45))
    client = _live(transport, LiveBudget(_counter(tmp_path), total=1))
    client.call(ROLE_PROBE_PLANNER, {"x": 1}, system="sys")
    client.call(ROLE_PROBE_PLANNER, {"x": 2}, system="sys")
    live_rec, spent_rec = client.call_log
    assert isinstance(live_rec, LlmCallRecord)
    assert (live_rec.role, live_rec.path, live_rec.reason) == (ROLE_PROBE_PLANNER, "live", None)
    assert (live_rec.tokens_in, live_rec.tokens_out, live_rec.tokens_estimated) == (321, 45, False)
    assert live_rec.latency_ms >= 0.0
    assert spent_rec.path == "fallback" and spent_rec.tokens_out == 0
    assert spent_rec.tokens_in == estimate_tokens("sys" + '{"x":2}')
    assert last_path() == "fallback"

    replay = ModelClient(mode=ModelMode.REPLAY, store=FixtureStore(FIXTURE_ROOT))
    case = PLANNER_CASES["valid"]
    assert replay.call(ROLE_PROBE_PLANNER, case.inputs()) is not None
    rec = replay.last_call
    assert rec is not None and rec.path == "replay" and rec.latency_ms == 0.0
    assert last_path() == "replay"


# --- determinism -----------------------------------------------------------------------------


def test_determinism_budget_zero_s05_seed11(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIVE_CALL_BUDGET", "0")
    logs: list[list[str]] = []
    for i in range(2):
        client = ModelClient(
            mode=ModelMode.LIVE, model_name=STUB_MODEL, transport=ExplodingTransport()
        )
        assert client.live_budget is not None and client.live_budget.total == 0
        run = investigate("S05", client, tmp_path / f"v{i}.md", seed=11)
        assert {c.reason for c in client.call_log} == {REASON_BUDGET_EXHAUSTED}
        logs.append(run.result.transition_log_lines())
    assert logs[0] == logs[1]
    replay = investigate("S05", _replay_empty(tmp_path), tmp_path / "r.md", seed=11)
    assert replay.result.transition_log_lines() == logs[0]
    assert not _counter(tmp_path).exists()
