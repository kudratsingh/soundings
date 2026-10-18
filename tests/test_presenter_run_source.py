"""Live presenter runs must start on the runner v3 source (WO-062B stage blocker).

The recorder patched the request at record time; the stage presenter has to send it itself,
or Harbor Night never reaches the hold and the twist toggles are refused (HTTP 400).
"""

from __future__ import annotations

from pathlib import Path

PRESENTER = Path(__file__).resolve().parents[1] / "web" / "src" / "presenter.ts"
API = Path(__file__).resolve().parents[1] / "web" / "src" / "api.ts"


def test_presenter_run_start_sends_the_v3_source() -> None:
    src = PRESENTER.read_text(encoding="utf-8")
    start = src.split("export async function startStepRun", 1)[1]
    assert "source: RUN_SOURCE" in start
    head = src.split("export async function startStepRun")[0]
    assert "RUN_SOURCE" in head  # imported from ./api
    assert "export const RUN_SOURCE = 'v3'" in API.read_text(encoding="utf-8")
