"""``config/ui.yaml``: display settings for the watch-officer screen (WO-060)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from soundings.schemas._base import CONFIG_DIR

UI_CONFIG_PATH = CONFIG_DIR / "ui.yaml"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TileLayer(_Cfg):
    url: str
    attribution: str


class Tiles(_Cfg):
    base: TileLayer
    seamarks: TileLayer
    map_attribution: str


class TilePolicy(_Cfg):
    verified_on: str
    osm_policy_url: str
    osm_summary: str
    openseamap_summary: str
    offline_note: str


class KindColors(_Cfg):
    fact: str
    assumption: str
    derived: str


class UiConfig(_Cfg):
    default_speed: float = Field(ge=0.0)
    awaiting_approval_speed: float = Field(gt=0.0)
    max_live_runs: int = Field(ge=1)
    tiles: Tiles
    tile_policy: TilePolicy
    colors: KindColors


def load_ui_config(path: Path | str = UI_CONFIG_PATH) -> UiConfig:
    """Parse and validate ``config/ui.yaml``."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return UiConfig.model_validate(data)


@lru_cache(maxsize=1)
def ui_config() -> UiConfig:
    """The committed ``config/ui.yaml`` (cached)."""
    return load_ui_config()
