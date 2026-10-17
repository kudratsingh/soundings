"""Stub transports and two drivers for the WO-087 live-mode tests. Nothing here reaches a network.

* ``ScriptedTransport`` answers like a hosted model from a callable; ``SleepyTransport``
  never answers inside the timeout; ``FailingTransport`` raises.
* ``investigate(sid, client)`` runs the WO-033 ``InvestigationMachine`` over a catalog
  scenario with ``LlmMachinePlanner``: WO-032's ``LlmPlanner`` (guard, fallback, the
  deterministic WO-031 choice) adapted to the machine's ``Planner`` protocol, with a probe
  runner that adds no rows, so every decision after the free watch evidence goes through
  the planner until the budget stops the investigation.
* ``handoff_s05(workdir, client)`` is the WO-042 S05 handoff with an injected client, so a
  live client's SITREP path lands in the audit log.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import build_evidence
from soundings.investigation.machine import (
    BudgetView,
    InvestigationLedger,
    InvestigationMachine,
    InvestigationResult,
    NoRowsProbeRunner,
    ProbeChoice,
    load_budgets,
    load_stopping_config,
)
from soundings.investigation.planner import (
    BudgetLedger,
    DeterministicPlanner,
    PlannerChoice,
    PlannerState,
    state_from_table,
)
from soundings.investigation.pol import load_pol_table
from soundings.investigation.posterior import (
    PosteriorReport,
    load_likelihood_tables,
    load_prior_config,
)
from soundings.investigation.probes import ALLOWLIST
from soundings.ladder.audit import AuditLog
from soundings.ladder.driver import LadderDriver, LadderRun
from soundings.ladder.handoff import HandoffService
from soundings.ladder.synthetic import S05_CONTACT, S05_HOUR, S05_TICK_S, s05_feed
from soundings.ladder.watch import DepartureDetector
from soundings.llm.client import ModelClient, ModelOutput, TransportReply
from soundings.llm.guard import ProbePlanOutput
from soundings.llm.planner import LlmPlanner
from soundings.llm.roles import RoleOutcome
from soundings.schemas.enums import ProbeId, Rung
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.sim.current import load_current_field, tide_phase_for_hour
from soundings.sim.run import run_scenario
from soundings.sim.scenario import resolve_scenario
from soundings.sim.types import SimRun
from tests.support.guard_streams import signed_orders_copy

TABLES = load_likelihood_tables()
PRIORS = load_prior_config()
STUB_MODEL = "stub-model"


# --- transports ---------------------------------------------------------------------------


@dataclass
class ScriptedTransport:
    """Answers with ``reply(role, inputs)``; records every request."""

    reply: Callable[[str, Any], ModelOutput | TransportReply]
    requests: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self, *, model: str, role: str, system: str | None, inputs: Any
    ) -> ModelOutput | TransportReply:
        self.requests.append({"model": model, "role": role, "inputs": inputs})
        return self.reply(role, inputs)


@dataclass
class SleepyTransport:
    """Blocks for ``seconds`` (released early by ``release``), then answers ``{}``."""

    seconds: float
    requests: int = 0
    gate: threading.Event = field(default_factory=threading.Event)

    def __call__(self, *, model: str, role: str, system: str | None, inputs: Any) -> ModelOutput:
        self.requests += 1
        self.gate.wait(self.seconds)
        return {}

    def release(self) -> None:
        self.gate.set()


@dataclass
class FailingTransport:
    """Raises ``error`` on every call."""

    error: BaseException
    requests: int = 0

    def __call__(self, *, model: str, role: str, system: str | None, inputs: Any) -> ModelOutput:
        self.requests += 1
        raise self.error


@dataclass
class ExplodingTransport:
    """Fails the test if anything calls it."""

    def __call__(self, *, model: str, role: str, system: str | None, inputs: Any) -> ModelOutput:
        raise AssertionError("the transport must not be called")


# --- the investigation driver ------------------------------------------------------------


class LlmMachinePlanner:
    """WO-032 ``LlmPlanner`` behind the WO-033 machine's ``Planner`` protocol."""

    def __init__(self, llm: LlmPlanner, contact_id: str, tick_s: float) -> None:
        self.llm = llm
        self.contact_id = contact_id
        self.tick_s = tick_s
        self.ledger = BudgetLedger()
        self.ran: list[ProbeId] = []
        self.choices: list[PlannerChoice] = []
        self.outcomes: list[RoleOutcome[ProbePlanOutput | None]] = []
        self.state: PlannerState | None = None

    def next_probe(
        self, table: EvidenceTable, posterior: PosteriorReport, budget: BudgetView, rung: Rung
    ) -> ProbeChoice | None:
        self.state = state_from_table(
            table,
            self.contact_id,
            TABLES,
            PRIORS,
            current_rung=rung,
            ledger=self.ledger.copy(),
            tick=budget.tick,
            tick_s=self.tick_s,
            ran_probes=frozenset(self.ran),
        )
        choice = self.llm.choose(
            self.state, TABLES, table=table, report=posterior, when=f"t={budget.tick}"
        )
        self.choices.append(choice)
        assert self.llm.last_outcome is not None
        self.outcomes.append(self.llm.last_outcome)
        if choice.probe_id is None:
            return None
        spec = ALLOWLIST[choice.probe_id]
        self.ledger.record(self.contact_id, choice.probe_id)
        self.ran.append(choice.probe_id)
        return ProbeChoice(
            probe_id=choice.probe_id,
            cost_usd=spec.cost_usd,
            latency_s=spec.latency_s,
            interruption=spec.interruption,
            rung_required=spec.rung_required,
            source=choice.source,
        )

    def deterministic_reply(self, role: str, inputs: Any) -> ModelOutput:
        """A well-formed planner reply naming the probe the deterministic rules pick."""
        assert self.state is not None
        pick = DeterministicPlanner().choose(self.state, TABLES).probe_id
        assert pick is not None
        return {
            "next_probe": pick.value,
            "why": "Separates the two leading hypotheses at the lowest cost.",
            "hypotheses_to_discriminate": ["UNAUTHORIZED_UUV", "FILED_OFF_PLAN"],
            "question_for_human": None,
        }


@cache
def _harbor() -> Harbor:
    return load_harbor()


@cache
def _sim(sid: str, seed: int | None) -> SimRun:
    scenario = resolve_scenario(sid, _harbor())
    return run_scenario(scenario, seed=scenario.seed if seed is None else seed, harbor=_harbor())


def _evidence(sid: str, run: SimRun) -> EvidenceTable:
    current = load_current_field()
    return build_evidence(
        run,
        _harbor(),
        current,
        load_pol_table(),
        hour_of_day=resolve_scenario(sid, _harbor()).hour_of_day,
        tide_phase_for_hour=lambda hour: tide_phase_for_hour(hour, current),
    )


def scenario_contact(run: SimRun) -> str:
    """The longest-tracked ground-truth contact (sim false alarms excluded)."""
    ids = [cid for cid, t in run.contact_truth.items() if not t.gt_id.startswith("fa-")]
    return sorted(ids, key=lambda cid: (-len(run.tracks[cid].points), cid))[0]


@dataclass
class Investigation:
    result: InvestigationResult
    planner: LlmMachinePlanner


def investigate(
    sid: str,
    client: ModelClient,
    violations_path: Path,
    *,
    seed: int | None = None,
    transport_slot: list[LlmMachinePlanner] | None = None,
) -> Investigation:
    """One investigation of ``sid`` with ``client`` behind the guarded planner.

    The investigation starts at the first tick of the contact's track (so the stop rule
    has not fired yet) and may run to the scenario's last tick. ``transport_slot``
    receives the adapter before the run, so a stub transport can answer from its state.
    """
    run = _sim(sid, seed)
    cid = scenario_contact(run)
    llm = LlmPlanner(client, violations_path=violations_path, call_limit=8)
    adapter = LlmMachinePlanner(llm, cid, run.tick_s)
    if transport_slot is not None:
        transport_slot.append(adapter)
    machine = InvestigationMachine(
        cid,
        _evidence(sid, run),
        planner=adapter,
        probe_runner=NoRowsProbeRunner(),
        tables=TABLES,
        prior_cfg=PRIORS,
        stopping=load_stopping_config(),
        budgets=load_budgets(),
        tick=run.tracks[cid].points[0].tick,
        tick_s=run.tick_s,
        max_tick=run.n_ticks - 1,
        ledger=InvestigationLedger(),
    )
    return Investigation(machine.run(), adapter)


# --- the S05 handoff with an injected client ---------------------------------------------


@dataclass
class LiveHandoff:
    run: LadderRun
    service: HandoffService
    audit: AuditLog


def handoff_s05(workdir: Path, client: ModelClient, *, cid: str = S05_CONTACT) -> LiveHandoff:
    """The WO-042 S05 handoff (``writer_mode="auto"``) with ``client`` as the writer's client."""
    workdir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(workdir / "audit.db")
    service = HandoffService(
        client,
        audit,
        run_id="s05-seed11-live",
        violations_path=workdir / "VIOLATIONS.md",
        writer_mode="auto",
    )
    driver = LadderDriver.from_config(
        tick_s=S05_TICK_S,
        hour_of_day=S05_HOUR,
        orders_path=signed_orders_copy(workdir),
        departure=DepartureDetector.from_config(load_harbor(), S05_TICK_S),
        listeners=(service,),
        sitrep_writer=service,
    )
    if service.sink is not None:
        service.sink.clock = driver.clock
    runs = driver.run([s05_feed(cid=cid)])
    return LiveHandoff(runs[cid], service, audit)
