"""This repository names no other project of the owner's.

The design notes describe the agent loop generically ("a state-machine agent design the
owner has used before"); no file, doc, deck input or commit message names or links
another repository. The pattern is assembled from pieces so this file does not match it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKIP_DIRS = frozenset({".git", "node_modules", ".venv"})

_TERMS = (
    "incident" + r"[- _]?" + "commander",
    "incident" + r"[- _]?" + "platform",
    "audit" + "-ws",
    "ar" + "xiv",
    "research" + r"[- ]" + "agent",
)
EXTERNAL = re.compile("|".join(_TERMS), re.IGNORECASE)


def _git(*args: str) -> str | None:
    if shutil.which("git") is None or not (REPO / ".git").exists():
        return None
    done = subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False
    )
    return done.stdout if done.returncode == 0 else None


def _repo_files() -> list[Path]:
    """Tracked and untracked-but-not-ignored files (what a commit could carry)."""
    listed = _git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed is not None:
        paths = [REPO / p for p in listed.split("\0") if p]
    else:
        paths = [p for p in REPO.rglob("*") if not SKIP_DIRS & set(p.relative_to(REPO).parts)]
    return sorted(p for p in paths if p.is_file() and not SKIP_DIRS & set(p.parts))


def test_pattern_sees_what_it_must() -> None:
    for sample in (
        "Incident" + " Commander",
        "incident" + "-platform",
        "audit" + "-ws",
        "ar" + "Xiv",
        "Research" + " Agent",
        "research" + "-agent",
    ):
        assert EXTERNAL.search(sample), sample


def test_no_file_names_another_repo() -> None:
    hits = []
    for path in _repo_files():
        text = path.read_bytes().decode("utf-8", errors="ignore")
        rel = path.relative_to(REPO).as_posix()
        if EXTERNAL.search(rel):
            hits.append(rel)
        for i, line in enumerate(text.splitlines(), start=1):
            if EXTERNAL.search(line):
                hits.append(f"{rel}:{i}")
    assert not hits, "\n".join(hits)


def test_no_commit_message_names_another_repo() -> None:
    log = _git("log", "--format=%H %B")
    if log is None:
        pytest.skip("no git history in this checkout")
    hits = [line for line in log.splitlines() if EXTERNAL.search(line)]
    assert not hits, "\n".join(hits)
