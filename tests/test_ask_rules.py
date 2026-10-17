"""WO-022 rule proofs: CLAUDE.md 2.2 (no model output or hidden state as control),
2.6 (evidence first; facts and assumptions apart), 2.1 and 2.5 (vocabulary; nothing past ask).
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import soundings.runner as runner_mod
from soundings.matcher import MatchCode, MatchOutcome
from soundings.runner import (
    ASK_TRANSITIONS,
    IllegalTransition,
    RunnerEvent,
    ScenarioRunner,
    _Case,
    _V1Run,
    check_transition,
)
from soundings.schemas import EvidenceKind, FeatureName, LadderState, find_denylisted
from soundings.schemas._base import REPO_ROOT
from tests.runner_runs import CATALOG, run

MATCHER = REPO_ROOT / "soundings" / "matcher.py"
RUNNER = REPO_ROOT / "soundings" / "runner.py"
S = LadderState


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


# --- 2.2 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [MATCHER, RUNNER], ids=["matcher", "runner"])
def test_no_llm_imports(path: Path) -> None:
    assert not any(n == "soundings.llm" or n.startswith("soundings.llm.") for n in _imports(path))


def test_matcher_imports_no_io_or_clock() -> None:
    names = _imports(MATCHER)
    for banned in ("os", "sqlite3", "sqlmodel", "time", "socket", "datetime"):
        assert not any(n == banned or n.startswith(f"{banned}.") for n in names), banned
    tree = ast.parse(MATCHER.read_text(encoding="utf-8"))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "now" not in attrs and "time" not in attrs


def test_every_state_assignment_goes_through_the_check() -> None:
    """``case.state = ...`` appears only in ``advance`` (after the check) or as FAILED."""
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "state":
                    value = ast.unparse(node.value)
                    if func.name == "advance" and value == "to_state":
                        body = ast.unparse(func)
                        assert body.index("check_transition") < body.index("case.state = to_state")
                    else:
                        assert value == "S.FAILED", (func.name, value)


def test_ask_transitions_are_the_five_spec_rows() -> None:
    assert len(ASK_TRANSITIONS) == 5
    assert {t[2] for t in ASK_TRANSITIONS} == {
        S.WATCHING,
        S.ASKING,
        S.CLEARED_AT_ASK,
        S.LOST_VEHICLE,
        S.INVESTIGATING,
    }
    assert {e.value for e in RunnerEvent} == {"contact_detected", "probe_result", "stop_rule_fired"}


@pytest.mark.parametrize(
    ("src", "event", "dst"),
    [
        (S.ASKING, RunnerEvent.STOP_RULE_FIRED, S.HOLDING),
        (S.ASKING, RunnerEvent.STOP_RULE_FIRED, S.HANDED_OFF),
        (S.WATCHING, RunnerEvent.STOP_RULE_FIRED, S.CLEARED_AT_ASK),
        (S.DETECTED, RunnerEvent.PROBE_RESULT, S.ASKING),
        (S.CLEARED_AT_ASK, RunnerEvent.CONTACT_DETECTED, S.WATCHING),
        (S.INVESTIGATING, RunnerEvent.PROBE_RESULT, S.ASKING),
    ],
)
def test_forged_transition_raises(src: LadderState, event: RunnerEvent, dst: LadderState) -> None:
    with pytest.raises(IllegalTransition):
        check_transition(src, event, dst)


def test_forged_transition_writes_a_failed_case() -> None:
    v1 = _V1Run(ScenarioRunner("S01"))
    case = _Case(case_id="c-0001#1", contact_id="c-0001", opened_tick=0, state=S.ASKING)
    with pytest.raises(IllegalTransition):
        v1.advance(
            case,
            5,
            RunnerEvent.PROBE_RESULT,
            S.CLEARED_AT_ASK,
            reason_code="forged",
            row_ids=["0000000000000001"],
            key="0" * 64,
        )
    assert case.state is S.FAILED and case.failure
    assert v1.transitions == []


def test_clearance_without_exact_registry_fails_the_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """A matcher that claimed CLEARED_AT_ASK without registry exact + hail valid is refused."""
    real = runner_mod.match

    def lying(*args: object, **kwargs: object) -> MatchOutcome:
        out = real(*args, **kwargs)  # type: ignore[arg-type]
        return out.model_copy(update={"code": MatchCode.CLEARED_AT_ASK, "plan_id": "P-X"})

    monkeypatch.setattr(runner_mod, "match", lying)
    result = ScenarioRunner("S05").run()
    assert result.cases
    assert all(c.state is not S.CLEARED_AT_ASK for c in result.cases)
    failed = [c for c in result.cases if c.state is S.FAILED]
    assert failed and all(c.failure for c in failed)


# --- 2.6 ---------------------------------------------------------------------------------------


def _sweep() -> list[tuple[str, int | None]]:
    return [(s, None) for s in CATALOG] + [("S02", 1), ("S02", 2), ("S02", 4)]


def test_every_outcome_and_notice_line_cites_existing_rows() -> None:
    for scenario_id, seed in _sweep():
        result = run(scenario_id, seed)
        ev = result.evidence
        for case in result.cases:
            assert case.outcomes, case.case_id
            for outcome in case.outcomes:
                assert outcome.evidence_row_ids
                assert all(i in ev for i in outcome.evidence_row_ids), case.case_id
        for t in result.transitions:
            assert t.evidence_row_ids and all(i in ev for i in t.evidence_row_ids)
        for notice in result.notices:
            assert all(i in ev for i in notice.evidence_row_ids)
            for line in notice.lines:
                assert line.evidence_row_ids and all(i in ev for i in line.evidence_row_ids)


def test_facts_and_assumptions_stay_apart_in_notices() -> None:
    seen = 0
    for scenario_id, seed in _sweep():
        result = run(scenario_id, seed)
        ev = result.evidence
        for notice in result.notices:
            seen += 1
            box_row = ev.get(notice.search_box.assumption_row_id)
            assert box_row.kind is EvidenceKind.ASSUMPTION
            assert box_row.feature is FeatureName.CURRENT_FIELD and box_row.assumes
            assume = [ln for ln in notice.lines if ln.section == "What we assume"]
            assert any(notice.search_box.assumption_row_id in ln.evidence_row_ids for ln in assume)
            for ln in notice.lines:
                if ln.section == "What we know":
                    kinds = {ev.get(i).kind for i in ln.evidence_row_ids}
                    assert EvidenceKind.ASSUMPTION not in kinds, ln.text
            box_lines = [ln for ln in notice.lines if ln.section == "Search box"]
            assert (
                box_lines and notice.search_box.assumption_row_id in box_lines[0].evidence_row_ids
            )
    assert seen >= 3


# --- 2.1 and 2.5 -------------------------------------------------------------------------------

WO_FILES = (
    "soundings/matcher.py",
    "soundings/runner.py",
    "config/matcher.yaml",
    "tests/test_matcher.py",
    "tests/test_ask_rung.py",
    "tests/test_runner_v1.py",
    "tests/test_ask_rules.py",
    "tests/test_search_box.py",
    "tests/test_reacquire.py",
)


def test_vocabulary_is_clean() -> None:
    for code in MatchCode:
        assert not find_denylisted(code.value)
    for event in RunnerEvent:
        assert not find_denylisted(event.value)
    assert not find_denylisted((REPO_ROOT / "config" / "matcher.yaml").read_text("utf-8"))
    for scenario_id, seed in _sweep():
        result = run(scenario_id, seed)
        for notice in result.notices:
            for line in notice.lines:
                assert not find_denylisted(line.text)
        for case in result.cases:
            for outcome in case.outcomes:
                assert not find_denylisted(outcome.reason)


def test_denylist_grep_prints_nothing() -> None:
    proc = subprocess.run(
        ["grep", "-riEf", "config/denylist.txt", *WO_FILES],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.stdout == ""
    assert proc.returncode == 1  # 1 = no match (2 would be an error)


def test_no_state_past_ask_in_this_code() -> None:
    """Runner v1 (its WO-022 block) and the matcher name no state beyond the ask rung."""
    source = RUNNER.read_text(encoding="utf-8")
    block = source[source.index("# --- WO-022 begin") : source.index("# --- WO-022 end ---")]
    text = block + MATCHER.read_text(encoding="utf-8")
    for later in ("ADVISING_SLOW", "HOLDING", "AWAITING_APPROVAL", "HANDED_OFF", "WATCH_AFTER"):
        assert later not in text, later
