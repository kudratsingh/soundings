"""Test session defaults: replay mode only, never a hosted model call (CLAUDE.md 2.3)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ["MODEL_MODE"] = "replay"


@pytest.fixture(autouse=True, scope="session")
def _force_replay_mode() -> Iterator[None]:
    mp = pytest.MonkeyPatch()
    mp.setenv("MODEL_MODE", "replay")
    yield
    mp.undo()
