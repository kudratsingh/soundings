"""Scenario families for ``--suite <family>`` (WO-051 part B1).

A family is a named, ordered list of scenario ids that share a story: the ordinary
Tuesday traffic, the night at the fuel berths, the vehicle that went quiet, the busy
working day, and the known-answer traps. The three catalog families together cover
S01..S08 exactly once (``tests/test_eval_families.py``).

An id in ``OPTIONAL_IDS`` (BT-01, owned by WO-080) is skipped while its file does not
exist; any other id that names no file is an error.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from soundings.sim.scenario import SCENARIO_ROOT

TRAPS_DIRNAME = "traps"

FAMILIES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "tuesday": ("S01", "S03", "S04", "S07", "S08"),
        "harbor_night": ("S05", "S06"),
        "lost_vehicle": ("S02",),
        "busy": ("BT-01",),
        "traps": ("TR-01", "TR-02", "TR-03", "TR-04"),
    }
)
CATALOG_FAMILIES: tuple[str, ...] = ("tuesday", "harbor_night", "lost_vehicle")
OPTIONAL_IDS: frozenset[str] = frozenset({"BT-01"})


class FamilyError(LookupError):
    """An unknown family, or a required scenario id with no file."""


def family_of(scenario_id: str) -> str | None:
    """The first family (registry order) that lists ``scenario_id``, else None."""
    for name, ids in FAMILIES.items():
        if scenario_id in ids:
            return name
    return None


def _locate(scenario_id: str, root: Path) -> Path | None:
    folder = root / TRAPS_DIRNAME if scenario_id.startswith("TR-") else root
    exact = folder / f"{scenario_id}.yaml"
    if exact.is_file():
        return exact
    found = sorted(folder.glob(f"{scenario_id}-*.yaml"))
    if len(found) > 1:
        raise FamilyError(f"scenario id {scenario_id!r} matches {len(found)} files")
    return found[0] if found else None


def family_paths(name: str, *, scenario_root: Path | None = None) -> list[Path]:
    """Scenario files of a family, sorted by path; optional ids that are absent are
    skipped. An unknown family or a missing required id raises ``FamilyError``."""
    if name not in FAMILIES:
        raise FamilyError(f"unknown family {name!r} (one of {', '.join(FAMILIES)})")
    root = SCENARIO_ROOT if scenario_root is None else Path(scenario_root)
    paths: list[Path] = []
    for sid in FAMILIES[name]:
        path = _locate(sid, root)
        if path is None:
            if sid in OPTIONAL_IDS:
                continue
            raise FamilyError(f"family {name!r}: no scenario file for {sid}")
        paths.append(path)
    return sorted(paths)
