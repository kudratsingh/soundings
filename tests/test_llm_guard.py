"""WO-032: the guard, the violation log, fallbacks, ``LlmPlanner`` and the client paths.

CLAUDE.md 2.2 (no model output becomes a control), 2.3 (no paid runs: every model call
here is a fixture, an empty store or a stub transport), 2.4 (determinism) and section 5
(every role falls back with zero fixtures).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import soundings.schemas.denylist as denylist_mod
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import build_evidence
from soundings.investigation.planner import (
    BudgetLedger,
    DeterministicPlanner,
    Planner,
    PlannerChoice,
    state_from_table,
)
from soundings.investigation.pol import load_pol_table
from soundings.investigation.posterior import (
    PosteriorReport,
    load_prior_config,
    score_contact,
)
from soundings.investigation.probes import ALLOWLIST as PROBE_ALLOWLIST
from soundings.investigation.probes import ProbeCache, ProbeContext, run_probe
from soundings.llm.client import (
    FixtureStore,
    LiveCallRefused,
    ModelClient,
    ModelMode,
    ModelOutput,
    ReplayMiss,
    fixture_key,
)
from soundings.llm.fallback import (
    GENERIC_QUESTION,
    PAIR_QUESTIONS,
    fallback_probe_plan,
    rule_based_probe_order,
    template_hypotheses,
    template_question,
)
from soundings.llm.guard import (
    VIOLATIONS_HEADER,
    GuardResult,
    ProbePlanOutput,
    RemainingBudget,
    ViolationCode,
    ViolationRow,
    append_violation,
    check_hypothesis_set,
    check_probe_plan,
)
from soundings.llm.planner import LlmPlanner
from soundings.llm.roles import (
    FALLBACK_DETERMINISTIC_PLANNER,
    FALLBACK_RULE_ORDER,
    FALLBACK_TEMPLATE_HYPOTHESES,
    LLM_BUDGET_SPENT,
    REPLAY_MISS,
    ROLE_HYPOTHESIS_WRITER,
    ROLE_PROBE_PLANNER,
    HypothesisWriter,
    ProbePlannerRole,
)
from soundings.registry.store import RegistryStore
from soundings.schemas.enums import Hypothesis, ProbeId, Rung
from soundings.schemas.harbor import load_harbor
from soundings.sim.current import load_current_field
from soundings.sim.run import run_scenario
from soundings.sim.scenario import resolve_scenario
from tests.llm_cases import (
    ALLOWLIST,
    FIXTURE_ROOT,
    PLANNER_CASES,
    TABLES,
    VIOLATING_PLANNER_CASES,
    WRITER_CASES,
    asset,
    benign_report,
    make_ctx,
    make_table,
    settled_report,
    threat_report,
)

LLM_DIR = Path(__file__).resolve().parent.parent / "soundings" / "llm"
EIGHT = {h.value for h in Hypothesis}
TABLE_ROW = re.compile(r"^\| .* \|$")


def _replay_client(root: Path = FIXTURE_ROOT) -> ModelClient:
    return ModelClient(mode=ModelMode.REPLAY, store=FixtureStore(root))


def _rows(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    header_end = lines.index("|---|---|---|---|---|")
    return lines[header_end + 1 :]


def _plan(**over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "next_probe": "acoustic_hail",
        "why": "A hail reply separates the top two.",
        "hypotheses_to_discriminate": ["UNAUTHORIZED_UUV", "FILED_OFF_PLAN"],
        "question_for_human": None,
    }
    raw.update(over)
    return raw


def _fixture_output(role: str, inputs: dict[str, Any]) -> ModelOutput:
    out = FixtureStore(FIXTURE_ROOT).load(role, inputs)
    assert out is not None
    return out


class CountingClient(ModelClient):
    """Returns a fixed reply and counts calls; never touches a store or a network."""

    def __init__(self, reply: ModelOutput | None) -> None:
        super().__init__(mode=ModelMode.REPLAY, store=FixtureStore(Path("/nonexistent")))
        self.reply = reply
        self.calls = 0

    def call(self, role: str, inputs: Any, *, system: str | None = None) -> ModelOutput | None:
        self.calls += 1
        return self.reply


class StubTransport:
    def __init__(self, reply: ModelOutput) -> None:
        self.reply = reply
        self.requests: list[dict[str, Any]] = []

    def __call__(self, *, model: str, role: str, system: str | None, inputs: Any) -> ModelOutput:
        self.requests.append({"model": model, "role": role, "system": system, "inputs": inputs})
        return dict(self.reply)


# --- the six violating planner fixtures --------------------------------------------------


@pytest.mark.parametrize("name", VIOLATING_PLANNER_CASES)
def test_violating_fixtures_rejected_and_logged(name: str, tmp_path: Path) -> None:
    case = PLANNER_CASES[name]
    log = tmp_path / "VIOLATIONS.md"
    role = ProbePlannerRole(_replay_client(), violations_path=log)
    report = threat_report()
    outcome = role.propose(make_table(), report, case.ctx, case.asset_context, when=f"t/{name}")

    assert case.expected is not None
    assert outcome.source == "fallback"
    assert outcome.violation is case.expected
    assert outcome.fallback_reason == case.expected.value
    assert outcome.value == fallback_probe_plan(case.ctx, report)
    rows = _rows(log)
    assert len(rows) == 1
    key = fixture_key(ROLE_PROBE_PLANNER, case.inputs())
    assert outcome.fixture_key == key
    cells = [c.strip() for c in rows[0].strip("|").split(" | ")]
    assert cells[0] == f"t/{name}"
    assert cells[1] == ROLE_PROBE_PLANNER
    assert cells[2] == key
    assert cells[3].startswith(case.expected.value)
    assert cells[4] == FALLBACK_RULE_ORDER


@pytest.mark.parametrize("name", VIOLATING_PLANNER_CASES)
def test_violating_fixtures_use_supplied_planner_fallback(name: str, tmp_path: Path) -> None:
    """A caller-supplied planner choice (the WO-031 planner in part B) replaces the reply."""
    case = PLANNER_CASES[name]
    log = tmp_path / "v.md"
    report = threat_report()
    chosen = fallback_probe_plan(case.ctx, report, ProbeId.ASK_OPERATOR_BY_PHONE)
    outcome = ProbePlannerRole(_replay_client(), violations_path=log).propose(
        make_table(), report, case.ctx, case.asset_context, when="w", fallback=lambda: chosen
    )
    assert outcome.value == chosen
    assert _rows(log)[0].endswith(f"| {FALLBACK_DETERMINISTIC_PLANNER} |")


def test_violating_cases_cover_every_required_code() -> None:
    codes = {PLANNER_CASES[n].expected for n in VIOLATING_PLANNER_CASES}
    assert codes == {
        ViolationCode.NAMES_RUNG,
        ViolationCode.NAMES_TOOL_CALL,
        ViolationCode.NAMES_APPROVAL,
        ViolationCode.OVER_BUDGET,
        ViolationCode.ABOVE_CURRENT_RUNG,
        ViolationCode.NOT_IN_ALLOWLIST,
    }


def test_valid_fixture_accepted(tmp_path: Path) -> None:
    case = PLANNER_CASES["valid"]
    log = tmp_path / "VIOLATIONS.md"
    outcome = ProbePlannerRole(_replay_client(), violations_path=log).propose(
        make_table(), threat_report(), case.ctx, case.asset_context, when="t"
    )
    assert outcome.source == "llm"
    assert outcome.violation is None and outcome.fallback_reason is None
    assert not log.exists()
    plan = outcome.value
    assert isinstance(plan, ProbePlanOutput)
    probe = ProbeId(plan.next_probe)
    assert probe in ALLOWLIST
    assert case.ctx.remaining.allows(ALLOWLIST[probe]) is None


# --- hypothesis set ----------------------------------------------------------------------


def test_hypothesis_set_valid_accepted(tmp_path: Path) -> None:
    log = tmp_path / "v.md"
    outcome = HypothesisWriter(_replay_client(), violations_path=log).write(
        threat_report(), make_table(), when="t"
    )
    assert outcome.source == "llm"
    assert {e.id for e in outcome.value.hypotheses} <= EIGHT
    assert not log.exists()


def test_hypothesis_set_ninth_rejected_template_used(tmp_path: Path) -> None:
    log = tmp_path / "v.md"
    report = benign_report()
    outcome = HypothesisWriter(_replay_client(), violations_path=log).write(
        report, make_table(), when="t"
    )
    assert outcome.violation is ViolationCode.HYPOTHESIS_OUTSIDE_SET
    assert outcome.source == "fallback"
    assert outcome.value == template_hypotheses(report)
    assert {e.id for e in outcome.value.hypotheses} <= EIGHT
    rows = _rows(log)
    assert len(rows) == 1
    key = fixture_key(ROLE_HYPOTHESIS_WRITER, WRITER_CASES["ninth_hypothesis"].inputs())
    assert f"| {key} | HYPOTHESIS_OUTSIDE_SET" in rows[0]
    assert rows[0].endswith(f"| {FALLBACK_TEMPLATE_HYPOTHESES} |")


def test_hypothesis_set_recommended_rung_rejected(tmp_path: Path) -> None:
    raw = {
        "hypotheses": [{"id": "UNAUTHORIZED_UUV", "why": "Powered transit."}],
        "question_for_human": None,
        "recommended_rung": "hold",
    }
    assert check_hypothesis_set(raw).violation is ViolationCode.NAMES_RUNG
    log = tmp_path / "v.md"
    outcome = HypothesisWriter(CountingClient(raw), violations_path=log).write(
        threat_report(), make_table(), when="t"
    )
    assert outcome.violation is ViolationCode.NAMES_RUNG
    assert len(_rows(log)) == 1


def test_hypothesis_set_rejects_probability_and_duplicates() -> None:
    entry = {"id": "BIOLOGIC", "why": "Clicks."}
    with_p = {"hypotheses": [{**entry, "p": 0.9}], "question_for_human": None}
    assert check_hypothesis_set(with_p).violation is ViolationCode.SCHEMA_INVALID
    dup = {"hypotheses": [entry, entry], "question_for_human": None}
    assert check_hypothesis_set(dup).violation is ViolationCode.SCHEMA_INVALID
    assert check_hypothesis_set([entry]).violation is ViolationCode.SCHEMA_INVALID


@settings(derandomize=True, max_examples=60, deadline=None)
@given(st.text(min_size=1, max_size=30).filter(lambda s: s not in EIGHT))
def test_hypothesis_set_never_adds_a_hypothesis(new_id: str) -> None:
    raw = {
        "hypotheses": [{"id": "BIOLOGIC", "why": "Clicks."}, {"id": new_id, "why": "New."}],
        "question_for_human": None,
    }
    result = check_hypothesis_set(raw)
    assert not result.accepted
    assert result.value is None


def test_hypothesis_set_template_only_uses_the_eight() -> None:
    for report in (threat_report(), benign_report(), settled_report()):
        out = template_hypotheses(report)
        assert len(out.hypotheses) == 3
        assert {e.id for e in out.hypotheses} <= EIGHT
        assert check_hypothesis_set(out.model_dump(mode="json")).accepted


# --- fixtures and misses -----------------------------------------------------------------


def test_fixture_keys_consistent() -> None:
    files = sorted((FIXTURE_ROOT / ROLE_PROBE_PLANNER).glob("*.json")) + sorted(
        (FIXTURE_ROOT / ROLE_HYPOTHESIS_WRITER).glob("*.json")
    )
    assert len(files) == len(PLANNER_CASES) + len(WRITER_CASES)
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        assert set(record) == {"role", "key", "inputs", "output"}
        assert record["key"] == fixture_key(record["role"], record["inputs"]) == path.stem
        assert path.parent.name == record["role"]
    keys = {p.stem for p in files}
    assert {fixture_key(ROLE_PROBE_PLANNER, c.inputs()) for c in PLANNER_CASES.values()} <= keys
    assert {fixture_key(ROLE_HYPOTHESIS_WRITER, c.inputs()) for c in WRITER_CASES.values()} <= keys


def test_no_fixtures_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    empty = tmp_path / "store"
    log = tmp_path / "v.md"
    case = PLANNER_CASES["valid"]
    report = threat_report()
    planner = ProbePlannerRole(_replay_client(empty), violations_path=log)
    p_out = planner.propose(make_table(), report, case.ctx, case.asset_context, when="t")
    w_out = HypothesisWriter(_replay_client(empty), violations_path=log).write(
        report, make_table(), when="t"
    )
    assert (p_out.source, p_out.fallback_reason, p_out.violation) == ("fallback", REPLAY_MISS, None)
    assert (w_out.source, w_out.fallback_reason, w_out.violation) == ("fallback", REPLAY_MISS, None)
    assert p_out.value == fallback_probe_plan(case.ctx, report)
    assert w_out.value == template_hypotheses(report)
    assert not log.exists()

    monkeypatch.setenv("SOUNDINGS_CI", "1")
    with pytest.raises(ReplayMiss):
        ProbePlannerRole(_replay_client(empty), violations_path=log).propose(
            make_table(), report, case.ctx, case.asset_context, when="t"
        )
    with pytest.raises(ReplayMiss):
        HypothesisWriter(_replay_client(empty), violations_path=log).write(
            report, make_table(), when="t"
        )
    assert not log.exists()


def test_never_loops(tmp_path: Path) -> None:
    rejected = _plan(next_probe="hold")
    stub = CountingClient(rejected)
    log = tmp_path / "v.md"
    planner = ProbePlannerRole(stub, violations_path=log)
    assert planner.call_limit == 4  # config/ladder.yaml budgets.llm_calls_per_contact
    ctx = make_ctx()
    reasons = [
        planner.propose(make_table(), threat_report(), ctx, asset(400.0), when=f"t{i}")
        for i in range(10)
    ]
    assert stub.calls == 4
    assert planner.calls_made(ctx.contact_id) == 4
    assert [r.fallback_reason for r in reasons[:4]] == ["NAMES_RUNG"] * 4
    assert [r.fallback_reason for r in reasons[4:]] == [LLM_BUDGET_SPENT] * 6
    assert all(r.value == fallback_probe_plan(ctx, threat_report()) for r in reasons)
    assert len(_rows(log)) == 4  # one row per rejected call, no retry


# --- violation log -----------------------------------------------------------------------


def test_violation_log_format(tmp_path: Path) -> None:
    log = tmp_path / "nested" / "VIOLATIONS.md"
    row = ViolationRow(
        "S05/seed11/t=212",
        ROLE_PROBE_PLANNER,
        "ab" * 32,
        ViolationCode.NAMES_RUNG,
        "deterministic_planner",
        "next_probe 'a|b'",
    )
    append_violation(row, log)
    text = log.read_text(encoding="utf-8")
    assert text.startswith(VIOLATIONS_HEADER)
    assert text[len(VIOLATIONS_HEADER) :] == (
        f"| S05/seed11/t=212 | probe_planner | {'ab' * 32} | NAMES_RUNG: next_probe 'a\\|b' "
        "| deterministic_planner |\n"
    )
    append_violation(row, log)
    rows = _rows(log)
    assert len(rows) == 2 and rows[0] == rows[1]
    assert all(TABLE_ROW.match(r) for r in rows)
    # unescaped pipes split exactly into the five header columns
    assert len(re.split(r"(?<!\\)\|", rows[0])) == 7


def test_violation_log_header_matches_repo_file() -> None:
    repo_log = LLM_DIR.parent.parent / "docs" / "log" / "VIOLATIONS.md"
    assert repo_log.read_text(encoding="utf-8").startswith(VIOLATIONS_HEADER)


def test_violation_log_appends_after_missing_newline(tmp_path: Path) -> None:
    log = tmp_path / "v.md"
    log.write_text(VIOLATIONS_HEADER.rstrip("\n"), encoding="utf-8")
    append_violation(ViolationRow("w", "r", "k", ViolationCode.SCHEMA_INVALID, "f"), log)
    assert log.read_text(encoding="utf-8").endswith(
        "|---|---|---|---|---|\n| w | r | k | SCHEMA_INVALID | f |\n"
    )


# --- determinism -------------------------------------------------------------------------


def test_guard_deterministic(tmp_path: Path) -> None:
    logs = [tmp_path / "a.md", tmp_path / "b.md"]
    for log in logs:
        for name, case in PLANNER_CASES.items():
            raw = _fixture_output(ROLE_PROBE_PLANNER, case.inputs())
            first, second = check_probe_plan(raw, case.ctx), check_probe_plan(raw, case.ctx)
            assert first == second
            assert first.violation is case.expected
            if first.violation is not None:
                append_violation(
                    ViolationRow(
                        f"t/{name}", ROLE_PROBE_PLANNER, "k", first.violation, "f", first.detail
                    ),
                    log,
                )
        for wcase in WRITER_CASES.values():
            raw = _fixture_output(ROLE_HYPOTHESIS_WRITER, wcase.inputs())
            assert check_hypothesis_set(raw) == check_hypothesis_set(raw)
            assert check_hypothesis_set(raw).violation is wcase.expected
    assert logs[0].read_bytes() == logs[1].read_bytes()


# --- guard classification ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (_plan(next_probe="acoustic_hail()"), ViolationCode.NAMES_TOOL_CALL),
        (_plan(next_probe="function_call"), ViolationCode.NAMES_TOOL_CALL),
        (_plan(tool_calls=[{"name": "camera_cue"}]), ViolationCode.NAMES_TOOL_CALL),
        (_plan(extra=[{"Function-Call": {}}]), ViolationCode.NAMES_TOOL_CALL),
        (_plan(action="hold"), ViolationCode.NAMES_TOOL_CALL),
        (_plan(next_probe="approve"), ViolationCode.NAMES_APPROVAL),
        (_plan(next_probe="Approved"), ViolationCode.NAMES_APPROVAL),
        (_plan(token="abc"), ViolationCode.NAMES_APPROVAL),
        (_plan(next_probe="hold"), ViolationCode.NAMES_RUNG),
        (_plan(next_probe="slow"), ViolationCode.NAMES_RUNG),
        (_plan(next_probe="hand to a person"), ViolationCode.NAMES_RUNG),
        (_plan(next_probe="HOLDING"), ViolationCode.NAMES_RUNG),
        (_plan(next_probe="advisory_one_basin"), ViolationCode.NAMES_RUNG),
        (_plan(next_probe="harbor_wide_slowdown"), ViolationCode.NAMES_RUNG),
        (_plan(recommended_rung="ask"), ViolationCode.NAMES_RUNG),
        (_plan(next_probe="registry lookup"), ViolationCode.NOT_IN_ALLOWLIST),
        (_plan(next_probe="ACOUSTIC_HAIL"), ViolationCode.NOT_IN_ALLOWLIST),
        (_plan(next_probe="registry_lookup"), ViolationCode.ALREADY_RUN),
        (_plan(next_probe=7), ViolationCode.SCHEMA_INVALID),
        (_plan(probability=0.9), ViolationCode.SCHEMA_INVALID),
        (_plan(why="two\nlines"), ViolationCode.SCHEMA_INVALID),
        (_plan(why="x" * 201), ViolationCode.SCHEMA_INVALID),
        (_plan(hypotheses_to_discriminate=[]), ViolationCode.SCHEMA_INVALID),
        (_plan(hypotheses_to_discriminate=["BIOLOGIC", "BIOLOGIC"]), ViolationCode.SCHEMA_INVALID),
        (
            _plan(hypotheses_to_discriminate=["BIOLOGIC", "SUBMARINE_DRONE"]),
            ViolationCode.HYPOTHESIS_OUTSIDE_SET,
        ),
    ],
)
def test_guard_classifies(raw: dict[str, Any], code: ViolationCode) -> None:
    result = check_probe_plan(raw, make_ctx())
    assert result == GuardResult(False, None, code, result.detail)
    assert result.detail


def test_guard_rejects_non_objects() -> None:
    for raw in (None, "acoustic_hail", ["acoustic_hail"], 3):
        assert check_probe_plan(raw, make_ctx()).violation is ViolationCode.SCHEMA_INVALID


def test_registry_lookup_is_a_probe_not_a_boundary_action() -> None:
    ctx = make_ctx(already_run=frozenset())
    assert check_probe_plan(_plan(next_probe="registry_lookup"), ctx).accepted


@pytest.mark.parametrize(
    ("remaining", "probe"),
    [
        (RemainingBudget(0, 2, 1), "ask_operator_by_phone"),
        (RemainingBudget(4, 0, 1), "acoustic_hail"),
        (RemainingBudget(4, 2, 0), "request_watch_officer_look"),
    ],
)
def test_guard_budget_limits(remaining: RemainingBudget, probe: str) -> None:
    result = check_probe_plan(_plan(next_probe=probe), make_ctx(remaining=remaining))
    assert result.violation is ViolationCode.OVER_BUDGET


def test_guard_denylisted_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(denylist_mod, "DENYLIST", (re.compile(r"\bzebra\b", re.IGNORECASE),))
    result = check_probe_plan(_plan(why="A zebra crossing."), make_ctx())
    assert result.violation is ViolationCode.DENYLISTED_TEXT
    raw = {"hypotheses": [{"id": "BIOLOGIC", "why": "A zebra."}], "question_for_human": None}
    assert check_hypothesis_set(raw).violation is ViolationCode.DENYLISTED_TEXT


def test_guard_rung_check_uses_current_rung() -> None:
    assert check_probe_plan(_plan(), make_ctx(current_rung=Rung.ASK)).accepted
    watch = check_probe_plan(_plan(), make_ctx(current_rung=Rung.WATCH))
    assert watch.violation is ViolationCode.ABOVE_CURRENT_RUNG


# --- fallbacks ---------------------------------------------------------------------------


def test_rule_based_order_watch_rung() -> None:
    ctx = make_ctx(current_rung=Rung.WATCH, already_run=frozenset())
    assert rule_based_probe_order(ctx) == (
        ProbeId.REGISTRY_LOOKUP,
        ProbeId.KINEMATICS_PROFILE,
        ProbeId.DRIFT_CHECK,
        ProbeId.PATTERN_OF_LIFE_CHECK,
        ProbeId.AIS_SURFACE_CORRELATE,
        ProbeId.ACOUSTIC_FEATURE_READ,
    )


def test_rule_based_order_respects_budget_and_rung() -> None:
    ctx = make_ctx(remaining=RemainingBudget(probes=3, paid_probes=0, interruptions=0))
    assert rule_based_probe_order(ctx) == (ProbeId.ASK_OPERATOR_BY_PHONE,)
    assert rule_based_probe_order(make_ctx(remaining=RemainingBudget(0, 2, 1))) == ()
    assert (
        fallback_probe_plan(make_ctx(remaining=RemainingBudget(0, 2, 1)), threat_report()) is None
    )


def test_fallback_plans_always_pass_the_guard() -> None:
    for ctx in (make_ctx(), make_ctx(current_rung=Rung.WATCH, already_run=frozenset())):
        for report in (threat_report(), benign_report(), settled_report()):
            plan = fallback_probe_plan(ctx, report)
            assert plan is not None
            assert check_probe_plan(plan.model_dump(mode="json"), ctx).accepted


def test_template_question() -> None:
    assert template_question(threat_report()) == (
        "Is any operator running an unfiled vehicle in this basin tonight?"
    )
    assert (
        template_question(benign_report())
        == PAIR_QUESTIONS[frozenset({Hypothesis.BIOLOGIC, Hypothesis.DEBRIS_OR_DRIFT})]
    )
    assert template_question(settled_report()) is None
    assert GENERIC_QUESTION not in PAIR_QUESTIONS.values()


# --- client record / live paths ----------------------------------------------------------


def test_record_writes_fixture_then_returns_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    transport = StubTransport(_plan())
    store = FixtureStore(tmp_path)
    client = ModelClient(
        mode=ModelMode.RECORD, store=store, model_name="stub-model", transport=transport
    )
    inputs = PLANNER_CASES["valid"].inputs()
    out = client.call(ROLE_PROBE_PLANNER, inputs, system="sys")
    assert out == _plan()
    assert transport.requests == [
        {"model": "stub-model", "role": ROLE_PROBE_PLANNER, "system": "sys", "inputs": inputs}
    ]
    path = store.path(ROLE_PROBE_PLANNER, fixture_key(ROLE_PROBE_PLANNER, inputs))
    assert json.loads(path.read_text(encoding="utf-8"))["output"] == _plan()
    # a second record call replays the fixture: no second transport call
    assert client.call(ROLE_PROBE_PLANNER, inputs) == _plan()
    assert len(transport.requests) == 1
    # and replay mode now finds it
    assert (
        ModelClient(mode=ModelMode.REPLAY, store=store).call(ROLE_PROBE_PLANNER, inputs) == _plan()
    )


def test_live_calls_transport_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    transport = StubTransport({"hypotheses": [], "question_for_human": None})
    client = ModelClient(
        mode=ModelMode.LIVE, store=FixtureStore(tmp_path), model_name="m", transport=transport
    )
    assert client.call(ROLE_HYPOTHESIS_WRITER, {"x": 1}) == {
        "hypotheses": [],
        "question_for_human": None,
    }
    assert client.call(ROLE_HYPOTHESIS_WRITER, {"x": 1}) is not None
    assert len(transport.requests) == 2
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("mode", [ModelMode.RECORD, ModelMode.LIVE])
def test_record_and_live_refuse_in_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: ModelMode
) -> None:
    monkeypatch.setenv("SOUNDINGS_CI", "1")
    transport = StubTransport(_plan())
    client = ModelClient(
        mode=mode, store=FixtureStore(tmp_path), model_name="m", transport=transport
    )
    with pytest.raises(LiveCallRefused, match="SOUNDINGS_CI"):
        client.call(ROLE_PROBE_PLANNER, {"x": 1})
    assert transport.requests == []
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("mode", [ModelMode.RECORD, ModelMode.LIVE])
def test_record_and_live_need_model_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: ModelMode
) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    transport = StubTransport(_plan())
    client = ModelClient(mode=mode, store=FixtureStore(tmp_path), transport=transport)
    with pytest.raises(LiveCallRefused, match="MODEL_NAME"):
        client.call(ROLE_PROBE_PLANNER, {"x": 1})
    assert transport.requests == []


@pytest.mark.parametrize("mode", [ModelMode.RECORD, ModelMode.LIVE])
def test_record_and_live_without_transport_never_reach_a_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: ModelMode
) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    monkeypatch.setenv("MODEL_NAME", "m")
    client = ModelClient(mode=mode, store=FixtureStore(tmp_path))
    with pytest.raises(NotImplementedError):
        client.call(ROLE_PROBE_PLANNER, {"x": 1})


def test_fixture_root_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOUNDINGS_FIXTURE_ROOT", str(tmp_path))
    assert FixtureStore().root == tmp_path
    assert FixtureStore(FIXTURE_ROOT).root == FIXTURE_ROOT
    monkeypatch.setenv("SOUNDINGS_FIXTURE_ROOT", "")
    assert FixtureStore().root == FIXTURE_ROOT


def test_no_model_name_literal_in_llm_package() -> None:
    for path in LLM_DIR.rglob("*.py"):
        assert "claude-" not in path.read_text(encoding="utf-8").lower(), path


# --- part B: the same fixtures through LlmPlanner ----------------------------------------


def _same_as_deterministic(choice: PlannerChoice, det: PlannerChoice) -> bool:
    """``choice`` is ``det`` apart from the source / fallback-reason labels."""
    return choice.model_copy(update={"source": "deterministic", "fallback_reason": None}) == det


def _llm_choose(planner: LlmPlanner, name: str) -> PlannerChoice:
    case = PLANNER_CASES[name]
    return planner.choose(
        case.state(),
        TABLES,
        table=make_table(),
        report=threat_report(),
        asset_context=case.asset_context,
        when=f"t/{name}",
    )


@pytest.mark.parametrize("name", VIOLATING_PLANNER_CASES)
def test_violating_fixtures_through_llm_planner(name: str, tmp_path: Path) -> None:
    case = PLANNER_CASES[name]
    assert case.expected is not None
    log = tmp_path / "VIOLATIONS.md"
    planner = LlmPlanner(_replay_client(), violations_path=log)
    choice = _llm_choose(planner, name)
    det = DeterministicPlanner().choose(case.state(), TABLES)

    assert choice.source == "fallback"
    assert choice.fallback_reason == case.expected.value
    assert _same_as_deterministic(choice, det)
    assert planner.last_outcome is not None
    assert planner.last_outcome.violation is case.expected
    rows = _rows(log)
    assert len(rows) == 1
    cells = [c.strip() for c in rows[0].strip("|").split(" | ")]
    key = fixture_key(ROLE_PROBE_PLANNER, case.inputs())
    assert cells[:3] == [f"t/{name}", ROLE_PROBE_PLANNER, key]
    assert cells[3].startswith(case.expected.value)
    assert cells[4] == FALLBACK_DETERMINISTIC_PLANNER
    if choice.probe_id is not None:
        assert case.state().ledger.allows(CONTACT_ID, choice.probe_id)


def test_valid_fixture_accepted_through_llm_planner(tmp_path: Path) -> None:
    log = tmp_path / "VIOLATIONS.md"
    planner = LlmPlanner(_replay_client(), violations_path=log)
    choice = _llm_choose(planner, "valid")
    out = _fixture_output(ROLE_PROBE_PLANNER, PLANNER_CASES["valid"].inputs())
    assert choice.source == "llm" and choice.fallback_reason is None
    assert choice.probe_id is ProbeId(out["next_probe"])
    assert choice.why == out["why"]
    assert choice.question_for_human == out["question_for_human"]
    assert not log.exists()
    state = PLANNER_CASES["valid"].state()
    assert choice.probe_id in PROBE_ALLOWLIST
    assert state.ledger.allows(state.contact_id, choice.probe_id)
    state.ledger.record(state.contact_id, choice.probe_id)  # never raises BudgetExceeded


def test_no_fixtures_falls_back_llm_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    log = tmp_path / "v.md"
    planner = LlmPlanner(_replay_client(tmp_path / "store"), violations_path=log)
    choice = _llm_choose(planner, "valid")
    det = DeterministicPlanner().choose(PLANNER_CASES["valid"].state(), TABLES)
    assert (choice.source, choice.fallback_reason) == ("fallback", REPLAY_MISS)
    assert _same_as_deterministic(choice, det)
    assert not log.exists()
    monkeypatch.setenv("SOUNDINGS_CI", "1")
    with pytest.raises(ReplayMiss):
        _llm_choose(LlmPlanner(_replay_client(tmp_path / "store"), violations_path=log), "valid")
    assert not log.exists()


def test_never_loops_llm_planner(tmp_path: Path) -> None:
    stub = CountingClient(_plan(next_probe="hold"))
    log = tmp_path / "v.md"
    planner = LlmPlanner(stub, violations_path=log)
    assert planner.call_limit == 4
    state = PLANNER_CASES["valid"].state()
    det = DeterministicPlanner().choose(state, TABLES)
    choices = []
    for i in range(10):
        before = stub.calls
        choices.append(planner.choose(state, TABLES, when=f"t{i}"))
        assert stub.calls - before <= 1  # one call per decision at most, never a retry
    assert stub.calls == 4 == planner.calls_made(state.contact_id)
    assert [c.fallback_reason for c in choices] == ["NAMES_RUNG"] * 4 + [LLM_BUDGET_SPENT] * 6
    assert all(_same_as_deterministic(c, det) for c in choices)
    assert len(_rows(log)) == 4


def test_hypothesis_writer_one_call_per_investigation(tmp_path: Path) -> None:
    reply = _fixture_output(ROLE_HYPOTHESIS_WRITER, WRITER_CASES["valid"].inputs())
    stub = CountingClient(reply)
    writer = HypothesisWriter(stub, violations_path=tmp_path / "v.md")
    first = writer.write(threat_report(), make_table(), when="t")
    second = writer.write(threat_report(), make_table(), when="t")
    assert first.source == "llm"
    assert (second.source, second.fallback_reason) == ("fallback", LLM_BUDGET_SPENT)
    assert second.value == template_hypotheses(threat_report())
    assert stub.calls == 1


# --- part B: the real catalog with an empty fixture store --------------------------------

CONTACT_ID = PLANNER_CASES["valid"].ctx.contact_id
CATALOG = [("S05", 11), ("S07", 17)]
FIXED_NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
WATCH_PROBES = tuple(p for p, s in PROBE_ALLOWLIST.items() if s.rung_required is Rung.WATCH)


def _main_contact(run: Any) -> str:
    ids = [c for c, t in run.contact_truth.items() if t.gt_id == "gt-1"]
    return str(sorted(ids, key=lambda c: (-len(run.tracks[c].points), c))[0])


def _catalog_loop(
    scenario_id: str, seed: int, planner: Planner
) -> tuple[list[ProbeId], list[PlannerChoice], PosteriorReport, EvidenceTable, BudgetLedger]:
    """Sweep the watch probes, then let ``planner`` pick ask probes until done."""
    harbor = load_harbor()
    scenario = resolve_scenario(scenario_id, harbor)
    run = run_scenario(scenario, seed=seed, harbor=harbor)
    field = load_current_field()
    pol = load_pol_table()
    table = build_evidence(
        run,
        harbor,
        field,
        pol,
        hour_of_day=scenario.hour_of_day,
        tide_phase_for_hour=field.phase_for_hour,
    )
    prior_cfg = load_prior_config()
    store = RegistryStore(harbor=harbor, now=lambda: FIXED_NOW)
    store.seed_from_scenario(scenario, skip_existing=True)
    cid = _main_contact(run)
    tick = run.tracks[cid].points[-1].tick
    ctx = ProbeContext(
        run=run,
        scenario=scenario,
        harbor=harbor,
        current_field=field,
        pol=pol,
        registry=store,
        tick=tick,
        current_rung=Rung.ASK,
        contact_id=cid,
    )
    cache = ProbeCache()
    ledger = BudgetLedger()
    picked: list[ProbeId] = []
    choices: list[PlannerChoice] = []
    try:
        for pid in WATCH_PROBES:  # the fixed watch sweep (not planner-chosen)
            run_probe(pid, ctx, table, cache=cache)
        ran = frozenset(WATCH_PROBES)
        for _ in range(len(PROBE_ALLOWLIST)):
            state = state_from_table(
                table,
                cid,
                TABLES,
                prior_cfg,
                current_rung=Rung.ASK,
                ledger=ledger,
                tick=tick,
                tick_s=run.tick_s,
                run_keys=frozenset(cache.keys()),
                ran_probes=ran,
            )
            report = score_contact(table, cid, TABLES, prior_cfg)
            if isinstance(planner, LlmPlanner):
                choice = planner.choose(
                    state, TABLES, table=table, report=report, when=f"{scenario_id}/t={tick}"
                )
            else:
                choice = planner.choose(state, TABLES)
            choices.append(choice)
            if choice.probe_id is None:
                break
            ledger.record(cid, choice.probe_id)  # raises BudgetExceeded if over budget
            run_probe(choice.probe_id, ctx, table, cache=cache)
            picked.append(choice.probe_id)
            ran = ran | {choice.probe_id}
    finally:
        store.close()
    return picked, choices, score_contact(table, cid, TABLES, prior_cfg), table, ledger


@pytest.mark.parametrize(("scenario_id", "seed"), CATALOG)
def test_catalog_fallback(
    scenario_id: str,
    seed: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    monkeypatch.setenv("SOUNDINGS_DB", str(tmp_path / "det.db"))
    det_seq, det_choices, det_report, _, _ = _catalog_loop(
        scenario_id, seed, DeterministicPlanner()
    )
    monkeypatch.setenv("SOUNDINGS_DB", str(tmp_path / "llm.db"))
    log = tmp_path / "VIOLATIONS.md"
    empty = tmp_path / "empty-store"
    planner = LlmPlanner(_replay_client(empty), violations_path=log)
    llm_seq, llm_choices, report, table, ledger = _catalog_loop(scenario_id, seed, planner)

    assert det_seq, "the deterministic loop should pick at least one ask probe"
    assert llm_seq == det_seq
    assert len(llm_choices) == len(det_choices)
    for got, want in zip(llm_choices, det_choices, strict=True):
        assert got.source == "fallback"
        assert got.fallback_reason in (REPLAY_MISS, LLM_BUDGET_SPENT)
        assert _same_as_deterministic(got, want)
    assert report == det_report
    counts, limits = ledger.counts(report.contact_id), ledger.limits
    assert counts.probes <= limits.probes_per_contact
    assert counts.paid <= limits.paid_probes_per_contact
    assert counts.interruptions <= limits.human_interruptions_per_contact
    assert planner.calls_made(report.contact_id) <= planner.call_limit
    assert not log.exists()
    assert not empty.exists() or not list(empty.rglob("*.json"))

    outcome = HypothesisWriter(_replay_client(empty), violations_path=log).write(
        report, table, when=f"{scenario_id}/final"
    )
    assert (outcome.source, outcome.fallback_reason) == ("fallback", REPLAY_MISS)
    assert outcome.value == template_hypotheses(report)
    assert {e.id for e in outcome.value.hypotheses} <= EIGHT
    assert not log.exists()
