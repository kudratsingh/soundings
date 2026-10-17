"""WO-060: no panel, label or button in the UI or its backend uses a denylisted word."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGETS = ["web/src", "soundings/stream", "soundings/api_routes", "config/ui.yaml"]
SUFFIXES = {".py", ".ts", ".tsx", ".json", ".md", ".css", ".yaml", ".html"}


def _patterns() -> list[re.Pattern[str]]:
    lines = (REPO / "config" / "denylist.txt").read_text(encoding="utf-8").splitlines()
    return [re.compile(line, re.IGNORECASE) for line in lines if line.strip()]


def _files() -> list[Path]:
    out: list[Path] = []
    for target in TARGETS:
        path = REPO / target
        if path.is_file():
            out.append(path)
        else:
            out += [p for p in sorted(path.rglob("*")) if p.is_file() and p.suffix in SUFFIXES]
    out.append(REPO / "web" / "index.html")
    return out


def test_ui_vocabulary_is_clean() -> None:
    pats = _patterns()
    files = _files()
    assert any(f.suffix == ".tsx" for f in files)
    hits = [
        f"{f.relative_to(REPO)}:{n}: {line.strip()}"
        for f in files
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
        if any(p.search(line) for p in pats)
    ]
    assert hits == []
