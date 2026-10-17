"""The investigator cannot peek.

Two structural claims, checked on the source tree (no runtime needed):

1. ``soundings/investigation`` and ``soundings/ladder`` import nothing from
   ``soundings.sim``: not at module level, not inside a function, not via
   ``from soundings import sim``, not via a relative import, and not by naming the module
   in a string (``importlib``). The transitive closure of what they import (lazy imports
   included) does not reach the simulator either, except through the two named CLI
   entry points below, which hand off to the ``soundings.dumps`` composition root.
2. Outside ``soundings/sim`` (which produces the truth), ``soundings/eval`` (which grades
   against it) and ``tests/``, no code reads a ground-truth field: no attribute, name,
   keyword, import alias or string key among ``TRUTH_NAMES``. The web client is scanned
   for the same identifiers.

The scanner is exercised on small synthetic sources first, so a pass here means the
scanner can see a violation, not just that it found none.

This runs in CI with the rest of the suite (the workflow runs ``uv run pytest``).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "soundings"
SIM = "soundings.sim"
GUARDED_PACKAGES: tuple[str, ...] = ("soundings.investigation", "soundings.ladder")

TRUTH_NAMES: frozenset[str] = frozenset(
    {
        "ground_truth",  # Scenario.ground_truth: the answer key of a scenario
        "contact_truth",  # SimRun.contact_truth: observed id -> truth
        "truth_paths",  # SimRun.truth_paths: true positions
        "gt_id",  # a ground-truth contact id
        "is_false_alarm",  # Detection label
        "detections",  # SimRun.detections: labelled readings
        "ContactTruth",
        "TruthPoint",
    }
)
"""Identifiers that name ground truth. ``ContactTruth.hypothesis`` is reachable only
through ``contact_truth`` / ``ContactTruth``, so it is covered by those two."""

TRUTH_READERS: tuple[str, ...] = ("soundings/sim/", "soundings/eval/")
"""Where truth may be read, besides ``tests/``: the simulator produces it, eval grades."""

ANSWER_KEY_ATTRS: frozenset[str] = frozenset({"expected"})
"""``Scenario.expected`` (the expected terminal state). Too generic a word to ban
repo-wide (local variables use it); banned as an attribute read inside the guarded
packages."""

CLI_HANDOFFS: dict[tuple[str, str], str] = {
    ("soundings.investigation.__main__", "soundings.dumps"): (
        "``python -m soundings.investigation`` entry point; imports the composition root "
        "inside main() at call time, never imported by investigation code"
    ),
    ("soundings.investigation.posterior", "soundings.dumps"): (
        "``python -m soundings.investigation.posterior`` entry point (posterior.main); "
        "lazy import inside main(), never on a scoring path"
    ),
}
"""Import edges allowed to lead from a guarded package towards the simulator, each a
developer CLI that runs a scenario and prints what the investigation makes of it."""

PORT_ONLY_ATTRS: dict[str, frozenset[str]] = {
    # The hail needs the source contact's registry entry for its modem id; only the
    # simulated hail may follow that link.
    "registry_contact_id": frozenset({"soundings/challenge/hail.py", "soundings/ports.py"}),
}


# --- the scanners -------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    where: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.where}:{self.line}: {self.what}"


def module_name(path: Path) -> str:
    rel = path.relative_to(REPO).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(module: str, is_package: bool, level: int, target: str | None) -> str:
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join([*base, target] if target else base)


def imported_modules(tree: ast.AST, module: str, is_package: bool) -> Iterator[tuple[int, str]]:
    """Every module an AST imports, anywhere in the file, as absolute dotted names.

    ``from a.b import c`` yields both ``a.b`` and ``a.b.c`` (``c`` may be a submodule).
    A string constant that looks like a ``soundings.`` module path is yielded too, so a
    dynamic ``importlib.import_module("soundings.sim...")`` is seen.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            base = (
                _resolve_relative(module, is_package, node.level, node.module)
                if node.level
                else (node.module or "")
            )
            yield node.lineno, base
            for alias in node.names:
                if alias.name != "*":
                    yield node.lineno, f"{base}.{alias.name}"
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.fullmatch(r"soundings(\.[A-Za-z_][A-Za-z0-9_]*)+", node.value)
        ):
            yield node.lineno, node.value


def is_sim(name: str) -> bool:
    return name == SIM or name.startswith(SIM + ".")


def in_guarded_package(mod: str) -> bool:
    """``soundings.investigation.x`` is guarded; ``soundings.investigation_wiring`` is not."""
    return any(mod == pkg or mod.startswith(pkg + ".") for pkg in GUARDED_PACKAGES)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
        # attribute docstrings: a bare string right after an assignment
        if hasattr(node, "body") and isinstance(node.body, list):
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    out.add(id(stmt.value))
    return out


def truth_reads(tree: ast.AST, names: frozenset[str] = TRUTH_NAMES) -> Iterator[tuple[int, str]]:
    """Every place a source reads one of ``names``.

    Attribute access, a bare name in load position, a keyword argument, an import alias,
    and a string constant equal to a name (a dict key, ``getattr`` or ``.get`` argument).
    A field definition (an annotated name in store position) is not a read; neither is a
    docstring that mentions a name.
    """
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in names:
            yield node.lineno, f".{node.attr}"
        elif isinstance(node, ast.Name) and node.id in names and isinstance(node.ctx, ast.Load):
            yield node.lineno, node.id
        elif isinstance(node, ast.keyword) and node.arg in names:
            yield node.lineno, f"{node.arg}="
        elif isinstance(node, ast.ImportFrom | ast.Import):
            for alias in node.names:
                if alias.name.rsplit(".", 1)[-1] in names:
                    yield node.lineno, f"import {alias.name}"
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in names
            and id(node) not in docstrings
        ):
            yield node.lineno, repr(node.value)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def package_files(prefix: str) -> list[Path]:
    root = REPO / Path(*prefix.split("."))
    return sorted(root.rglob("*.py"))


def all_modules() -> dict[str, Path]:
    return {module_name(p): p for p in sorted(PKG.rglob("*.py"))}


# --- the scanner can see a violation --------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "from soundings.sim.types import SimRun\n",
        "import soundings.sim.run as r\n",
        "from soundings import sim\n",
        "def f():\n    from soundings.sim import scenario\n    return scenario\n",
        "from ..sim import types\n",
        "import importlib\nm = importlib.import_module('soundings.sim.run')\n",
    ],
)
def test_import_scanner_sees_sim(source: str) -> None:
    tree = ast.parse(source)
    found = [n for _, n in imported_modules(tree, "soundings.investigation.x", False) if is_sim(n)]
    assert found, source


@pytest.mark.parametrize(
    "source",
    [
        "x = run.contact_truth[cid]\n",
        "x = run.truth_paths.get(gid, [])\n",
        "x = scenario.ground_truth\n",
        "x = data['ground_truth']\n",
        "x = getattr(s, 'ground_truth')\n",
        "make(ground_truth='uuv')\n",
        "from soundings.sim.types import ContactTruth\n",
        "y = [d for d in run.detections if not d.is_false_alarm]\n",
        "z = {'ground_truth': h}\n",
    ],
)
def test_truth_scanner_sees_a_read(source: str) -> None:
    assert list(truth_reads(ast.parse(source))), source


def test_truth_scanner_ignores_definitions_and_docstrings() -> None:
    source = (
        '"""Mentions ground_truth and contact_truth in prose."""\n'
        "class Scenario:\n"
        '    """The ground_truth field is the answer key."""\n'
        "    ground_truth: int\n"
        '    """contact_truth is never read here."""\n'
    )
    assert not list(truth_reads(ast.parse(source)))


# --- claim 1: the investigation and the ladder import nothing from the simulator --------------


def _direct_sim_imports() -> list[Hit]:
    hits: list[Hit] = []
    for package in GUARDED_PACKAGES:
        for path in package_files(package):
            mod = module_name(path)
            for line, name in imported_modules(_parse(path), mod, path.name == "__init__.py"):
                if is_sim(name):
                    hits.append(Hit(str(path.relative_to(REPO)), line, f"imports {name}"))
    return hits


def test_guarded_packages_exist() -> None:
    for package in GUARDED_PACKAGES:
        assert package_files(package), package
    assert in_guarded_package("soundings.investigation.probes")
    assert not in_guarded_package("soundings.investigation_wiring")


def test_investigation_and_ladder_import_nothing_from_sim() -> None:
    hits = _direct_sim_imports()
    assert not hits, "\n".join(map(str, hits))


def _import_graph() -> dict[str, set[str]]:
    modules = all_modules()
    graph: dict[str, set[str]] = {}
    for mod, path in modules.items():
        edges: set[str] = set()
        for _, name in imported_modules(_parse(path), mod, path.name == "__init__.py"):
            # keep the longest prefix that is a real module in the package
            parts = name.split(".")
            while parts and ".".join(parts) not in modules:
                parts.pop()
            if parts:
                edges.add(".".join(parts))
        graph[mod] = edges - {mod}
    return graph


def _path_to_sim(graph: dict[str, set[str]], start: str) -> list[str] | None:
    """A chain of imports from ``start`` to a ``soundings.sim`` module, or None."""
    seen = {start}
    stack: list[list[str]] = [[start]]
    while stack:
        chain = stack.pop()
        for nxt in sorted(graph.get(chain[-1], ())):
            if (chain[-1], nxt) in CLI_HANDOFFS or nxt in seen:
                continue
            if is_sim(nxt):
                return [*chain, nxt]
            seen.add(nxt)
            stack.append([*chain, nxt])
    return None


def test_no_transitive_path_from_investigation_or_ladder_to_sim() -> None:
    graph = _import_graph()
    chains = []
    for mod in sorted(graph):
        if in_guarded_package(mod):
            chain = _path_to_sim(graph, mod)
            if chain is not None:
                chains.append(" -> ".join(chain))
    assert not chains, "\n".join(chains)


def test_cli_handoffs_are_real_and_lazy() -> None:
    """Each allowlisted edge exists, and only inside a function (never at import time)."""
    modules = all_modules()
    for (src, dst), reason in CLI_HANDOFFS.items():
        assert reason
        tree = _parse(modules[src])
        top_level = {
            name
            for stmt in tree.body
            if isinstance(stmt, ast.Import | ast.ImportFrom)
            for _, name in imported_modules(ast.Module(body=[stmt], type_ignores=[]), src, False)
        }
        assert dst not in top_level, f"{src} imports {dst} at module level"
        everywhere = {n for _, n in imported_modules(tree, src, False)}
        assert dst in everywhere, f"stale allowlist entry {src} -> {dst}"


# --- claim 2: nobody outside sim and eval reads ground truth ------------------------------------


def _truth_reads_outside_readers() -> list[Hit]:
    hits: list[Hit] = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith(TRUTH_READERS):
            continue
        for line, what in truth_reads(_parse(path)):
            hits.append(Hit(rel, line, f"reads {what}"))
    return hits


def test_no_ground_truth_read_outside_sim_and_eval() -> None:
    hits = _truth_reads_outside_readers()
    assert not hits, "\n".join(map(str, hits))


def test_guarded_packages_never_read_the_answer_key() -> None:
    hits = []
    for package in GUARDED_PACKAGES:
        for path in package_files(package):
            for node in ast.walk(_parse(path)):
                if isinstance(node, ast.Attribute) and node.attr in ANSWER_KEY_ATTRS:
                    rel = path.relative_to(REPO).as_posix()
                    hits.append(Hit(rel, node.lineno, f"reads .{node.attr}"))
    assert not hits, "\n".join(map(str, hits))


def test_port_only_attributes_stay_with_their_reader() -> None:
    hits = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith(TRUTH_READERS):
            continue
        for attr, allowed in PORT_ONLY_ATTRS.items():
            if rel in allowed:
                continue
            for line, what in truth_reads(_parse(path), frozenset({attr})):
                hits.append(Hit(rel, line, f"reads {what}"))
    assert not hits, "\n".join(map(str, hits))


WEB_TRUTH = re.compile(r"\b(" + "|".join(sorted(TRUTH_NAMES)) + r")\b")


def test_web_client_names_no_ground_truth() -> None:
    src = REPO / "web" / "src"
    if not src.is_dir():
        pytest.skip("no web client in this checkout")
    hits = []
    for path in sorted(src.rglob("*")):
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if WEB_TRUTH.search(line):
                hits.append(f"{path.relative_to(REPO).as_posix()}:{i}")
    assert not hits, "\n".join(hits)
