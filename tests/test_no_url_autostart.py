"""Pressing Start must not write the scenario into the URL (owner directive 2026-10-17 17:50).

A restored or reopened tab would otherwise auto-start the last run on load. Deep links
(?scenario=...) keep starting on load for the shot scripts and the e2e smokes.
"""

from __future__ import annotations

from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "web" / "src" / "App.tsx"


def test_begin_does_not_rewrite_the_url_with_the_scenario() -> None:
    src = APP.read_text(encoding="utf-8")
    begin = src.split("const begin = (", 1)[1].split("\n  }", 1)[0]
    assert "replaceState" not in begin
    assert "q.set('scenario'" not in begin


def test_deep_link_autostart_is_still_guarded_by_the_scenario_param() -> None:
    src = APP.read_text(encoding="utf-8")
    assert "if (params.scenario) {" in src
