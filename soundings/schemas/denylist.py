"""The enforced vocabulary denylist, compiled from ``config/denylist.txt`` at import.

CLAUDE.md 2.1 and 2.5: nothing kinetic and nothing after the handoff may be named. Every
human-readable string in a schema is typed ``FreeText``, which runs ``assert_clean``.
A missing denylist file is a hard error at import time, never an empty list.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator

from soundings.schemas._base import CONFIG_DIR

DENYLIST_PATH: Path = CONFIG_DIR / "denylist.txt"


def _compile(path: Path) -> tuple[re.Pattern[str], ...]:
    lines = path.read_text(encoding="utf-8").splitlines()  # raises if the file is missing
    patterns = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    if not patterns:
        raise RuntimeError(f"{path} holds no patterns")
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


DENYLIST: tuple[re.Pattern[str], ...] = _compile(DENYLIST_PATH)


def find_denylisted(text: str) -> list[str]:
    """Every denylisted match in ``text``, pattern by pattern in file order."""
    found: list[str] = []
    for pattern in DENYLIST:
        found.extend(m.group(0) for m in pattern.finditer(text))
    return found


def assert_clean(text: str) -> str:
    """Return ``text`` unchanged, or raise ``ValueError`` if it holds a denylisted word."""
    hits = find_denylisted(text)
    if hits:
        raise ValueError(f"text contains {len(hits)} denylisted term(s) (CLAUDE.md 2.5)")
    return text


FreeText = Annotated[str, AfterValidator(assert_clean)]
"""Every human-readable string in a schema."""
