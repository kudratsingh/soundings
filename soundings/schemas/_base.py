"""Shared base model, constrained scalar types, and the file-input helper."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"

# Bounding box of the simplified Port of Los Angeles map (SPEC-SIM section 1).
LAT_MIN, LAT_MAX = 33.70, 33.78
LON_MIN, LON_MAX = -118.30, -118.22

Lat = Annotated[float, Field(ge=LAT_MIN, le=LAT_MAX)]
Lon = Annotated[float, Field(ge=LON_MIN, le=LON_MAX)]
Probability = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegInt = Annotated[int, Field(ge=0)]
PositiveFloat = Annotated[float, Field(gt=0.0)]
NonNegFloat = Annotated[float, Field(ge=0.0)]

HarborId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
"""Zone, asset, sensor node and basin ids."""

RefId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
"""Plan ids and modem ids (for example ``P-2026-1017-003``, ``MOD-4471``)."""


class StrictModel(BaseModel):
    """Every Soundings model: no extra keys, immutable, strict types, finite floats.

    Enum fields stay lax (see ``enums._VocabEnum``) so a string from a file becomes a
    member; nothing else coerces (``True`` never becomes ``1``). Sequence fields are
    tuples; ``from_file_data`` turns YAML/GeoJSON lists into tuples before validation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


def from_file_data(obj: Any) -> Any:
    """Recursively turn lists into tuples so file data validates against tuple fields."""
    if isinstance(obj, dict):
        return {k: from_file_data(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return tuple(from_file_data(v) for v in obj)
    return obj
