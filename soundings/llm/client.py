"""The single gate for model calls: mode switch plus a content-addressed fixture store.

Binding rules (CLAUDE.md):

* Section 2.3: no paid runs without the owner's explicit approval. The default mode is
  ``replay``; CI pins ``MODEL_MODE=replay`` and sets ``SOUNDINGS_CI``, under which a
  replay miss raises instead of silently falling back. Phase 0 makes no live calls at
  all: ``record`` and ``live`` raise ``NotImplementedError``.
* Section 2.4: determinism. Every call is keyed by
  ``sha256(role + canonical_json(inputs))`` and replayed from
  ``tests/fixtures/model_calls/<role>/<key>.json``. Floats are rounded to 6 dp before
  hashing so float noise cannot change a key.

The model name comes only from the ``MODEL_NAME`` environment variable. The vendor SDK
is imported lazily, inside the live path, never at module import time.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from soundings.canonical import FLOAT_DECIMALS, _normalize, canonical_json

log = logging.getLogger(__name__)

__all__ = [
    "FLOAT_DECIMALS",
    "FixtureStore",
    "ModelClient",
    "ModelMode",
    "ModelOutput",
    "ReplayMiss",
    "canonical_json",
    "fixture_key",
    "in_ci",
    "model_name_from_env",
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "model_calls"
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

ModelOutput = dict[str, Any]


class ModelMode(enum.StrEnum):
    REPLAY = "replay"
    RECORD = "record"
    LIVE = "live"

    @classmethod
    def from_env(cls) -> ModelMode:
        """Read ``MODEL_MODE`` (default ``replay``). An unknown value raises ``ValueError``."""
        raw = os.environ.get("MODEL_MODE", cls.REPLAY.value).strip().lower()
        try:
            return cls(raw)
        except ValueError:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(f"MODEL_MODE={raw!r} is not one of: {allowed}") from None


def model_name_from_env() -> str | None:
    """The model name, from ``MODEL_NAME`` only. ``None`` when unset."""
    name = os.environ.get("MODEL_NAME", "").strip()
    return name or None


def in_ci() -> bool:
    """True when ``SOUNDINGS_CI`` is set to anything other than empty or ``0``."""
    return os.environ.get("SOUNDINGS_CI", "").strip() not in ("", "0")


class ReplayMiss(LookupError):
    """No recorded fixture for this (role, inputs) pair while in replay mode."""

    def __init__(self, role: str, key: str) -> None:
        super().__init__(f"replay miss: role={role} key={key}")
        self.role = role
        self.key = key


def fixture_key(role: str, inputs: Any) -> str:
    """``sha256(role + canonical_json(inputs))`` as lowercase hex."""
    return hashlib.sha256((role + canonical_json(inputs)).encode("utf-8")).hexdigest()


def _check_role(role: str) -> None:
    if not _ROLE_RE.match(role):
        raise ValueError(f"invalid role name {role!r}")


class FixtureStore:
    """Recorded model calls at ``<root>/<role>/<key>.json``."""

    def __init__(self, root: Path = DEFAULT_FIXTURE_ROOT) -> None:
        self.root = root

    def path(self, role: str, key: str) -> Path:
        _check_role(role)
        return self.root / role / f"{key}.json"

    def load(self, role: str, inputs: Any) -> ModelOutput | None:
        """Return the recorded output for (role, inputs), or ``None`` if absent."""
        p = self.path(role, fixture_key(role, inputs))
        if not p.is_file():
            return None
        record = json.loads(p.read_text(encoding="utf-8"))
        output = record["output"]
        if not isinstance(output, dict):
            raise ValueError(f"fixture {p} has a non-object output")
        return output

    def save(self, role: str, inputs: Any, output: ModelOutput) -> Path:
        """Write a fixture atomically (via a ``.tmp`` sibling) and return its path."""
        key = fixture_key(role, inputs)
        p = self.path(role, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        record = {"role": role, "key": key, "inputs": _normalize(inputs), "output": output}
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(record, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(p)
        return p


class ModelClient:
    """Mode-aware model gateway. Callers supply a deterministic fallback for ``None``."""

    def __init__(
        self,
        mode: ModelMode | None = None,
        store: FixtureStore | None = None,
        model_name: str | None = None,
    ) -> None:
        self.mode = mode if mode is not None else ModelMode.from_env()
        self.store = store if store is not None else FixtureStore()
        self.model_name = model_name if model_name is not None else model_name_from_env()

    def call(self, role: str, inputs: Any) -> ModelOutput | None:
        """Return the model output for (role, inputs).

        Replay: the recorded fixture; on a miss, raise ``ReplayMiss`` in CI, otherwise
        log a warning and return ``None`` so the caller uses its fallback.
        """
        _check_role(role)
        if self.mode is not ModelMode.REPLAY:
            raise NotImplementedError("no live calls in Phase 0")
        output = self.store.load(role, inputs)
        if output is not None:
            return output
        key = fixture_key(role, inputs)
        if in_ci():
            raise ReplayMiss(role, key)
        log.warning("replay miss role=%s key=%s; caller falls back", role, key)
        return None
