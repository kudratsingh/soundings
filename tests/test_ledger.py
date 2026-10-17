"""WO-086: the rung ledger and the blunt-policy counter (unit and rule-proof tests).

The ledger counts each observed contact once, in the bucket of its max rung so far, with
the eval helpers; the blunt counter steps once per unknown contact; ledger events follow
the event that changed a count; the background worker never changes the stream; the module
is display only; labels are clean and the red number is labelled a counterfactual.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from soundings.eval.policies import (
    LEDGER_BUCKET_OF_RUNG,
    BangPolicy,
    LedgerBucket,
    PolicyEvent,
    StateStep,
    Transition,
    closure_contacts,
    closure_count,
    closure_episodes,
    contact_spans,
    load_eval_config,
    rung_ledger,
)
from soundings.investigation.kalman import projection_view
from soundings.schemas import LadderState, Rung
from soundings.schemas.denylist import assert_clean
from soundings.schemas.scenario import Scenario
from soundings.schemas.track import Track
from soundings.sim.run import run_scenario
from soundings.sim.scenario import resolve_scenario
from soundings.stream.engine import BatchReplaySource, SourceRequest, stream_events
from soundings.stream.events import (
    LedgerEvent,
    StreamEvent,
    TransitionPayload,
    event_json,
    parse_event,
)
from soundings.stream.ledger import (
    TICK_OVERLAYS,
    UNAVAILABLE_MESSAGE,
    BluntCounter,
    LedgerAnnotator,
    RungLedger,
)
from soundings.stream.projection import projection_series

REPO = Path(__file__).resolve().parents[1]
BT01 = REPO / "scenarios" / "BT-01.yaml"
S = LadderState

needs_bt01 = pytest.mark.skipif(
    not BT01.exists(), reason="BT-01 comes from WO-080 part A (not on main yet)"
)
# Exit-test runs: BT-01 at seed 7 (when present) and S05 at seed 11.
RUNS = [pytest.param("BT-01", 7, marks=needs_bt01, id="BT-01"), pytest.param("S05", 11, id="S05")]


def _scenario(sid: str) -> Scenario:
    return resolve_scenario(sid)


@pytest.fixture(scope="module")
def s05() -> list[StreamEvent]:
    return stream_events("S05", 11)


def _tp(cid: str, frm: LadderState, to: LadderState) -> TransitionPayload:
    return TransitionPayload(
        contact_id=cid,
        case_id=f"{cid}#1",
        from_state=frm,
        event="x",
        to_state=to,
        reason_code="R",
        evidence_row_ids=["r1"],
    )


def _ledgers(events: list[StreamEvent]) -> list[LedgerEvent]:
    return [e for e in events if isinstance(e, LedgerEvent)]


# --- the shared helpers ------------------------------------------------------------------


def test_bucket_map_is_one_to_one() -> None:
    assert list(LEDGER_BUCKET_OF_RUNG) == list(Rung)
    assert sorted(LEDGER_BUCKET_OF_RUNG.values()) == sorted(LedgerBucket)


def test_rung_ledger_max_rung_buckets() -> None:
    steps: list[StateStep] = [
        _tp("a", S.DETECTED, S.WATCHING),
        _tp("b", S.DETECTED, S.WATCHING),
        _tp("b", S.WATCHING, S.ASKING),
        _tp("b", S.ASKING, S.CLEARED_AT_ASK),
        _tp("c", S.ASKING, S.ADVISING_SLOW),
        _tp("c", S.ADVISING_SLOW, S.HOLDING),
        # AWAITING_APPROVAL keeps the rung that asked (RUNG_OF_STATE None).
        _tp("c", S.HOLDING, S.AWAITING_APPROVAL),
        _tp("d", S.ASKING, S.ADVISING_SLOW),
        _tp("d", S.ADVISING_SLOW, S.AWAITING_APPROVAL),
        _tp("e", S.HOLDING, S.HANDED_OFF),
        _tp("e", S.HANDED_OFF, S.RESOLVED_DEPARTED),
    ]
    counts = rung_ledger(steps, seen=["a", "f"])
    assert counts == {
        LedgerBucket.RESOLVED_AT_WATCH: 2,  # a (watch only) and f (seen only)
        LedgerBucket.CLEARED_AT_ASK: 1,
        LedgerBucket.ADVISORIES: 1,
        LedgerBucket.HOLDS: 1,
        LedgerBucket.HANDED_OFF: 1,
    }
    assert sum(counts.values()) == 6
    # Eval Transitions satisfy the same Protocol.
    t = Transition(
        seq=0,
        contact_id="c-0099",
        tick=0,
        t_s=0.0,
        from_state=None,
        to_state=S.HANDED_OFF,
        event=PolicyEvent.STATE_CHANGE,
    )
    assert isinstance(t, StateStep)
    assert rung_ledger([t])[LedgerBucket.HANDED_OFF] == 1


def test_seen_only_contact_is_resolved_at_watch() -> None:
    ledger = RungLedger()
    sample = stream_events("S01", 1)
    tick = next(e for e in sample if e.type == "tick" and e.payload.contacts)
    assert ledger.observe(tick)
    assert ledger.counts()[LedgerBucket.RESOLVED_AT_WATCH] == len(
        {c.contact_id for c in tick.payload.contacts}
    )
    assert not ledger.observe(tick)  # nothing new


def test_ledger_moves_up_and_total_is_contacts_seen(s05: list[StreamEvent]) -> None:
    ledger = RungLedger()
    previous: dict[LedgerBucket, int] | None = None
    for e in s05:
        ledger.observe(e)
        counts = ledger.counts()
        assert sum(counts.values()) == ledger.contacts_seen
        if previous is not None:
            # A contact only moves up: the count at or above each bucket never falls.
            order = list(LedgerBucket)
            for i in range(len(order)):
                assert sum(counts[b] for b in order[i:]) >= sum(previous[b] for b in order[i:])
        previous = counts
    seen = {c.contact_id for e in s05 if e.type == "tick" for c in e.payload.contacts}
    steps = [e.payload for e in s05 if e.type == "transition"]
    assert ledger.counts() == rung_ledger(steps, seen=seen)
    final = _ledgers(s05)[-1].payload
    assert final.counts == ledger.counts()
    assert final.contacts_seen == len(seen)


def test_ledger_event_only_on_change(s05: list[StreamEvent]) -> None:
    ledger = RungLedger()
    blunt = BluntCounter(_scenario("S05"), 11).join()
    last: tuple[object, ...] | None = None
    n_ledger = 0
    for i, e in enumerate(s05):
        if isinstance(e, LedgerEvent):
            n_ledger += 1
            k = i - 1
            while s05[k].type in TICK_OVERLAYS:
                k -= 1
            before = s05[k]
            assert before.type in ("tick", "transition")
            assert (e.tick, e.t_s) == (before.tick, before.t_s)
            assert e.payload.counts == ledger.counts()
            key = (tuple(e.payload.counts.values()), e.payload.blunt.berth_closures)
            assert key != last, "a ledger event must carry a change"
            assert e.payload.blunt.berth_closures == blunt.closures_at(e.t_s)
            last = key
            continue
        ledger.observe(e)
        if e.type in ("tick", "transition"):
            key = (tuple(ledger.counts().values()), blunt.closures_at(e.t_s))
            j = i + 1
            while j < len(s05) and s05[j].type in TICK_OVERLAYS:
                j += 1
            nxt = s05[j] if j < len(s05) else None
            if key != last:
                assert isinstance(nxt, LedgerEvent), f"no ledger event after seq {e.seq}"
    assert n_ledger > 0


def test_annotator_restamps_and_round_trips(s05: list[StreamEvent]) -> None:
    assert [e.seq for e in s05] == list(range(len(s05)))
    assert s05[-1].type == "run_finished"
    assert s05[-1].payload.n_events == len(s05)
    for e in _ledgers(s05)[:5] + s05[:50]:
        back = parse_event(event_json(e))
        assert back == e
        assert event_json(back) == event_json(e)
    # Annotating an annotated stream changes nothing (the engine may see either).
    from soundings.stream.ledger import with_ledger

    again = with_ledger(s05, BluntCounter(_scenario("S05"), 11))
    assert [event_json(e) for e in again] == [event_json(e) for e in s05]


def test_track_hash_mismatch_shows_unavailable() -> None:
    sc = _scenario("S05")
    raw = BatchReplaySource(SourceRequest(scenario=sc, seed=11, run_id="r", speed=0)).events()
    wrong = BluntCounter(sc, 12)  # another seed: another sim run, another track hash
    out = LedgerAnnotator(wrong).annotate(raw)
    errors = [e for e in out if e.type == "error"]
    assert len(errors) == 1 and errors[0].payload.message == UNAVAILABLE_MESSAGE
    assert out[1] is errors[0] or out[1] == errors[0]
    ledgers = _ledgers(out)
    assert ledgers and all(not e.payload.blunt.available for e in ledgers)
    assert all(e.payload.blunt.berth_closures == 0 for e in ledgers)
    # Streaming with no known hash: the mismatch is caught at run_finished, before it.
    ann = LedgerAnnotator(BluntCounter(sc, 12))
    streamed: list[StreamEvent] = []
    for e in raw:
        streamed.extend(ann.feed(e))
    assert [e.seq for e in streamed] == list(range(len(streamed)))
    assert streamed[-1].type == "run_finished"
    assert streamed[-1].payload.n_events == len(streamed)
    tail = [e.type for e in streamed[-3:]]
    assert tail == ["error", "ledger", "run_finished"]
    assert not _ledgers(streamed)[-1].payload.blunt.available


def test_blunt_worker_error_shows_unavailable() -> None:
    def boom() -> None:
        raise RuntimeError("worker failed")

    sc = _scenario("S05")
    blunt = BluntCounter(sc, 11, worker_hook=boom).start().join()
    assert not blunt.available and blunt.error is not None
    raw = BatchReplaySource(SourceRequest(scenario=sc, seed=11, run_id="r", speed=0)).events()
    out = LedgerAnnotator(blunt).annotate(raw)
    assert any(e.type == "error" for e in out)
    assert all(not e.payload.blunt.available for e in _ledgers(out))


# --- exit test: bang counter increments on every unknown contact -------------------------


@pytest.mark.parametrize(("sid", "seed"), RUNS)
def test_blunt_steps_per_contact(sid: str, seed: int) -> None:
    sc = _scenario(sid)
    sim = run_scenario(sc, seed=seed)
    spans = contact_spans(sim)
    blunt = BluntCounter(sc, seed).start().join()
    assert blunt.available
    bang = BangPolicy().run(sc, seed)
    log = bang.transition_log
    covered = closure_contacts(log)
    # Every observed contact is covered once; filed ones too (bang never reads the registry).
    assert sorted(covered) == sorted(sp.contact_id for sp in spans)
    assert closure_count(log) == len(spans) == blunt.n_contacts
    # Step k lands at the tick of the k-th contact's closure step.
    step_tick: dict[str, int] = {}
    for t in log:
        if t.contact_id not in step_tick and (
            t.event is PolicyEvent.COVERED_BY_CLOSURE or t.event is PolicyEvent.APPROVAL_REQUESTED
        ):
            step_tick[t.contact_id] = t.tick
    ticks = sorted(step_tick.values())
    tick_s = float(sim.tick_s)
    for k, tick in enumerate(ticks):
        assert blunt.closures_at(tick * tick_s) >= k + 1
        if tick > 0:
            assert blunt.closures_at((tick - 1) * tick_s) <= k
    last = max(ticks)
    assert blunt.closures_at(last * tick_s) == len(spans)
    # The step function moves in unit steps per contact: the total of all steps is the
    # number of contacts, and it never falls.
    values = [blunt.closures_at(t * tick_s) for t in range(last + 1)]
    assert values == sorted(values)
    assert sum(b - a for a, b in zip([0, *values], values, strict=False)) == len(spans)
    # Episodes are the secondary number: one per closure request.
    assert blunt.episodes_at(last * tick_s) == closure_episodes(log) <= len(spans)
    # The streamed counter is the same step function.
    if sid == "S05":
        final = _ledgers(stream_events(sid, seed))[-1].payload.blunt
        assert final.berth_closures == len(spans)
        assert final.episodes == closure_episodes(log)


# --- determinism: the worker never changes the stream ------------------------------------


def _stream_hash(events: list[StreamEvent]) -> str:
    h = hashlib.sha256()
    for e in events:
        h.update(event_json(e).encode())
        h.update(b"\n")
    return h.hexdigest()


def test_thread_timing_irrelevant(s05: list[StreamEvent]) -> None:
    sc = _scenario("S05")
    base = _stream_hash(s05)
    raw = BatchReplaySource(SourceRequest(scenario=sc, seed=11, run_id="cli", speed=0)).events()
    slow = BluntCounter(sc, 11, worker_hook=lambda: time.sleep(0.3)).start()
    assert _stream_hash(LedgerAnnotator(slow).annotate(raw)) == base
    inline = BluntCounter(sc, 11)  # never started: computed inline on join
    assert _stream_hash(LedgerAnnotator(inline).annotate(raw)) == base
    env = {k: v for k, v in os.environ.items() if k != "PYTHONHASHSEED"}
    for hash_seed in ("0", "1"):
        out = subprocess.run(
            [sys.executable, "-m", "soundings.stream", "--scenario", "S05", "--seed", "11"],
            capture_output=True,
            check=True,
            cwd=REPO,
            env={**env, "PYTHONHASHSEED": hash_seed, "MODEL_MODE": "replay"},
        )
        assert hashlib.sha256(out.stdout).hexdigest() == base, hash_seed
        assert b'"type":"ledger"' in out.stdout


# --- projection per reading (WO-084's view, causal) --------------------------------------


def test_projection_series_matches_projection_view() -> None:
    sim = run_scenario(_scenario("S05"), seed=11)
    for cid in sorted(sim.tracks)[:3]:
        track = sim.tracks[cid]
        series = projection_series(track)
        assert len(series) == len(track.points)
        assert series[0] is None
        n = len(track.points)
        for i in sorted({1, 2, n // 2, n - 1}):
            if i < 1:
                continue
            prefix = Track(
                contact_id=track.contact_id,
                tick_s=track.tick_s,
                points=track.points[: i + 1],
            )
            assert series[i] == projection_view(prefix), (cid, i)


def test_tick_projection_is_streamed(s05: list[StreamEvent]) -> None:
    projected = [
        c.projection for e in s05 if e.type == "tick" for c in e.payload.contacts if c.projection
    ]
    assert projected
    for p in projected[:50]:
        assert p.ellipse.semi_major_m >= p.ellipse.semi_minor_m > 0


# --- rule proofs -------------------------------------------------------------------------

LEDGER_PY = REPO / "soundings" / "stream" / "ledger.py"
FORBIDDEN_IN_LEDGER = (
    "soundings.llm",
    "anthropic",
    "soundings.ladder.machine",
    "soundings.ladder.guards",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


def test_display_only() -> None:
    for name in _imports(LEDGER_PY):
        for bad in FORBIDDEN_IN_LEDGER:
            assert not (name == bad or name.startswith(bad + ".")), name
    decision_code = [
        *sorted((REPO / "soundings" / "ladder").rglob("*.py")),
        *sorted((REPO / "soundings" / "investigation").rglob("*.py")),
        REPO / "soundings" / "runner.py",
    ]
    for path in decision_code:
        for name in _imports(path):
            assert not name.startswith("soundings.stream.ledger"), path
            assert not name.startswith("soundings.stream.projection"), path
    from soundings.api import create_app

    paths = [getattr(r, "path", "") for r in create_app().routes]
    assert not [p for p in paths if "ledger" in p or "blunt" in p]


WEB = REPO / "web" / "src"
LEDGER_TSX = WEB / "components" / "RungLedger.tsx"


def _ts_strings(text: str, name: str) -> list[str]:
    """String literals inside ``export const NAME ... ;`` of a TS file."""
    m = re.search(rf"export const {name}\b[^=]*=\s*([\[{{].*?\n[\]}}])", text, re.S)
    assert m is not None, f"{name} not exported"
    return re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))


def test_labels_clean() -> None:
    text = LEDGER_TSX.read_text(encoding="utf-8")
    labels = _ts_strings(text, "LEDGER_LABELS")
    tips = _ts_strings(text, "LEDGER_TOOLTIPS")
    assert len(labels) >= 2 * len(LedgerBucket)  # key and label per bucket, plus blunt
    for bucket in LedgerBucket:
        assert bucket.value in labels
    for s in [*labels, *tips, *_ts_strings(text, "BLUNT_TOOLTIP")]:
        assert_clean(s)
    words = " ".join([*labels, *tips]).lower()
    # The blunt label names the baseline's closure, never a vehicle action.
    assert "berth closures under the blunt policy" in words


def test_blunt_labeled_counterfactual() -> None:
    text = LEDGER_TSX.read_text(encoding="utf-8")
    tip = " ".join(_ts_strings(text, "BLUNT_TOOLTIP"))
    assert "counterfactual" in tip
    assumed = " ".join(load_eval_config().baselines.assumptions["approval_assumed"].split())
    assert assumed in " ".join(tip.split())
