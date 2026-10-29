"""Configuration and secrets loading."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTENT_DIR = ROOT / "content"
STATE_DIR = ROOT / "state"
ASSETS_DIR = ROOT / "assets"
WORK_DIR = ROOT / ".work"


@lru_cache(maxsize=1)
def cfg() -> dict[str, Any]:
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def secret(name: str, required: bool = True) -> str | None:
    value = os.environ.get(name)
    if required and not value:
        raise RuntimeError(
            f"Missing secret {name}. Add it under GitHub → Settings → Secrets → Actions "
            f"(or export it locally). See docs/SETUP.md."
        )
    return value


@dataclass(frozen=True)
class Format:
    key: str          # "short" | "long"
    width: int
    height: int
    shot_seconds: float

    @property
    def is_short(self) -> bool:
        return self.key == "short"


def video_format(kind: str) -> Format:
    r = cfg()["render"]
    if kind == "short":
        return Format("short", 1080, 1920, float(r["shorts_shot_seconds"]))
    if kind == "long":
        return Format("long", 1920, 1080, float(r["longform_shot_seconds"]))
    raise ValueError(f"unknown format {kind!r}")
