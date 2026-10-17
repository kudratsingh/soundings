"""Scenario files under ``scenarios/`` and the resolver over WO-010's loader.

The catalog is the glob ``scenarios/S[0-9][0-9]-*.yaml`` (sorted by path), so
``scenarios/smoke.yaml`` and ``scenarios/live/`` are never part of it.
``resolve_scenario`` turns ``smoke``, a catalog id such as ``S04``, or a path into a
validated ``Scenario`` via ``soundings.schemas.scenario.load_scenario`` (which also
resolves every zone, asset and node id against the harbor).
"""

from __future__ import annotations

import re
from pathlib import Path

from soundings.schemas.harbor import Harbor
from soundings.schemas.scenario import Scenario, load_scenario

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = REPO_ROOT / "scenarios"
CATALOG_GLOB = "S[0-9][0-9]-*.yaml"
SMOKE_PATH = SCENARIO_ROOT / "smoke.yaml"
_CATALOG_ID = re.compile(r"^S\d{2}$")
_BT_ID = re.compile(r"^BT-\d{2}$")  # WO-080: generated busy-day files, outside the catalog


class UnknownScenarioError(LookupError):
    """``resolve_scenario`` got a reference that names no scenario file."""


def catalog_paths(root: Path | str = SCENARIO_ROOT) -> list[Path]:
    """Catalog scenario files, sorted by path."""
    return sorted(Path(root).glob(CATALOG_GLOB))


def scenario_path(ref: str | Path, root: Path | str = SCENARIO_ROOT) -> Path:
    """The file a reference names: ``smoke``, a catalog id (``S04``), ``BT-01``, or a path."""
    text = str(ref)
    base = Path(root)
    if text == "smoke":
        return base / "smoke.yaml"
    if _BT_ID.match(text):
        bt = base / f"{text}.yaml"
        if not bt.is_file():
            raise UnknownScenarioError(f"no file for {text!r}")
        return bt
    if _CATALOG_ID.match(text):
        matches = sorted(base.glob(f"{text}-*.yaml"))
        if len(matches) != 1:
            raise UnknownScenarioError(f"catalog id {text!r} matches {len(matches)} files")
        return matches[0]
    path = Path(text)
    if path.suffix in (".yaml", ".yml") and path.is_file():
        return path
    raise UnknownScenarioError(f"unknown scenario {text!r}")


def resolve_scenario(
    ref: str | Path, harbor: Harbor | None = None, root: Path | str = SCENARIO_ROOT
) -> Scenario:
    """Load and validate the scenario a reference names (see ``scenario_path``)."""
    return load_scenario(scenario_path(ref, root), harbor)
