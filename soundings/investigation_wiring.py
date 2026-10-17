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

import dataclasses  # WO-088D
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager  # WO-060B
from contextvars import ContextVar  # WO-060B
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
    advance_port,  # WO-088D
    catch_up,
    contact_fix,  # WO-088D
    latency_ticks,  # WO-088D
    look_eta_estimate_s,  # WO-088D
    probe_latency_ticks,
    release,  # WO-088D
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
    busy = busy or int(snap.tick) > tick  # WO-088D: a search already flown past now
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


# --- WO-088D begin ---
FIX_MAX_AGE_S = 30.0
"""Demo value: the platform dispatches to look only at a fix at most this old."""


class ResponderSession:
    """The harbor's responder as one run sees it (a ``ResponderPort`` over the sim).

    Every call goes to the injected port. Each dispatch is remembered by
    ``(kind, contact, tick)``: a look the platform dispatched when it was chosen is the
    same dispatch when the probe lands (``run_probe`` asks again at the send tick), so
    the responder really is in transit, and busy, between the two. ``step`` never moves
    time backwards (a search flown to its landing tick holds the responder's clock).
    """

    def __init__(self, port: ResponderPort) -> None:
        self.port = port
        self._memo: dict[tuple[str, str, int], Any] = {}

    def __getattr__(self, name: str) -> Any:  # trail, recall, inside_geofence, search_eta_s
        if name in ("port", "_memo"):
            raise AttributeError(name)
        return getattr(self.port, name)

    @property
    def snapshot(self) -> Any:
        return self.port.snapshot

    def dispatch_to_look(
        self, contact_id: str, contact_lat: float, contact_lon: float, tick: int
    ) -> Any:
        key = ("look", contact_id, tick)
        if key not in self._memo:
            self._memo[key] = self.port.dispatch_to_look(contact_id, contact_lat, contact_lon, tick)
        return self._memo[key]

    def dispatch_search(self, contact_id: str, search_box: Any, tick: int) -> Any:
        key = ("search", contact_id, tick)
        if key not in self._memo:
            self._memo[key] = self.port.dispatch_search(contact_id, search_box, tick)
        return self._memo[key]

    def step(
        self,
        tick: int,
        *,
        contact_lat: float | None = None,
        contact_lon: float | None = None,
        contact_speed_mps: float | None = None,
        nearest_asset_lat: float | None = None,
        nearest_asset_lon: float | None = None,
    ) -> Any:
        if tick < int(self.port.snapshot.tick):
            return self.port.snapshot
        return self.port.step(
            tick,
            contact_lat=contact_lat,
            contact_lon=contact_lon,
            contact_speed_mps=contact_speed_mps,
            nearest_asset_lat=nearest_asset_lat,
            nearest_asset_lon=nearest_asset_lon,
        )

    def travel_time_s(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        return float(self.port.travel_time_s(lat1, lon1, lat2, lon2))

    def dispatches(self) -> tuple[tuple[str, str, int, bool], ...]:
        """Every dispatch asked for: ``(kind, contact_id, tick, accepted)``, in order."""
        return tuple((k[0], k[1], k[2], bool(r.accepted)) for k, r in self._memo.items())


def advance_responder(world: ProbeWorld, tick: int, rung: Rung = Rung.ASK) -> None:
    """Step the run's responder to ``tick``: toward its target with the target's last
    fix and nearest asset while it has one, else on with what it was doing (return to
    dock, or docked). Called once per runner tick, so the trail has a point per tick."""
    port = world.responder
    if port is None:
        return
    snap = port.snapshot
    if int(snap.tick) >= tick:
        return
    target = getattr(snap, "target_contact_id", None)
    if target is None or track_upto(world.run, target, tick) is None:
        catch_up(port, tick)
        return
    advance_port(world.context(target, tick, rung), port, tick)


def fresh_fix(world: ProbeWorld, contact_id: str, tick: int) -> bool:
    """True when the contact's last fix is at most ``FIX_MAX_AGE_S`` old at ``tick``."""
    track = track_upto(world.run, contact_id, tick)
    if track is None:
        return False
    return (tick - track.points[-1].tick) * float(world.run.tick_s) <= FIX_MAX_AGE_S


def dispatch_look_now(
    world: ProbeWorld, contact_id: str, tick: int, rung: Rung, *, max_ticks: int | None = None
) -> dict[str, Any] | None:
    """Dispatch the responder to look at ``contact_id`` now; the probe args
    (``{"eta_ticks": n}``: the real transit ETA plus the settle time) or None when the
    responder declines, or when the look would land after ``max_ticks`` (then it is
    sent back to dock at once). The guard check is the caller's."""
    port = world.responder
    if port is None:
        return None
    ctx = world.context(contact_id, tick, rung)
    fix = contact_fix(ctx, tick)
    if fix is None:
        return None
    catch_up(port, tick)
    result = port.dispatch_to_look(contact_id, fix[0], fix[1], tick)
    if not result.accepted:
        return None
    from soundings.investigation.probes import LOOK_SETTLE_S

    n = latency_ticks(round(float(result.eta_s) + LOOK_SETTLE_S, 6), ctx.tick_s)
    if max_ticks is not None and n > max_ticks:
        release(port, tick)
        return None
    return {"eta_ticks": n}


def look_fits(world: ProbeWorld, contact_id: str, tick: int, rung: Rung, ticks_left: int) -> bool:
    """True when a look dispatched now would land within ``ticks_left`` (estimate)."""
    est = look_eta_estimate_s(world.context(contact_id, tick, rung))
    if est is None:
        return False
    return latency_ticks(round(est, 6), float(world.run.tick_s)) <= ticks_left


# --- WO-088D end ---


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
        self.eyes_on_gate: Callable[[str, int], bool] | None = None  # WO-085

    def next_probe(
        self, table: EvidenceTable, posterior: PosteriorReport, budget: BudgetView, rung: Rung
    ) -> ProbeChoice | None:
        cid = posterior.contact_id
        tick_s = float(self.world.run.tick_s)
        # --- WO-088 begin --- (a look is a candidate only when the guard permits it)
        available = responder_available(self.world, cid, table, budget.tick)
        # --- WO-088D begin --- (a look must land in time and look at a fresh fix)
        available = (
            available
            and fresh_fix(self.world, cid, budget.tick)
            and look_fits(self.world, cid, budget.tick, rung, budget.ticks_left)
        )
        # --- WO-088D end ---
        latency: dict[ProbeId, float] = {}
        if available:
            ctx0 = self.world.context(cid, budget.tick, rung)
            for pid in sorted(RESPONDER_PROBES):  # latency = the dispatch's transit time
                latency[pid] = float(resolve_args(pid, ctx0, table)["eta_ticks"]) * tick_s
        ledger = planner_ledger(cid, budget.ledger, budget.limits)
        ledger.seed_dispatches(cid, responder_dispatches(table, cid))
        # --- WO-088 end ---
        # --- WO-085 begin --- (the officer channel refuses an eyes-on look: no room left)
        if self.eyes_on_gate is not None and not self.eyes_on_gate(cid, budget.tick):
            ledger = eyes_on_refused(ledger, cid)
        # --- WO-085 end ---
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
        choice = self._choose(state, table, posterior, cid, budget.tick)
        # --- WO-088D begin --- (dispatch to look when chosen; the real ETA is the latency)
        look_args: dict[str, Any] | None = None
        if choice.probe_id is ProbeId.RESPONDER_LOOK:
            look_args = dispatch_look_now(
                self.world, cid, budget.tick, rung, max_ticks=budget.ticks_left
            )
            if look_args is None:  # declined: choose again without the responder
                state = dataclasses.replace(state, responder_available=False, probe_latency_s={})
                choice = self._choose(state, table, posterior, cid, budget.tick)
        # --- WO-088D end ---
        if choice.probe_id is None:
            return None
        spec = ALLOWLIST[choice.probe_id]
        ctx = self.world.context(cid, budget.tick, rung)
        if look_args is not None and choice.probe_id is ProbeId.RESPONDER_LOOK:  # WO-088D
            return ProbeChoice(
                probe_id=choice.probe_id,
                args=look_args,
                cost_usd=spec.cost_usd,
                latency_s=float(look_args["eta_ticks"]) * tick_s,
                interruption=spec.interruption,
                rung_required=spec.rung_required,
                source=choice.source,
                why=choice.why or "",
            )
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

    # --- WO-088D begin ---
    def _choose(
        self,
        state: PlannerState,
        table: EvidenceTable,
        posterior: PosteriorReport,
        cid: str,
        tick: int,
    ) -> PlannerChoice:
        if isinstance(self.planner, LlmPlanner):
            when = f"{self.label}{cid}/t={tick}"
            choice = self.planner.choose(
                state, self.tables, table=table, report=posterior, when=when
            )
        else:
            choice = self.planner.choose(state, self.tables)
        self.choices.append(RecordedChoice(contact_id=cid, tick=tick, choice=choice))
        return choice

    # --- WO-088D end ---


# --- WO-085 begin ---
def eyes_on_refused(ledger: BudgetLedger, contact_id: str) -> BudgetLedger:
    """A copy of ``ledger`` with the contact's interruption budget spent, so the planner
    picks its next probe or stops (WO-085: the officer channel's eyes-on cap). The
    per-contact ``BudgetLimits`` are untouched."""
    out = ledger.copy()
    c = out.counts(contact_id)
    limit = out.limits.human_interruptions_per_contact
    out._counts[contact_id] = c.model_copy(  # noqa: SLF001 - same seeding as planner_ledger
        update={"interruptions": max(c.interruptions, limit)}
    )
    return out


# --- WO-085 end ---


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


# --- WO-060B begin: stage client hook ---
_KIT_CLIENT: ContextVar[ModelClient | None] = ContextVar("soundings_kit_client", default=None)


@contextmanager
def use_kit_client(client: ModelClient | None) -> Iterator[None]:
    """Within the block, ``make_kit(llm_roles=True)`` builds its ``LlmPlanner`` and
    ``HypothesisWriter`` over ``client`` (the WO-060B run engine passes the stage live
    client here) instead of a fresh replay-only client. ``None`` changes nothing."""
    token = _KIT_CLIENT.set(client)
    try:
        yield
    finally:
        _KIT_CLIENT.reset(token)


# --- WO-060B end ---


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
        # --- WO-060B begin: stage client hook ---
        stage = _KIT_CLIENT.get()
        if stage is not None:
            client = stage
            planner = LlmPlanner(
                stage, deterministic=DeterministicPlanner(), violations_path=violations_path
            )
        # --- WO-060B end ---
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
    "use_kit_client",  # WO-060B
    "planner_ledger",
    "ran_probes",
]
