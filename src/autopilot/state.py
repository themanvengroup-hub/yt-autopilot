"""Episode store.

Each episode is a folder under content/<id>/ with an episode.json file that is
committed back to the repo by the workflows. Git history doubles as the audit
log, and no database is needed. Binary media is never committed: voice
recordings stay on Telegram (we keep the file_id) and renders go to YouTube.

Lifecycle:
    scripted ─(voice received)→ recorded ─(render+upload)→ uploaded ─(/publish)→ scheduled|published
                                    └──────────(error)──────→ failed (retryable)
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .config import CONTENT_DIR, STATE_DIR, cfg

STATUSES = ("scripted", "recorded", "uploaded", "scheduled", "published", "failed", "skipped")


def now_local() -> datetime:
    return datetime.now(ZoneInfo(cfg()["channel"]["timezone"]))


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "episode"


@dataclass
class Episode:
    id: str
    ref: str                        # short code used in Telegram, e.g. S1029 / L1029b
    kind: str                       # short | long
    status: str
    created_at: str
    script: dict[str, Any]
    sources: list[dict[str, str]] = field(default_factory=list)
    telegram_message_ids: list[int] = field(default_factory=list)
    voice_file_id: str | None = None
    youtube_id: str | None = None
    publish_at: str | None = None
    error: str | None = None
    attempts: int = 0
    history: list[str] = field(default_factory=list)

    # ── persistence ────────────────────────────────────────────
    @property
    def path(self):
        return CONTENT_DIR / self.id / "episode.json"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def transition(self, status: str, note: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(status)
        self.history.append(f"{now_local().isoformat(timespec='seconds')} {self.status}→{status} {note}".strip())
        self.status = status
        self.save()

    @classmethod
    def load(cls, episode_id: str) -> "Episode":
        data = json.loads((CONTENT_DIR / episode_id / "episode.json").read_text(encoding="utf-8"))
        return cls(**data)

    @classmethod
    def create(cls, kind: str, script: dict[str, Any], sources: list[dict[str, str]]) -> "Episode":
        ts = now_local()
        base = f"{ts:%Y%m%d}-{kind}-{slugify(script.get('title', 'episode'))}"
        episode_id, n = base, 2
        while (CONTENT_DIR / episode_id).exists():
            episode_id, n = f"{base}-{n}", n + 1
        ref = f"{kind[0].upper()}{ts:%m%d}"
        taken = {e.ref for e in all_episodes()}
        suffix = iter("bcdefghijklmnopqrstuvwxyz")
        candidate = ref
        while candidate in taken:
            candidate = ref + next(suffix)
        ep = cls(id=episode_id, ref=candidate, kind=kind, status="scripted",
                 created_at=ts.isoformat(timespec="seconds"), script=script, sources=sources)
        ep.save()
        return ep


def all_episodes() -> Iterator[Episode]:
    if not CONTENT_DIR.exists():
        return
    for p in sorted(CONTENT_DIR.glob("*/episode.json")):
        yield Episode(**json.loads(p.read_text(encoding="utf-8")))


def episodes_with(status: str) -> list[Episode]:
    return [e for e in all_episodes() if e.status == status]


def find_by_message(message_id: int) -> Episode | None:
    for e in all_episodes():
        if message_id in e.telegram_message_ids:
            return e
    return None


def find_by_ref(ref: str) -> Episode | None:
    ref = ref.strip().lower()
    for e in all_episodes():
        if ref in (e.ref.lower(), e.id.lower(), (e.youtube_id or "").lower()):
            return e
    return None


def recent_titles(limit: int = 60) -> list[str]:
    eps = sorted(all_episodes(), key=lambda e: e.created_at, reverse=True)
    return [e.script.get("title", "") for e in eps[:limit]]


# ── small key/value state (telegram offset, insights) ─────────
def read_state(name: str, default: Any = None) -> Any:
    p = STATE_DIR / f"{name}.json"
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_state(name: str, value: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{name}.json").write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
