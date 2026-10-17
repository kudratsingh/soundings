# --- WO-060B begin: the runner v3 source ---
"""The ``v3`` run source: runner v3 stepped one tick at a time (WO-060 part B).

``ScenarioRunnerV3.start()`` precomputes the sim, the ask rung and the investigation
(runner v2) and builds the ladder feeds; this source then calls the ladder's ``step()`` once
per tick and turns what happened into the WO-060 event stream. Per tick, in order:

1. ``tick`` (observed readings of the extended sim the ladder watches), ``sensor_ranges``
   (WO-089) and ``responder`` (WO-088) when present;
2. the runner v2 evidence rows stamped at this tick (table order);
3. ``investigation_step`` for each planner choice whose probe landed at this tick (planner
   source ``deterministic | llm | fallback`` and ``why``, the probe's rows), then one
   ``posterior`` per contact so updated (WO-030 over the rows up to this tick);
4. ``recommendation`` (outcome, computed rung, stop clause) and its ``posterior``;
5. the ladder's steps: ``transition`` (guard and checkpoint hash; every cited row sent
   first), ``rung`` when it changed, ``ladder_action`` (advisory and hold with their
   standing-orders clause, approval request with scope and timeout, release, SITREP
   delivery, watch), ``approval_request`` / ``approval_result`` (granted, denied, timeout,
   backup paged), ``sitrep`` (WO-042 draft / final / update with markdown), the runner's
   LOST_VEHICLE notice (search box) and the cases the ladder does not drive.

Every decision is the runner's: this module only reads results (CLAUDE.md 2.2). The ladder
runs on its own TickClock, so the stream is identical at every pace (2.4). After
HANDED_OFF only WATCH_AFTER_HANDOFF and RESOLVED_DEPARTED follow (2.1).

``manual=True`` puts a ``ManualApprover`` on the ladder: the run waits on a person (the
WO-061 approval button) and ``deliver_approval(token)`` hands the person's WO-041 token to
the ladder, whose guard checks it against the requested scope on the next tick.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from soundings.canonical import round6, stable_hash
from soundings.investigation.posterior import (
    Posterior,
    load_likelihood_tables,
    load_prior_config,
    score_contact,
)
from soundings.investigation_wiring import use_kit_client
from soundings.ladder.approval import verify_token
from soundings.ladder.handoff import evidence_table
from soundings.ladder.machine import StepResult
from soundings.ladder.table import rung_after
from soundings.ladder.types import ActionKind, ActionScope, EventKind
from soundings.llm.guard import DEFAULT_VIOLATIONS_PATH
from soundings.runner import (
    CasePlannerChoice,
    LostVehicleNotice,
    ManualApprover,
    RunSession,
    ScenarioRunnerV3,
    Transition,
)
from soundings.schemas.config import STANDING_ORDERS_PATH
from soundings.schemas.enums import Hypothesis, LadderState, Rung, ToggleName
from soundings.schemas.evidence import EvidenceRow
from soundings.schemas.harbor import load_harbor
from soundings.schemas.scenario import Scenario, Toggle
from soundings.sim.hashing import track_hash
from soundings.stream.events import (
    ApprovalRequestEvent,
    ApprovalRequestPayload,
    ApprovalResultEvent,
    ApprovalResultPayload,
    EvidenceRowEvent,
    EvidenceRowPayload,
    GuardInfo,
    InvestigationStepEvent,
    InvestigationStepPayload,
    LadderActionEvent,
    LadderActionPayload,
    PosteriorEvent,
    PosteriorPayload,
    RecommendationEvent,
    RecommendationPayload,
    ResponderEvent,
    RunFinished,
    RunFinishedPayload,
    RungEvent,
    RungPayload,
    RunStarted,
    RunStartedPayload,
    SensorRangesEvent,
    SitrepEvent,
    SitrepPayload,
    StreamEvent,
    TickContact,
    TickEvent,
    TickPayload,
    TransitionEvent,
    TransitionPayload,
)
from soundings.stream.ledger import LedgerAnnotator
from soundings.stream.projection import projection_series
from soundings.stream.responder import responder_payloads, responder_snapshots
from soundings.stream.stage import RunClient, client_for_run

SOURCE_NAME = "v3"
APPROVER_PRIMARY = "primary"
APPROVER_BACKUP = "backup"

_ACTION_LABEL: dict[str, str] = {
    "hold_longer": "Keep the hold on {asset} longer (up to {minutes} min)",
    "hold_one_gate_or_berth": "Hold {asset} (up to {minutes} min)",
    "advisory_one_basin": "Slow advisory for {basin} (up to {minutes} min)",
}


class NotManual(LookupError):
    """``deliver_approval`` on a run whose ladder has the simulated approver."""


class TokenRefused(ValueError):
    """The approval token does not verify for the open request (WO-041 ``verify_token``)."""


def _signed_by(path: Path = STANDING_ORDERS_PATH) -> str | None:
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        name = doc["orders"]["signed_by"]
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return None
    return str(name) if isinstance(name, str) else None


def _approvers(path: Path = STANDING_ORDERS_PATH) -> dict[str, str]:
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        raw = doc["orders"]["approvers"]
        return {k: str(v["name"]) for k, v in sorted(raw.items())}
    except (OSError, KeyError, TypeError, AttributeError, yaml.YAMLError):
        return {}


SURGE_DEFAULT_N = 6
"""``contact_surge`` from the run-start toggles: extra contacts (stress-test value)."""


def run_toggles(scenario: Scenario, toggles: Iterable[ToggleName]) -> Scenario:
    """``scenario`` with the run-start toggles added (a toggle it already has is kept).

    The UI starts a run with bare names, so the two sim toggles that need parameters get
    run defaults: ``sensor_dropout`` takes the first hydrophone node (id order) offline at a
    quarter of the scenario, ``contact_surge`` adds ``SURGE_DEFAULT_N`` contacts.
    ``contact_goes_quiet`` names a contact and cannot start from a bare name."""
    from soundings.stream.engine import UnsupportedOption

    have = {t.name for t in scenario.toggles}
    extra: list[Toggle] = []
    for name in sorted(set(toggles), key=lambda t: t.value):
        if name in have:
            continue
        if name is ToggleName.SENSOR_DROPOUT:
            nodes = sorted(n.node_id for n in load_harbor().sensor_nodes)
            extra.append(Toggle(name=name, node_id=nodes[0], at_tick=scenario.duration_ticks // 4))
        elif name is ToggleName.CONTACT_SURGE:
            extra.append(Toggle(name=name, n=SURGE_DEFAULT_N))
        elif name is ToggleName.CONTACT_GOES_QUIET:
            raise UnsupportedOption("contact_goes_quiet needs a contact id (scenario file)")
        else:
            extra.append(Toggle(name=name))
    return scenario.model_copy(update={"toggles": scenario.toggles + tuple(extra)})


def action_label(action: str, scope: Mapping[str, Any]) -> str:
    """A plain label for an approval request (what the person is asked to approve)."""
    template = _ACTION_LABEL.get(action)
    asset = scope.get("asset_id") or "the berth"
    basin = scope.get("basin") or "the basin"
    minutes = scope.get("max_minutes") or "?"
    if template is None:
        return action.replace("_", " ")
    return template.format(asset=asset, basin=basin, minutes=minutes)


def posterior_payload(contact_id: str, probs: Mapping[str, float]) -> PosteriorPayload:
    """A ``posterior`` payload from a hypothesis -> probability mapping (6 dp)."""
    post = Posterior.from_weights({Hypothesis(k): float(v) for k, v in probs.items()})
    return PosteriorPayload(
        contact_id=contact_id,
        probs={h: round6(p) for h, p in sorted(post.probs.items(), key=lambda kv: kv[0].value)},
        entropy=round6(post.entropy()),
        top2_margin=round6(post.top2_margin()),
        threat_mass=round6(post.threat_mass()),
        benign_mass=round6(post.benign_mass()),
    )


def _cited_ids(obj: Any) -> set[str]:
    """Every row id a SITREP dict cites (``evidence_ids`` / ``evidence_row_ids`` lists)."""
    out: set[str] = set()
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if k in ("evidence_ids", "evidence_row_ids") and isinstance(v, (list, tuple)):
                out.update(str(i) for i in v)
            else:
                out |= _cited_ids(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out |= _cited_ids(v)
    return out


class V3Source:
    """Runner v3 stepped per tick; events are produced lazily by ``pull(until_t_s)``."""

    name = SOURCE_NAME
    lazy = True

    def __init__(
        self,
        req: Any,
        *,
        manual: bool = False,
        violations_path: Path | None = None,
        client_factory: Callable[[bool], RunClient] = client_for_run,
    ) -> None:
        self.req = req
        toggles = tuple(ToggleName(t) for t in req.toggles)
        scenario = run_toggles(req.scenario, toggles) if toggles else req.scenario
        self.scenario = scenario
        self.run_client = client_factory(bool(req.use_llm))
        client = self.run_client.client
        self.manual = manual
        self.approver = ManualApprover() if manual else None
        vpath = violations_path if violations_path is not None else DEFAULT_VIOLATIONS_PATH
        # --- WO-085: surge triage --- (a run carrying contact_surge goes through the
        # triage queue and the officer channel; every other run keeps triage=None)
        from soundings.triage import load_triage_config

        surge = any(t.name is ToggleName.CONTACT_SURGE for t in scenario.toggles)
        triage = load_triage_config() if surge else None
        # --- WO-085 end ---
        with use_kit_client(client):
            self.rs: RunSession = ScenarioRunnerV3(scenario, seed=req.seed, triage=triage).start(
                llm_roles=self.run_client.llm_roles,
                violations_path=vpath,
                toggles=toggles,
                approver=self.approver,
                writer_mode="auto" if client is not None else "template",
                sitrep_client=client,
                run_id=req.run_id,
            )
        self.v2 = self.rs.v2
        self.tick_s = float(self.v2.sim.tick_s)
        self.timings = self.rs.driver.timings
        self.signed_by = None if self.rs.unsigned else _signed_by()
        self.approver_names = _approvers()
        self.buffer: list[StreamEvent] = []
        self._annotator: LedgerAnnotator | None = None
        self._seq = 0
        self._tick = -1
        self._exhausted = False
        self._sent: set[str] = set()
        self._rows: dict[str, EvidenceRow] = {r.row_id: r for r in self.v2.evidence}
        self._rung: dict[str, Rung] = {}
        self._state: dict[str, LadderState] = {}
        self._open: dict[str, list[str]] = defaultdict(list)  # contact -> open request ids
        self._scope: dict[str, dict[str, Any]] = {}  # contact -> the scope last requested
        self._packets = 0
        self._sitrep_version: dict[str, int] = defaultdict(int)
        self._index()
        self._start()

    # --- precomputed per-tick maps ----------------------------------------------------------

    def _index(self) -> None:
        v2 = self.v2
        sim = self.rs.sim_ext
        self.readings: dict[int, list[TickContact]] = defaultdict(list)
        for cid in sorted(sim.tracks):
            projections = projection_series(sim.tracks[cid])  # WO-086, as the batch source
            for p, projection in zip(sim.tracks[cid].points, projections, strict=True):
                self.readings[p.tick].append(
                    TickContact(
                        contact_id=cid,
                        lat=round6(p.lat),
                        lon=round6(p.lon),
                        depth_m=round6(p.depth_m),
                        confidence=round6(p.confidence),
                        sensor_id=p.sensor_id,
                        projection=projection,
                    )
                )
        self.rows_at: dict[int, list[EvidenceRow]] = defaultdict(list)
        for row in v2.evidence:
            self.rows_at[row.t].append(row)
        # Planner choices land when their probe's rows land.
        self.steps_at: dict[int, list[tuple[CasePlannerChoice, list[str]]]] = defaultdict(list)
        taken: set[str] = set()
        for c in v2.planner_choices:
            ids: list[str] = []
            landed = c.tick
            if c.choice.probe_id is not None:
                pid = c.choice.probe_id.value
                for r in v2.evidence:
                    if (
                        r.contact_id == c.contact_id
                        and r.probe_id is not None
                        and r.probe_id.value == pid
                        and r.t >= c.tick
                        and r.row_id not in taken
                    ):
                        ids.append(r.row_id)
                if ids:
                    taken.update(ids)
                    landed = max(self._rows[i].t for i in ids)
            self.steps_at[landed].append((c, sorted(ids)))
        self.recs_at: dict[int, list[tuple[str, Any]]] = defaultdict(list)
        for case_id, rec in v2.recommendations.items():
            self.recs_at[rec.tick].append((case_id, rec))
        laddered = set(self.rs.case_of.values())
        self.other_at: dict[int, list[Transition]] = defaultdict(list)
        for t in sorted(v2.transitions, key=lambda t: t.seq):
            if t.case_id not in laddered:
                self.other_at[t.tick].append(t)
        self.notices_at: dict[int, list[LostVehicleNotice]] = defaultdict(list)
        for n in v2.notices:
            self.notices_at[n.tick].append(n)
        last = [sim.n_ticks - 1, *self.rows_at, *self.steps_at, *self.recs_at, *self.notices_at]
        self.last_tick = max(0, *last)
        # Sensor rings (WO-089) and the responder (WO-088), as the batch source streams them.
        from soundings.stream.engine import sensor_range_events_at

        self.ranges_at = sensor_range_events_at(sim, self.last_tick + 1)
        snaps = responder_snapshots(v2, self.scenario)
        self.responder_at = responder_payloads(snaps) if snaps else {}

    # --- emitting ----------------------------------------------------------------------------

    def _stamp(self, tick: int) -> dict[str, object]:
        out = {
            "run_id": self.req.run_id,
            "seq": self._seq,
            "tick": tick,
            "t_s": round6(tick * self.tick_s),
        }
        self._seq += 1
        return out

    def _emit(self, cls: Any, tick: int, payload: Any) -> None:
        self._push(cls.model_validate({**self._stamp(tick), "payload": payload}))

    def _push(self, event: StreamEvent) -> None:
        """Append one event; with a WO-086 ledger annotator, its re-stamped output."""
        if self._annotator is None:
            self.buffer.append(event)
        else:
            self.buffer.extend(self._annotator.feed(event))

    @property
    def track_hash(self) -> str:
        """The scenario-length sim's track hash (what ``run_finished`` reports)."""
        return track_hash(self.v2.sim)

    def annotate(self, annotator: LedgerAnnotator) -> None:
        """Stream WO-086 ``ledger`` events too: the annotator re-stamps ``seq`` and adds a
        ledger event whenever a count changes, event by event as the run is stepped."""
        so_far = list(self.buffer)
        self.buffer.clear()
        self._annotator = annotator
        for event in so_far:
            self._push(event)

    def _send_row(self, row: EvidenceRow, tick: int) -> None:
        if row.row_id in self._sent:
            return
        self._sent.add(row.row_id)
        self._rows.setdefault(row.row_id, row)
        self._emit(EvidenceRowEvent, tick, EvidenceRowPayload(row_id=row.row_id, row=row))

    def _send_ids(self, ids: Iterable[str], tick: int) -> None:
        for i in sorted(set(ids)):
            row = self._rows.get(i)
            if row is not None:
                self._send_row(row, tick)

    def _start(self) -> None:
        sc = self.scenario
        self._emit(
            RunStarted,
            0,
            RunStartedPayload(
                scenario_id=sc.id,
                scenario_name=sc.name,
                seed=self.req.seed,
                tick_s=self.tick_s,
                n_ticks=self.v2.sim.n_ticks,
                speed=round6(self.req.speed),
                toggles=sorted(self.rs.toggles, key=lambda t: t.value),
                source="stepped",
                runner="v3",
                model_path=self.run_client.path,
                orders="unsigned" if self.rs.unsigned else "signed",
            ),
        )

    # --- the lazy stepping -------------------------------------------------------------------

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    def pull(self, until_t_s: float) -> int:
        """Step every tick with ``t_s <= until_t_s``; returns how many events were added."""
        before = len(self.buffer)
        while not self._exhausted:
            nxt = self._tick + 1
            if nxt * self.tick_s > until_t_s:
                break
            self._tick = nxt
            self._do_tick(nxt)
            if nxt >= self.last_tick and self.rs.done:
                self._finish(nxt)
        return len(self.buffer) - before

    def events(self) -> list[StreamEvent]:
        """The whole stream (steps to the end; a manual run's requests time out)."""
        self.pull(math.inf)
        return self.buffer

    def _do_tick(self, tick: int) -> None:
        self._emit(TickEvent, tick, TickPayload(contacts=self.readings.get(tick, [])))
        if tick in self.ranges_at:
            self._push(
                SensorRangesEvent.model_validate(
                    {**self._stamp(tick), "payload": self.ranges_at[tick]}
                )
            )
        if tick in self.responder_at:
            self._push(
                ResponderEvent.model_validate(
                    {**self._stamp(tick), "payload": self.responder_at[tick]}
                )
            )
        for row in self.rows_at.get(tick, []):
            self._send_row(row, tick)
        self._investigation(tick)
        if not self.rs.done:
            for r in self.rs.session.step():
                self._ladder_step(r, tick)
            self._sitreps(tick)
        for t in self.other_at.get(tick, []):
            self._transition_v2(t, tick)
        for n in self.notices_at.get(tick, []):
            self._notice(n, tick)

    # --- investigation -------------------------------------------------------------------

    def _investigation(self, tick: int) -> None:
        touched: list[str] = []
        for c, ids in self.steps_at.get(tick, []):
            self._send_ids(ids, tick)
            ch = c.choice
            self._emit(
                InvestigationStepEvent,
                tick,
                InvestigationStepPayload(
                    contact_id=c.contact_id,
                    case_id=c.case_id,
                    probe_id=None if ch.probe_id is None else ch.probe_id.value,
                    source=ch.source,
                    fallback_reason=ch.fallback_reason,
                    why=ch.why,
                    reason=ch.reason,
                    sent_tick=c.tick,
                    landed_tick=tick,
                    row_ids=ids,
                ),
            )
            if ids and c.contact_id not in touched:
                touched.append(c.contact_id)
        for cid in touched:
            self._posterior_now(cid, tick)
        for case_id, rec in self.recs_at.get(tick, []):
            self._send_ids(rec.evidence_row_ids, tick)
            self._emit(
                RecommendationEvent,
                tick,
                RecommendationPayload(
                    contact_id=rec.contact_id,
                    case_id=case_id,
                    outcome=rec.outcome.value,
                    recommended_rung=rec.recommended_rung,
                    stop_clause=rec.rule_id.value,
                    threat_mass=round6(rec.threat_mass),
                    benign_mass=round6(rec.benign_mass),
                    top=rec.top,
                    uncertain=rec.uncertain,
                    question_for_human=rec.question_for_human,
                    evidence_row_ids=sorted(i for i in rec.evidence_row_ids if i in self._sent),
                ),
            )
            self._emit(PosteriorEvent, tick, posterior_payload(rec.contact_id, rec.posterior))

    def _posterior_now(self, contact_id: str, tick: int) -> None:
        table = evidence_table(
            r for r in self.v2.evidence.rows() if r.contact_id == contact_id and r.t <= tick
        )
        report = score_contact(table, contact_id, _TABLES(), _PRIORS())
        probs = {h.value: p for h, p in report.posterior.probs.items()}
        self._emit(PosteriorEvent, tick, posterior_payload(contact_id, probs))

    # --- the ladder ----------------------------------------------------------------------

    def _ladder_step(self, r: StepResult, tick: int) -> None:
        cp = r.checkpoint
        if cp is None:
            return
        cid = cp.contact_id
        t_s = round6(cp.now_s)
        rows = cp.event.payload.rows
        for row in sorted(rows, key=lambda x: (x.t, x.row_id)):
            self._send_row(row, tick)
        guard = None
        if cp.guard_decisions:
            d = cp.guard_decisions[0]
            guard = GuardInfo(
                name=d.guard.value,
                result=d.verdict.value,
                inputs={"reason": d.reason, "inputs_hash": d.inputs_hash},
            )
        arg = cp.event.arg
        self._emit(
            TransitionEvent,
            tick,
            TransitionPayload(
                contact_id=cid,
                case_id=self.rs.case_of.get(cid, cid),
                from_state=cp.from_state,
                event=cp.event.kind.value,
                to_state=cp.to_state,
                reason_code=cp.event.kind.value if arg is None else f"{cp.event.kind.value}:{arg}",
                evidence_row_ids=sorted({row.row_id for row in rows}),
                guard=guard,
                audit_row_hash=cp.hash,
            ),
        )
        self._state[cid] = cp.to_state
        previous = self._rung.get(cid, Rung.WATCH)
        rung = rung_after(previous, cp.to_state)
        if cid not in self._rung or rung is not previous:
            self._rung[cid] = rung
            self._emit(RungEvent, tick, RungPayload(contact_id=cid, rung=rung, state=cp.to_state))
        if cp.event.kind is EventKind.APPROVAL and arg in ("granted", "denied", "timeout"):
            paged = any(a.kind is ActionKind.PAGE_BACKUP for a in r.actions)
            outcome = "backup_paged" if arg == "timeout" and paged else arg
            self._approval_result(cid, outcome, cp.event.payload.approval_token, tick)
        for action in r.actions:
            self._action(cid, cp.to_state, cp.rung, action, t_s, tick)

    def _action(
        self, cid: str, state: LadderState, rung: Rung, action: Any, t_s: float, tick: int
    ) -> None:
        kind: ActionKind = action.kind
        scope = None if action.scope is None else action.scope.model_dump(mode="json")
        clause: str | None = None
        signed_by: str | None = None
        timeout: float | None = None
        if action.scope is not None and kind in (
            ActionKind.ISSUE_SLOW_ADVISORY,
            ActionKind.START_HOLD,
        ):
            clause = f"standing_orders.{rung.value}.{action.scope.action}"
            signed_by = self.signed_by
        elif action.scope is not None and kind is ActionKind.RECORD_APPROVED_ACTION:
            clause = f"approval.{action.scope.action}"
        if kind is ActionKind.REQUEST_APPROVAL:
            timeout = float(self.timings.primary_timeout_s)
        elif kind is ActionKind.PAGE_BACKUP:
            timeout = float(self.timings.backup_timeout_s or self.timings.primary_timeout_s)
        expires = None if timeout is None else round6(t_s + timeout)
        self._emit(
            LadderActionEvent,
            tick,
            LadderActionPayload(
                contact_id=cid,
                state=state,
                action=kind.value,
                scope=scope,
                clause=clause,
                signed_by=signed_by,
                timeout_s=timeout,
                expires_t_s=expires,
            ),
        )
        if kind is ActionKind.REQUEST_APPROVAL and scope is not None and expires is not None:
            self._approval_request(cid, rung, scope, expires, APPROVER_PRIMARY, tick)
        elif kind is ActionKind.PAGE_BACKUP and expires is not None:
            for rid in self._open.pop(cid, []):
                self._emit(
                    ApprovalResultEvent,
                    tick,
                    ApprovalResultPayload(request_id=rid, outcome="backup_paged"),
                )
            self._approval_request(cid, rung, scope or {}, expires, APPROVER_BACKUP, tick)

    def _approval_request(
        self,
        cid: str,
        rung: Rung,
        scope: Mapping[str, Any],
        expires: float,
        who: str,
        tick: int,
    ) -> None:
        action = str(scope.get("action", "approval"))
        rid = f"apr-{cid}-{tick}-{who}"
        self._open[cid].append(rid)
        self._scope[cid] = dict(scope)
        self._emit(
            ApprovalRequestEvent,
            tick,
            ApprovalRequestPayload(
                contact_id=cid,
                request_id=rid,
                action=action,
                action_label=action_label(action, scope),
                scope=dict(scope),
                recommended_rung=Rung.HAND_TO_PERSON if rung is Rung.HOLD else rung,
                expires_t_s=expires,
                approver=self.approver_names.get(who, who),
            ),
        )

    def _approval_result(
        self, cid: str, outcome: str, token: Mapping[str, Any] | None, tick: int
    ) -> None:
        for rid in self._open.pop(cid, []):
            self._emit(
                ApprovalResultEvent,
                tick,
                ApprovalResultPayload(
                    request_id=rid,
                    outcome=outcome,  # type: ignore[arg-type]
                    token_hash=None if token is None else stable_hash(dict(token)),
                ),
            )

    def _sitreps(self, tick: int) -> None:
        packets = self.rs.service.packets
        for p in packets[self._packets :]:
            data = p.sitrep.to_dict()
            if p.inputs is not None:  # rows the writer read (e.g. the ladder's monitor rows)
                for row in p.inputs.evidence:
                    self._rows.setdefault(row.row_id, row)
            self._send_ids(_cited_ids(data), tick)
            self._sitrep_version[p.contact_id] += 1
            self._emit(
                SitrepEvent,
                tick,
                SitrepPayload(
                    contact_id=p.contact_id,
                    version=self._sitrep_version[p.contact_id],
                    writer=p.sitrep.writer.value,
                    kind="sitrep",
                    sitrep=data,
                    status=p.status.value,
                    markdown=p.markdown,
                    path=p.path.value,
                ),
            )
        self._packets = len(packets)

    def _notice(self, n: LostVehicleNotice, tick: int) -> None:
        self._send_ids(n.evidence_row_ids, tick)
        for line in n.lines:
            self._send_ids(line.evidence_row_ids, tick)
        self._sitrep_version[n.contact_id] += 1
        self._emit(
            SitrepEvent,
            tick,
            SitrepPayload(
                contact_id=n.contact_id,
                version=self._sitrep_version[n.contact_id],
                writer="template",
                kind="lost_vehicle",
                notice=n,
            ),
        )

    def _transition_v2(self, t: Transition, tick: int) -> None:
        """A runner v2 case the ladder does not drive (a contact's later case)."""
        self._send_ids(t.evidence_row_ids, tick)
        self._emit(
            TransitionEvent,
            tick,
            TransitionPayload(
                contact_id=t.contact_id,
                case_id=t.case_id,
                from_state=t.from_state,
                event=t.event.value,
                to_state=t.to_state,
                reason_code=t.reason_code,
                evidence_row_ids=list(t.evidence_row_ids),
            ),
        )
        if t.contact_id not in self.rs.case_of:
            self._state[t.contact_id] = t.to_state

    def _finish(self, tick: int) -> None:
        result = self.rs.result()
        n_events = len(self.buffer) + 1
        self._emit(
            RunFinished,
            tick,
            RunFinishedPayload(
                terminal=dict(sorted(self._state.items())),
                track_hash=track_hash(self.v2.sim),
                transition_log_hash=result.ladder_log_hash,
                n_events=n_events,
            ),
        )
        if self._annotator is not None:
            self.buffer.extend(self._annotator.flush())
        if result.audit is not None:
            result.audit.close()
        self._exhausted = True

    # --- approvals -----------------------------------------------------------------------

    def open_requests(self) -> dict[str, list[str]]:
        """Open approval request ids by contact."""
        return {k: list(v) for k, v in sorted(self._open.items()) if v}

    def approve(self, request_id: str, approver: str) -> ApprovalResultEvent:
        from soundings.stream.engine import NoPendingApproval

        raise NoPendingApproval(
            "the v3 source takes a person's approval only as a WO-041 token "
            f"(deliver_approval); no token-less approval of {request_id!r}"
        )

    def deliver_approval(self, token: Mapping[str, Any]) -> str:
        """Hand a person's WO-041 approval token to the ladder (WO-061's button).

        The ladder's guard checks the token against the requested scope on the next tick;
        a token for another scope changes nothing. Returns the token's contact id."""
        from soundings.stream.engine import NoPendingApproval

        if self.approver is None:
            raise NotManual("this run's ladder uses the simulated approver (start manual=True)")
        cid = str(token.get("contact_id", ""))
        if not self._open.get(cid):
            raise NoPendingApproval(f"no open approval request for {cid!r}")
        # The ladder's guard decides on the next tick; a token it would refuse is not
        # forwarded (a refused grant has no checkpoint, and the WO-042 audit sink needs one).
        now_s = round6((self._tick + 1) * self.tick_s)
        verdict = verify_token(
            token,
            contact_id=cid,
            requested=ActionScope.model_validate(self._scope[cid]),
            now_s=now_s,
        )
        if not verdict.permits:
            raise TokenRefused(verdict.reason)
        self.rs.approve(cid, "granted", token=token)
        return cid


_tables_cache: dict[str, Any] = {}


def _TABLES() -> Any:  # noqa: N802
    if "t" not in _tables_cache:
        _tables_cache["t"] = load_likelihood_tables()
    return _tables_cache["t"]


def _PRIORS() -> Any:  # noqa: N802
    if "p" not in _tables_cache:
        _tables_cache["p"] = load_prior_config()
    return _tables_cache["p"]


def v3_source(req: Any) -> V3Source:
    """The registered ``v3`` factory (simulated approver)."""
    return V3Source(req)


def v3_manual_source(req: Any) -> V3Source:
    """The registered ``v3-manual`` factory (a person approves; WO-061 wires the button)."""
    return V3Source(req, manual=True)


__all__ = [
    "SOURCE_NAME",
    "NotManual",
    "TokenRefused",
    "V3Source",
    "action_label",
    "posterior_payload",
    "v3_manual_source",
    "v3_source",
]
# --- WO-060B end ---
