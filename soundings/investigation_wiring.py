"""WO-033 part B: WO-031 and WO-032 collaborators behind the part A machine Protocols.

``InvestigationMachine`` (part A) takes three injected collaborators. This module adapts
the real ones to them:

* ``PlannerAdapter`` -> ``Planner``: wraps WO-031 ``DeterministicPlanner`` or WO-032
  ``LlmPlanner`` (both ``choose(state, tables)``). It builds the WO-031 ``PlannerState``
  from the machine's posterior and ``BudgetView``, and returns a ``ProbeChoice`` whose
  cost, latency, interruption and rung are the ``ALLOWLIST`` values (never the model's)
  and whose ``args`` are ``resolve_args``, so the machine's probe key equals the key
  ``run_probe`` uses. The ``LlmPlanner`` already falls back to the deterministic choice
  on a rejection, a replay miss or a spent call budget; every ``PlannerChoice`` is kept
  in ``choices`` for the run result.
* ``RunProbeRunner`` -> ``ProbeRunner``: WO-031 ``run_probe`` with one shared
  ``ProbeCache`` (idempotency) over a ``ProbeWorld`` (the sim run, scenario, harbor,
  current field, pattern-of-life table, plans, hail simulator). Executor failures become
  ``ProbeError`` (recorded as ``error``), never a guess.
* ``WriterAdapter`` -> ``QuestionWriter``: WO-032 ``HypothesisWriter`` when the model
  roles are on, else WO-032 ``template_question``. Text only; the replay-miss fallback
  of the writer is that same template, so a zero-fixture run with the roles on produces
  the same recommendation (and hash) as the default run.

``InvestigationHandle`` is the per-contact handle WO-043 (ladder driver) and WO-085
(triage queue) drive tick by tick: ``start(tick)``, ``step(tick)``, ``recommendation``.
``InvestigationKit`` holds the shared configuration and builds handles.

Nothing here is a control: the planner names a typed ``ProbeId`` from the allowlist or
nothing, and the writer's text never feeds a field of the ``Recommendation`` other than
``question_for_human`` (CLAUDE.md 2.1, 2.2). Deterministic: no clock, no randomness.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from soundings.challenge.hail import HailSimulator, KnownOperatorTable
from soundings.investigation.evidence import EvidenceError, EvidenceTable
from soundings.investigation.features import PolBaseline
from soundings.investigation.machine import (
    BudgetLimits,
    BudgetView,
    InvestigationCheckpoint,
    InvestigationLedger,
    InvestigationMachine,
    InvestigationResult,
    InvestigationState,
    ProbeChoice,
    ProbeError,
    Recommendation,
    StopDecision,
    StopOutcome,
    StoppingConfig,
    TemplateQuestionWriter,
)
from soundings.investigation.planner import (
    BudgetCounts,
    BudgetLedger,
    DeterministicPlanner,
    PlannerChoice,
    PlannerState,
    observed_confidence,
)
from soundings.investigation.planner import BudgetLimits as PlannerBudgetLimits
from soundings.investigation.planner import Planner as ChoosePlanner
from soundings.investigation.posterior import LikelihoodTables, PosteriorReport, PriorConfig
from soundings.investigation.probes import (
    ALLOWLIST,
    RESPONDER_PROBES,
    ProbeCache,
    ProbeContext,
    ProbeNotAllowed,
    ResponderPort,
    catch_up,
    probe_latency_ticks,
    resolve_args,
    run_probe,
    state_name,
    track_upto,
)
from soundings.ladder.guards import ResponderDispatchInputs, responder_dispatch_permitted
from soundings.llm.client import FixtureStore, ModelClient, ModelMode
from soundings.llm.fallback import template_question
from soundings.llm.guard import DEFAULT_VIOLATIONS_PATH
from soundings.llm.planner import LLM_ROLES_DEFAULT, LlmPlanner, make_planner
from soundings.llm.roles import HypothesisWriter
from soundings.matcher import MatchConfig, SearchBox
from soundings.registry.models import PlanView
from soundings.schemas.enums import ProbeId, Rung
from soundings.schemas.evidence import EvidenceRow
from soundings.schemas.harbor import Harbor
from soundings.schemas.scenario import Scenario
from soundings.sim.types import SimRun

# --- the probe world and runner ----------------------------------------------------------


@dataclass(frozen=True)
class ProbeWorld:
    """Everything a probe may read, minus the tick, rung and contact (``ProbeContext``)."""

    run: SimRun
    scenario: Scenario
    harbor: Harbor
    current_field: Any
    pol: PolBaseline | None
    plans_at: Callable[[int], Sequence[PlanView]]
    hail: HailSimulator | None = None
    match_cfg: MatchConfig | None = None
    known: KnownOperatorTable | None = None
    # --- WO-088 begin ---
    responder: ResponderPort | None = None
    search_box_for: Callable[[str, int], SearchBox | None] | None = None
    # --- WO-088 end ---

    def context(self, contact_id: str, tick: int, rung: Rung) -> ProbeContext:
        return ProbeContext(
            run=self.run,
            scenario=self.scenario,
            harbor=self.harbor,
            current_field=self.current_field,
            pol=self.pol,
            registry=list(self.plans_at(tick)),
            tick=tick,
            current_rung=rung,
            contact_id=contact_id,
            hail=self.hail,
            match_cfg=self.match_cfg,
            known=self.known,
            responder=self.responder,
            search_box=None
            if self.search_box_for is None
            else self.search_box_for(contact_id, tick),
        )


# --- WO-088 begin ---
def responder_dispatch_inputs(
    world: ProbeWorld, contact_id: str, table: EvidenceTable, tick: int
) -> ResponderDispatchInputs | None:
    """The responder guard's inputs for a look at ``contact_id`` now (None: no responder)."""
    port = world.responder
    if port is None:
        return None
    snap = catch_up(port, tick)  # time passes for the responder too (return to dock)
    track = track_upto(world.run, contact_id, tick)
    inside: bool | None = None
    if track is not None:
        last = track.points[-1]
        fence = getattr(port, "inside_geofence", None)
        inside = bool(fence(last.lat, last.lon)) if callable(fence) else None
    standoff = getattr(snap, "standoff_m", None)
    busy = state_name(snap) != "DOCKED"
    return ResponderDispatchInputs(
        contact_id=contact_id,
        station_inside_geofence=inside,
        standoff_m=None if standoff is None else float(standoff),
        other_vehicles_dispatched=1 if busy else 0,
        dispatches_for_contact=responder_dispatches(table, contact_id),
    )


def _landing_latency_s(
    probe_id: ProbeId, ctx: ProbeContext, table: EvidenceTable, default: float
) -> float:
    """When the probe lands: a responder's ``eta_ticks`` (the key's), else the allowlist."""
    if probe_id not in RESPONDER_PROBES:
        return default
    args = resolve_args(probe_id, ctx, table)
    return float(probe_latency_ticks(probe_id, args, ctx.tick_s)) * ctx.tick_s


def responder_dispatches(table: EvidenceTable, contact_id: str) -> int:
    """Responder dispatches already made for this contact (1 once any row landed)."""
    ran = {r.probe_id for r in table.for_contact(contact_id) if r.probe_id in RESPONDER_PROBES}
    return len(ran)


def responder_available(
    world: ProbeWorld, contact_id: str, table: EvidenceTable, tick: int
) -> bool:
    """True when the boundary guard lets the platform dispatch to look alone, now."""
    inputs = responder_dispatch_inputs(world, contact_id, table, tick)
    return inputs is not None and responder_dispatch_permitted(inputs).permits


# --- WO-088 end ---


class RunProbeRunner:
    """``ProbeRunner`` over WO-031 ``run_probe`` (one shared ``ProbeCache``).

    The machine passes the tick the probe lands at; the probe is sent
    ``ceil(latency_s / tick_s)`` ticks earlier, which is the ``ProbeContext`` tick.
    ``run_probe`` appends its rows to ``table`` itself; the rows are returned so the
    machine's checkpoint carries them.
    """

    def __init__(
        self,
        world: ProbeWorld,
        table: EvidenceTable,
        *,
        cache: ProbeCache | None = None,
        rung: Rung = Rung.ASK,
    ) -> None:
        self.world = world
        self.table = table
        self.cache = cache if cache is not None else ProbeCache()
        self.rung = rung

    def run(
        self, probe_id: ProbeId, contact_id: str, tick: int, args: Mapping[str, Any]
    ) -> Sequence[EvidenceRow]:
        sent = tick - probe_latency_ticks(probe_id, args, float(self.world.run.tick_s))  # WO-088
        ctx = self.world.context(contact_id, max(0, sent), self.rung)
        try:
            result = run_probe(probe_id, ctx, self.table, args=dict(args), cache=self.cache)
        except (ProbeNotAllowed, EvidenceError, RuntimeError) as exc:
            raise ProbeError(f"{probe_id.value}: {type(exc).__name__}") from exc
        return result.rows


# --- the planner -------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedChoice:
    """One planner decision, for ``RunResult.planner_choices``."""

    contact_id: str
    tick: int
    choice: PlannerChoice


def planner_ledger(
    contact_id: str, ledger: InvestigationLedger, limits: BudgetLimits
) -> BudgetLedger:
    """A WO-031 ``BudgetLedger`` holding the machine ledger's counts for one contact."""
    out = BudgetLedger(
        PlannerBudgetLimits(
            probes_per_contact=limits.probes_per_contact,
            paid_probes_per_contact=limits.paid_probes_per_contact,
            human_interruptions_per_contact=limits.human_interruptions_per_contact,
        )
    )
    counts = BudgetCounts(
        probes=ledger.probes_used, paid=ledger.paid_used, interruptions=ledger.interruptions
    )
    out._counts[contact_id] = counts  # noqa: SLF001 - seeding the ledger from the machine
    return out


def ran_probes(table: EvidenceTable, contact_id: str) -> frozenset[ProbeId]:
    """Probes that already produced rows for this contact (the ask-rung hail included)."""
    return frozenset(r.probe_id for r in table.for_contact(contact_id) if r.probe_id is not None)


class PlannerAdapter:
    """``Planner`` (machine Protocol) over a WO-031 ``choose(state, tables)`` planner."""

    def __init__(
        self,
        planner: ChoosePlanner,
        world: ProbeWorld,
        tables: LikelihoodTables,
        *,
        label: str = "",
    ) -> None:
        self.planner = planner
        self.world = world
        self.tables = tables
        self.label = label
        self.choices: list[RecordedChoice] = []

    def next_probe(
        self, table: EvidenceTable, posterior: PosteriorReport, budget: BudgetView, rung: Rung
    ) -> ProbeChoice | None:
        cid = posterior.contact_id
        tick_s = float(self.world.run.tick_s)
        # --- WO-088 begin --- (a look is a candidate only when the guard permits it)
        available = responder_available(self.world, cid, table, budget.tick)
        latency: dict[ProbeId, float] = {}
        if available:
            ctx0 = self.world.context(cid, budget.tick, rung)
            for pid in sorted(RESPONDER_PROBES):  # latency = the dispatch's transit time
                latency[pid] = float(resolve_args(pid, ctx0, table)["eta_ticks"]) * tick_s
        ledger = planner_ledger(cid, budget.ledger, budget.limits)
        ledger.seed_dispatches(cid, responder_dispatches(table, cid))
        # --- WO-088 end ---
        state = PlannerState(
            contact_id=cid,
            posterior=posterior.posterior,
            observed_confidence=observed_confidence(table, cid, self.tables),
            current_rung=rung,
            ledger=ledger,
            tick=budget.tick,
            tick_s=tick_s,
            ran_probes=ran_probes(table, cid),
            responder_available=available,  # WO-088
            probe_latency_s=latency,  # WO-088
        )
        if isinstance(self.planner, LlmPlanner):
            when = f"{self.label}{cid}/t={budget.tick}"
            choice = self.planner.choose(
                state, self.tables, table=table, report=posterior, when=when
            )
        else:
            choice = self.planner.choose(state, self.tables)
        self.choices.append(RecordedChoice(contact_id=cid, tick=budget.tick, choice=choice))
        if choice.probe_id is None:
            return None
        spec = ALLOWLIST[choice.probe_id]
        ctx = self.world.context(cid, budget.tick, rung)
        return ProbeChoice(
            probe_id=choice.probe_id,
            args=resolve_args(choice.probe_id, ctx, table),
            cost_usd=spec.cost_usd,
            latency_s=_landing_latency_s(choice.probe_id, ctx, table, spec.latency_s),  # WO-088
            interruption=spec.interruption,
            rung_required=spec.rung_required,
            source=choice.source,
            why=choice.why or "",
        )


# --- the writer --------------------------------------------------------------------------


class WriterAdapter:
    """``QuestionWriter`` over WO-032: the guarded writer, or the template question.

    Only a ``HANDOFF`` gets a question (a person takes the contact); the text is display
    only. With no model text the WO-032 template is used, then the part A template.
    """

    def __init__(self, writer: HypothesisWriter | None = None, *, label: str = "") -> None:
        self.writer = writer
        self.label = label

    def question_for_human(
        self, report: PosteriorReport, decision: StopDecision, table: EvidenceTable
    ) -> str | None:
        if decision.outcome is not StopOutcome.HANDOFF:
            return None
        text: str | None = None
        if self.writer is not None:
            when = f"{self.label}{report.contact_id}/stop"
            outcome = self.writer.write(report, table, when=when)
            text = outcome.value.question_for_human
        else:
            text = template_question(report)
        if text is None:
            text = TemplateQuestionWriter().question_for_human(report, decision, table)
        return text


# --- the kit and the handle --------------------------------------------------------------


@dataclass
class InvestigationKit:
    """Shared configuration for every investigation of one run."""

    tables: LikelihoodTables
    prior_cfg: PriorConfig
    stopping: StoppingConfig
    budgets: BudgetLimits
    world: ProbeWorld
    planner: ChoosePlanner = field(default_factory=DeterministicPlanner)
    writer: HypothesisWriter | None = None
    cache: ProbeCache = field(default_factory=ProbeCache)
    label: str = ""
    rung: Rung = Rung.ASK

    def __post_init__(self) -> None:
        self.planner_adapter = PlannerAdapter(
            self.planner, self.world, self.tables, label=self.label
        )
        self.writer_adapter = WriterAdapter(self.writer, label=self.label)

    @property
    def choices(self) -> list[RecordedChoice]:
        return self.planner_adapter.choices

    def handle(
        self,
        contact_id: str,
        table: EvidenceTable,
        *,
        max_tick: int,
        ledger: InvestigationLedger | None = None,
        checkpoint_path: str | Path | None = None,
    ) -> InvestigationHandle:
        return InvestigationHandle(
            contact_id,
            table,
            kit=self,
            max_tick=max_tick,
            ledger=ledger,
            checkpoint_path=checkpoint_path,
        )


class InvestigationHandle:
    """One contact's investigation, driven tick by tick.

    ``start(tick)`` builds the machine at ``tick`` and runs every transition that needs
    no time (score, stop rule, a zero-latency probe); ``step(tick)`` continues up to
    ``tick`` (a probe lands at ``sent + ceil(latency_s / tick_s)``). Both return the new
    checkpoints. ``recommendation`` is the frozen ``Recommendation`` once ``STOPPED``.
    """

    def __init__(
        self,
        contact_id: str,
        table: EvidenceTable,
        *,
        kit: InvestigationKit,
        max_tick: int,
        ledger: InvestigationLedger | None = None,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        self.contact_id = contact_id
        self.table = table
        self.kit = kit
        self.max_tick = max_tick
        self.ledger = ledger
        self.checkpoint_path = checkpoint_path
        self.machine: InvestigationMachine | None = None

    def start(self, tick: int) -> tuple[InvestigationCheckpoint, ...]:
        if self.machine is not None:
            raise RuntimeError(f"{self.contact_id}: investigation already started")
        kit = self.kit
        self.machine = InvestigationMachine(
            self.contact_id,
            self.table,
            planner=kit.planner_adapter,
            probe_runner=RunProbeRunner(kit.world, self.table, cache=kit.cache, rung=kit.rung),
            tables=kit.tables,
            prior_cfg=kit.prior_cfg,
            stopping=kit.stopping,
            budgets=kit.budgets,
            tick=tick,
            tick_s=float(kit.world.run.tick_s),
            max_tick=max(tick, self.max_tick),
            writer=kit.writer_adapter,
            ledger=self.ledger,
            rung=kit.rung,
            checkpoint_path=self.checkpoint_path,
        )
        return self.machine.advance(tick)

    def step(self, tick: int) -> tuple[InvestigationCheckpoint, ...]:
        if self.machine is None:
            raise RuntimeError(f"{self.contact_id}: investigation not started")
        return self.machine.advance(tick)

    @property
    def started(self) -> bool:
        return self.machine is not None

    @property
    def done(self) -> bool:
        return self.machine is not None and self.machine.done

    @property
    def state(self) -> InvestigationState | None:
        return None if self.machine is None else self.machine.state

    @property
    def recommendation(self) -> Recommendation | None:
        return None if self.machine is None else self.machine.recommendation

    def result(self) -> InvestigationResult:
        if self.machine is None:
            raise RuntimeError(f"{self.contact_id}: investigation not started")
        return self.machine.result()


def make_kit(
    world: ProbeWorld,
    *,
    tables: LikelihoodTables,
    prior_cfg: PriorConfig,
    stopping: StoppingConfig,
    budgets: BudgetLimits,
    llm_roles: bool = LLM_ROLES_DEFAULT,
    fixture_store: FixtureStore | None = None,
    violations_path: Path = DEFAULT_VIOLATIONS_PATH,
    label: str = "",
) -> InvestigationKit:
    """The kit a runner uses. ``llm_roles=False`` (default): ``DeterministicPlanner`` and
    the template question. ``llm_roles=True``: WO-032 ``make_planner`` (an ``LlmPlanner``
    over a replay-only client, no transport) and a guarded ``HypothesisWriter`` on the
    same replay-only store; every miss or rejection falls back to the deterministic
    choice or the template (CLAUDE.md 2.2, 2.3)."""
    planner = make_planner(llm_roles, fixture_store=fixture_store, violations_path=violations_path)
    writer: HypothesisWriter | None = None
    if llm_roles:
        client = ModelClient(mode=ModelMode.REPLAY, store=fixture_store)
        writer = HypothesisWriter(client, violations_path=violations_path)
    return InvestigationKit(
        tables=tables,
        prior_cfg=prior_cfg,
        stopping=stopping,
        budgets=budgets,
        world=world,
        planner=planner,
        writer=writer,
        label=label,
    )


__all__ = [
    "DEFAULT_VIOLATIONS_PATH",
    "LLM_ROLES_DEFAULT",
    "InvestigationHandle",
    "InvestigationKit",
    "PlannerAdapter",
    "ProbeWorld",
    "RecordedChoice",
    "RunProbeRunner",
    "WriterAdapter",
    "make_kit",
    "planner_ledger",
    "ran_probes",
]
