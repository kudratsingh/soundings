"""The S05-shaped driver stream with the WO-042 handoff service and audit sink attached.

``run_s05`` drives ``soundings.ladder.synthetic.s05_feed`` under the signed orders (a tmp
copy) with ``HandoffService`` as both the driver's SITREP writer and a ladder listener.
``writer_mode="template"`` makes no model call; ``"auto"`` replays from ``fixture_root``
(the repo fixtures by default). Used by tests/test_sitrep.py, tests/test_audit.py and the
determinism subprocesses.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from soundings.ladder.audit import AuditLog
from soundings.ladder.driver import LadderDriver, LadderRun
from soundings.ladder.handoff import HandoffService, WriterMode
from soundings.ladder.synthetic import S05_CONTACT, S05_HOUR, S05_TICK_S, s05_feed
from soundings.ladder.watch import DepartureDetector
from soundings.llm.client import FixtureStore, ModelClient, ModelMode
from soundings.schemas import ToggleName, load_harbor
from tests.support.guard_streams import signed_orders_copy

REJECTION_CONTACT = "c-0105"
"""The S05 shape under another contact id: its final SITREP replays the rung-change fixture."""


@dataclass
class S05Handoff:
    run: LadderRun
    service: HandoffService
    audit: AuditLog
    violations: Path


def run_s05(
    workdir: Path,
    *,
    writer_mode: WriterMode = "template",
    fixture_root: Path | None = None,
    cid: str = S05_CONTACT,
    toggles: tuple[ToggleName, ...] = (),
    audit_db: Path | str | None = None,
    run_id: str = "s05-seed11",
) -> S05Handoff:
    workdir.mkdir(parents=True, exist_ok=True)
    orders = signed_orders_copy(workdir)
    audit = AuditLog(audit_db if audit_db is not None else workdir / "audit.db")
    violations = workdir / "VIOLATIONS.md"
    client = ModelClient(mode=ModelMode.REPLAY, store=FixtureStore(fixture_root))
    detector = DepartureDetector.from_config(load_harbor(), S05_TICK_S)
    service = HandoffService(
        client,
        audit,
        run_id=run_id,
        violations_path=violations,
        writer_mode=writer_mode,
    )
    driver = LadderDriver.from_config(
        tick_s=S05_TICK_S,
        hour_of_day=S05_HOUR,
        orders_path=orders,
        toggles=toggles,
        departure=detector,
        listeners=(service,),
        sitrep_writer=service,
    )
    if service.sink is not None:
        service.sink.clock = driver.clock
    runs = driver.run([s05_feed(cid=cid)])
    return S05Handoff(runs[cid], service, audit, violations)


def main() -> int:
    """Print every SITREP digest and the audit chain head (the determinism subprocess)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = run_s05(Path(tmp), writer_mode=sys.argv[1] if len(sys.argv) > 1 else "auto")  # type: ignore[arg-type]
        for s in out.run.sitreps:
            sys.stdout.write(f"{s.kind} {s.tick} {s.digest}\n")
        sys.stdout.write(f"head {out.audit.verify().head}\n")
        out.audit.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
