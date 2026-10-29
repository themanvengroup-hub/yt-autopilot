"""B-roll footage from three sources, tried in this order for every shot:

1. Your own library: assets/broll/<topic>/*.mp4 (e.g. assets/broll/money/notes.mp4).
   A folder is used when its name appears in the shot's search query. Put
   CreateStudio animations or hand-picked clips here to give the channel a recurring look.
2. Pixabay video API (PIXABAY_API_KEY): free, key issued instantly, no attribution required.
3. Pexels video API (PEXELS_API_KEY): free, but new keys can be restricted for a while.

The stock providers are searched once per unique query and the results are
spread across shots, which keeps usage far below the free rate limits
(Pixabay 100 req/min, Pexels 200 req/hour).
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path

import requests

from . import media
from .config import ASSETS_DIR, WORK_DIR, Format, cfg, secret

log = logging.getLogger(__name__)
CACHE = WORK_DIR / "broll-cache"
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


@dataclass
class Clip:
    path: Path
    duration: float


@dataclass
class Candidate:
    key: str          # unique id across providers, e.g. "pixabay:12345"
    url: str
    width: int
    height: int
    duration: float


# ── providers ────────────────────────────────────────────────
def _pick_size(options: list[tuple[str, int, int]], fmt: Format) -> tuple[str, int, int] | None:
    """Smallest rendition that is still at least the target width; else the largest."""
    options = [o for o in options if o[0] and o[1] and o[2]]
    if not options:
        return None
    target = fmt.width if not fmt.is_short else fmt.height  # landscape sources get cropped for Shorts
    big = sorted((o for o in options if max(o[1], o[2]) >= target), key=lambda o: o[1] * o[2])
    return big[0] if big else max(options, key=lambda o: o[1] * o[2])


def _pixabay(query: str, fmt: Format, key: str) -> list[Candidate]:
    r = requests.get("https://pixabay.com/api/videos/",
                     params={"key": key, "q": query[:100], "per_page": 20, "safesearch": "true",
                             "video_type": "film"}, timeout=20)
    r.raise_for_status()
    out = []
    for hit in r.json().get("hits", []):
        renditions = [(v.get("url", ""), v.get("width", 0), v.get("height", 0))
                      for v in (hit.get("videos") or {}).values()]
        best = _pick_size(renditions, fmt)
        if best:
            out.append(Candidate(f"pixabay:{hit['id']}", best[0], best[1], best[2], float(hit.get("duration") or 8)))
    # Pixabay has no orientation filter; prefer portrait for Shorts when available
    if fmt.is_short:
        out.sort(key=lambda c: c.height <= c.width)
    return out


def _pexels(query: str, fmt: Format, key: str) -> list[Candidate]:
    r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": key},
                     params={"query": query, "orientation": "portrait" if fmt.is_short else "landscape",
                             "size": "medium", "per_page": 15}, timeout=20)
    r.raise_for_status()
    out = []
    for v in r.json().get("videos", []):
        renditions = [(f.get("link", ""), f.get("width", 0), f.get("height", 0))
                      for f in v.get("video_files", []) if f.get("file_type") == "video/mp4"]
        best = _pick_size(renditions, fmt)
        if best:
            out.append(Candidate(f"pexels:{v['id']}", best[0], best[1], best[2], float(v.get("duration") or 8)))
    return out


class BrollLibrary:
    def __init__(self, fmt: Format):
        self.fmt = fmt
        self.providers = []
        if key := secret("PIXABAY_API_KEY", required=False):
            self.providers.append(("pixabay", lambda q, k=key: _pixabay(q, fmt, k)))
        if key := secret("PEXELS_API_KEY", required=False):
            self.providers.append(("pexels", lambda q, k=key: _pexels(q, fmt, k)))
        self.local = self._scan_local()
        self._results: dict[str, list[Candidate]] = {}
        self._used: set[str] = set()
        if not self.providers and not self.local:
            log.warning("no B-roll source configured (PIXABAY_API_KEY / PEXELS_API_KEY / assets/broll) "
                        "— shots will use solid backgrounds")

    # ── local library ────────────────────────────────────────
    @staticmethod
    def _scan_local() -> dict[str, list[Path]]:
        root = ASSETS_DIR / "broll"
        lib: dict[str, list[Path]] = {}
        if root.exists():
            for p in root.rglob("*"):
                if p.suffix.lower() in VIDEO_EXT:
                    lib.setdefault(p.parent.name.lower(), []).append(p)
        if lib:
            log.info("local B-roll folders: %s", ", ".join(f"{k}({len(v)})" for k, v in lib.items()))
        return lib

    def _local_clip(self, query: str) -> Clip | None:
        words = set(re.findall(r"[a-z]+", query.lower()))
        for folder, paths in self.local.items():
            if folder in words or folder.rstrip("s") in words:
                fresh = [p for p in paths if f"local:{p}" not in self._used] or paths
                p = random.choice(fresh)
                self._used.add(f"local:{p}")
                try:
                    return Clip(p, media.duration(p))
                except Exception:  # noqa: BLE001 - unreadable file, skip it
                    log.warning("cannot read local clip %s", p)
        return None

    # ── stock providers ──────────────────────────────────────
    def _search(self, query: str) -> list[Candidate]:
        if query in self._results:
            return self._results[query]
        results: list[Candidate] = []
        for name, search in self.providers:
            try:
                results += search(query)
            except requests.RequestException as exc:
                log.warning("%s search %r failed: %s", name, query, exc)
        self._results[query] = results
        return results

    def _download(self, c: Candidate) -> Clip | None:
        dest = CACHE / f"{c.key.replace(':', '_')}_{c.width}.mp4"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with requests.get(c.url, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(dest, "wb") as fh:
                        for chunk in r.iter_content(1 << 16):
                            fh.write(chunk)
            except requests.RequestException as exc:
                log.warning("download %s failed: %s", c.key, exc)
                dest.unlink(missing_ok=True)
                return None
        return Clip(dest, c.duration)

    def clip_for(self, query: str, min_seconds: float) -> Clip | None:
        """Own library first, then an unused stock clip, then any stock clip."""
        if clip := self._local_clip(query):
            return clip
        if not self.providers:
            return None
        queries = [q for q in (query, cfg()["render"]["fallback_broll_query"]) if q]
        for q in queries:
            for c in self._search(q):
                if c.key in self._used or c.duration < min(min_seconds, 4):
                    continue
                if clip := self._download(c):
                    self._used.add(c.key)
                    return clip
        for q in queries:  # library exhausted: allow reuse rather than failing the render
            for c in self._search(q):
                if clip := self._download(c):
                    return clip
        return None
