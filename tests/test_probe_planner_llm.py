"""WO-032: the LLM clause of the BUILD-PLAN planner line (SPEC-INVESTIGATION section 6).

``tests/test_probe_planner.py`` (WO-031) carries the deterministic and budget clauses.
Here: a model reply outside the allowlist is rejected and the deterministic choice is
used (``outside_allowlist_falls_back``), and ``LlmPlanner`` fed any guard-accepted reply
never exceeds a budget (``llm_budget_property``). Every model reply is a hand-written
fixture or a stub; nothing reaches a network (CLAUDE.md 2.3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import soundings.llm.roles as roles_mod
from soundings.investigation.planner import (
    BudgetLedger,
    DeterministicPlanner,
    PlannerChoice,
    PlannerState,
    load_budget_limits,
)
from soundings.investigation.probes import ALLOWLIST, rung_index
from soundings.llm.client import FixtureStore, ModelClient, ModelMode, ModelOutput, fixture_key
from soundings.llm.guard import (
    GuardResult,
    ProbePlanOutput,
    RemainingBudget,
    ViolationCode,
    check_probe_plan,
)
from soundings.llm.planner import (
    LLM_ROLES_DEFAULT,
    POST_GUARD_RECHECK,
    REASON_LLM_PROPOSAL,
    LlmPlanner,
    guard_context,
    make_planner,
)
from soundings.llm.roles import FALLBACK_DETERMINISTIC_PLANNER, ROLE_PROBE_PLANNER
from soundings.schemas.enums import Hypothesis, ProbeId, Rung
from tests.llm_cases import (
    CONTACT,
    FIXTURE_ROOT,
    PLANNER_CASES,
    TABLES,
    make_ctx,
    make_table,
    planner_state,
    threat_report,
)

LIMITS = load_budget_limits()


class StubClient(ModelClient):
    """Replies from a fixed list (the last one repeats); counts calls; no store, no network."""

    def __init__(self, replies: list[ModelOutput | None]) -> None:
        super().__init__(mode=ModelMode.REPLAY, store=FixtureStore(Path("/nonexistent")))
        self.replies = replies
        self.calls = 0

    def call(self, role: str, inputs: Any, *, system: str | None = None) -> ModelOutput | None:
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply


def _plan(next_probe: str) -> dict[str, Any]:
    return {
        "next_probe": next_probe,
        "why": "This would separate the top two.",
        "hypotheses_to_discriminate": ["UNAUTHORIZED_UUV", "FILED_OFF_PLAN"],
        "question_for_human": None,
    }


def _labels_stripped(choice: PlannerChoice) -> PlannerChoice:
    return choice.model_copy(update={"source": "deterministic", "fallback_reason": None})


def _log_rows(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[lines.index("|---|---|---|---|---|") + 1 :]


# --- outside the allowlist ---------------------------------------------------------------


def test_outside_allowlist_falls_back(tmp_path: Path) -> None:
    case = PLANNER_CASES["free_text"]
    log = tmp_path / "VIOLATIONS.md"
    planner = LlmPlanner(
        ModelClient(mode=ModelMode.REPLAY, store=FixtureStore(FIXTURE_ROOT)), violations_path=log
    )
    state = case.state()
    choice = planner.choose(
        state,
        TABLES,
        table=make_table(),
        report=threat_report(),
        asset_context=case.asset_context,
        when="t/free_text",
    )
    det = DeterministicPlanner().choose(state, TABLES)
    assert choice.source == "fallback"
    assert choice.fallback_reason == ViolationCode.NOT_IN_ALLOWLIST.value
    assert _labels_stripped(choice) == det
    assert choice.probe_id == det.probe_id and det.probe_id is not None
    rows = _log_rows(log)
    assert len(rows) == 1
    key = fixture_key(ROLE_PROBE_PLANNER, case.inputs())
    assert f"| {key} | NOT_IN_ALLOWLIST" in rows[0]
    assert rows[0].endswith(f"| {FALLBACK_DETERMINISTIC_PLANNER} |")


@pytest.mark.parametrize(
    "value",
    [
        "ACOUSTIC_HAIL",
        "registry lookup",
        "sonar_ping",
        "listen longer",
        "acoustic_hail, then camera_cue",
        "",
    ],
)
def test_outside_allowlist_falls_back_stub_values(value: str, tmp_path: Path) -> None:
    log = tmp_path / "v.md"
    state = PLANNER_CASES["valid"].state()
    choice = LlmPlanner(StubClient([_plan(value)]), violations_path=log).choose(state, TABLES)
    assert choice.source == "fallback"
    assert choice.fallback_reason in (
        ViolationCode.NOT_IN_ALLOWLIST.value,
        ViolationCode.SCHEMA_INVALID.value,
    )
    assert _labels_stripped(choice) == DeterministicPlanner().choose(state, TABLES)
    assert len(_log_rows(log)) == 1


def test_accepted_reply_keeps_deterministic_ranking(tmp_path: Path) -> None:
    state = PLANNER_CASES["valid"].state()
    det = DeterministicPlanner().choose(state, TABLES)
    pick = next(r.probe_id for r in det.ranked if r.probe_id is not det.probe_id)
    choice = LlmPlanner(StubClient([_plan(pick.value)]), violations_path=tmp_path / "v.md").choose(
        state, TABLES
    )
    assert choice.source == "llm" and choice.reason == REASON_LLM_PROPOSAL
    assert choice.probe_id is pick
    assert choice.ranked == det.ranked
    ranked = {r.probe_id: r for r in det.ranked}[pick]
    assert (choice.gain, choice.score) == (ranked.gain, ranked.score)
    assert choice.why == "This would separate the top two."
    assert not (tmp_path / "v.md").exists()


def test_post_guard_recheck_catches_a_permissive_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a guard that accepts everything cannot push a probe past the ledger or rung."""

    def accept_all(raw: Any, ctx: Any) -> GuardResult:
        return GuardResult.accept(ProbePlanOutput.model_validate(raw))

    monkeypatch.setattr(roles_mod, "check_probe_plan", accept_all)
    state = planner_state(make_ctx(current_rung=Rung.WATCH))
    choice = LlmPlanner(
        StubClient([_plan("acoustic_hail")]), violations_path=tmp_path / "v.md"
    ).choose(state, TABLES)
    assert choice.source == "fallback" and choice.fallback_reason == POST_GUARD_RECHECK
    assert _labels_stripped(choice) == DeterministicPlanner().choose(state, TABLES)


def test_make_planner_defaults_to_deterministic(tmp_path: Path) -> None:
    assert LLM_ROLES_DEFAULT is False
    assert type(make_planner()) is DeterministicPlanner
    planner = make_planner(True, fixture_store=FixtureStore(tmp_path))
    assert isinstance(planner, LlmPlanner)
    client = planner.role.client
    assert client.mode is ModelMode.REPLAY and client.transport is None
    assert client.store.root == tmp_path


# --- the budget property -----------------------------------------------------------------

PROBE_VALUES = [p.value for p in ProbeId]
REPLY = st.builds(
    lambda probe, hyps, q: {
        "next_probe": probe,
        "why": "Proposed by the stub.",
        "hypotheses_to_discriminate": hyps,
        "question_for_human": q,
    },
    st.sampled_from(PROBE_VALUES),
    st.lists(st.sampled_from([h.value for h in Hypothesis]), min_size=1, max_size=3, unique=True),
    st.sampled_from([None, "Is anyone working near this position?"]),
)


@st.composite
def _states(draw: st.DrawFn) -> PlannerState:
    rung = draw(st.sampled_from([Rung.WATCH, Rung.ASK]))
    remaining = RemainingBudget(
        probes=draw(st.integers(0, LIMITS.probes_per_contact)),
        paid_probes=draw(st.integers(0, LIMITS.paid_probes_per_contact)),
        interruptions=draw(st.integers(0, LIMITS.human_interruptions_per_contact)),
    )
    ran = frozenset(draw(st.sets(st.sampled_from(list(ProbeId)))))
    return planner_state(make_ctx(current_rung=rung, remaining=remaining, already_run=ran))


def _within_limits(ledger: BudgetLedger) -> bool:
    c = ledger.counts(CONTACT)
    return (
        c.probes <= LIMITS.probes_per_contact
        and c.paid <= LIMITS.paid_probes_per_contact
        and c.interruptions <= LIMITS.human_interruptions_per_contact
    )


@settings(derandomize=True, max_examples=150, deadline=None)
@given(state=_states(), replies=st.lists(REPLY, min_size=1, max_size=6))
def test_llm_budget_property(
    state: PlannerState, replies: list[ModelOutput], tmp_path_factory: Any
) -> None:
    """Loop choose -> record on one contact: any guard-accepted reply stays in budget."""
    log = tmp_path_factory.mktemp("llm_budget") / "v.md"
    planner = LlmPlanner(StubClient(list(replies)), violations_path=log)
    ledger = state.ledger.copy()
    ran = set(state.ran_probes)
    for _ in range(8):
        current = PlannerState(
            contact_id=state.contact_id,
            posterior=state.posterior,
            observed_confidence=state.observed_confidence,
            current_rung=state.current_rung,
            ledger=ledger,
            tick=state.tick,
            ran_probes=frozenset(ran),
        )
        raw = replies[min(planner.calls_made(CONTACT), len(replies) - 1)]
        verdict = check_probe_plan(raw, guard_context(current))
        choice = planner.choose(current, TABLES)
        if choice.source == "llm":
            assert verdict.accepted
            assert choice.probe_id is ProbeId(raw["next_probe"])
        if choice.probe_id is None:
            break
        spec = ALLOWLIST[choice.probe_id]
        assert rung_index(spec.rung_required) <= rung_index(current.current_rung)
        assert choice.probe_id not in ran
        assert ledger.allows(CONTACT, choice.probe_id)
        ledger.record(CONTACT, choice.probe_id)  # raises BudgetExceeded if over budget
        assert _within_limits(ledger)
        ran.add(choice.probe_id)
    assert planner.calls_made(CONTACT) <= planner.call_limit
    assert _within_limits(ledger)
