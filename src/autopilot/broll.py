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
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from . import media
from .config import ASSETS_DIR, WORK_DIR, Format, cfg, secret
from .state import read_state, write_state

log = logging.getLogger(__name__)
CACHE = WORK_DIR / "broll-cache"
DL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0 Safari/537.36 yt-autopilot",
    "Referer": "https://pixabay.com/",
}
_STOP = {"the", "and", "with", "close", "aerial", "slow", "motion", "cinematic", "shot", "view", "night", "timelapse", "footage"}
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
    tags: str = ""    # provider tags / slug words, used for relevance ranking


# ── providers ────────────────────────────────────────────────
def _pick_size(options: list[tuple[str, int, int]], fmt: Format) -> tuple[str, int, int] | None:
    """Smallest rendition that still fills the frame without upscaling; else the largest.

    A vertical Short cut from landscape footage keeps only a 9:16 slice, so it needs a
    source at least 1920 px tall (i.e. 4K) to stay sharp.
    """
    options = [o for o in options if o[0] and o[1] and o[2]]
    if not options:
        return None

    def fills(o: tuple[str, int, int]) -> bool:
        w, h = o[1], o[2]
        scale = max(fmt.width / w, fmt.height / h)   # cover-fit scale factor
        return scale <= 1.05

    good = sorted((o for o in options if fills(o)), key=lambda o: o[1] * o[2])
    return good[0] if good else max(options, key=lambda o: o[1] * o[2])


def _sharpness(c: "Candidate", fmt: Format) -> float:
    """>=1 means the clip covers the frame at native resolution or better (no upscaling)."""
    return 1 / max(fmt.width / c.width, fmt.height / c.height)


def _pixabay(query: str, fmt: Format, key: str) -> list[Candidate]:
    r = requests.get("https://pixabay.com/api/videos/",
                     params={"key": key, "q": query[:100], "per_page": 30, "safesearch": "true",
                             "video_type": "film", "order": "popular", "min_width": 1280}, timeout=20)
    r.raise_for_status()
    out = []
    for hit in r.json().get("hits", []):
        renditions = [(v.get("url", ""), v.get("width", 0), v.get("height", 0))
                      for v in (hit.get("videos") or {}).values()]
        best = _pick_size(renditions, fmt)
        if best:
            out.append(Candidate(f"pixabay:{hit['id']}", best[0], best[1], best[2], float(hit.get("duration") or 8),
                                 hit.get("tags", "")))
    # Pixabay has no orientation filter: rank portrait first for Shorts, then sharpness;
    # stable sort keeps Pixabay's popularity order as the tie-breaker (a decent quality proxy)
    out.sort(key=lambda c: (not (fmt.is_short and c.height > c.width), -min(_sharpness(c, fmt), 1.0)))
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
            out.append(Candidate(f"pexels:{v['id']}", best[0], best[1], best[2], float(v.get("duration") or 8),
                                 v.get("url", "").replace("-", " ")))
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
        self._history: list[str] = read_state("broll_used", [])
        self._used: set[str] = set(self._history)   # never repeat a clip from a recent video
        self._downloaded: list[Clip] = []
        self._reuse_idx = 0
        self._last_dl = 0.0
        self._rate_limited = 0
        self._blocked_until = 0.0
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
        words = {w for w in re.findall(r"[a-z]+", query.lower()) if len(w) > 2} - _STOP
        def score(c: Candidate) -> tuple:
            tags = c.tags.lower()
            rel = sum(1 for w in words if w in tags or w.rstrip("s") in tags) / max(len(words), 1)
            portrait = self.fmt.is_short and c.height > c.width
            return (-round(rel, 1), not portrait, -min(_sharpness(c, self.fmt), 1.0))
        results.sort(key=score)
        self._results[query] = results
        return results

    def _download(self, c: Candidate) -> Clip | None:
        """Polite download: browser-like headers, pacing between requests, and real back-off on
        429/5xx (honouring Retry-After). After repeated rate-limits a circuit breaker stops new
        downloads for this render instead of hammering the CDN (which only extends the ban)."""
        dest = CACHE / f"{c.key.replace(':', '_')}_{c.width}.mp4"
        if dest.exists() and dest.stat().st_size > 50_000:
            return Clip(dest, c.duration)
        if self._blocked_until > time.time():
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            wait = 1.2 - (time.time() - self._last_dl)          # ~1 request / 1.2 s
            if wait > 0:
                time.sleep(wait)
            self._last_dl = time.time()
            try:
                with requests.get(c.url, stream=True, timeout=120, headers=DL_HEADERS) as r:
                    if r.status_code in (429, 503):
                        retry = r.headers.get("Retry-After", "")
                        pause = float(retry) if retry.isdigit() else 15 * (attempt + 1)
                        self._rate_limited += 1
                        log.warning("CDN rate limit on %s (attempt %d); pausing %.0fs", c.key, attempt + 1, pause)
                        if self._rate_limited >= 6:
                            self._blocked_until = time.time() + 600
                            log.error("stock CDN keeps rate-limiting; no new downloads for this render")
                            return None
                        time.sleep(min(pause, 60))
                        continue
                    r.raise_for_status()
                    with open(dest, "wb") as fh:
                        for chunk in r.iter_content(1 << 16):
                            fh.write(chunk)
                self._rate_limited = max(self._rate_limited - 1, 0)
                self._downloaded.append(Clip(dest, c.duration))
                return self._downloaded[-1]
            except requests.RequestException as exc:
                log.warning("download %s failed: %s", c.key, exc)
                dest.unlink(missing_ok=True)
                time.sleep(3)
        dest.unlink(missing_ok=True)
        return None

    def _remember(self, key: str) -> None:
        self._used.add(key)
        if not key.startswith("local:"):
            self._history.append(key)
            write_state("broll_used", self._history[-600:])

    def clip_for(self, queries: list[str] | str, min_seconds: float) -> Clip | None:
        """Own library first, then the best unused stock clip across the query list
        (primary, alternates, channel fallback), then any clip rather than a blank frame."""
        if isinstance(queries, str):
            queries = [queries]
        queries = [q for q in queries if q]
        for q in queries:
            if clip := self._local_clip(q):
                return clip
        if not self.providers:
            return None
        ordered = queries + [cfg()["render"]["fallback_broll_query"]]
        tries = 0
        for q in ordered:
            for c in self._search(q):
                if c.key in self._used or c.duration < min(min_seconds + 1.0, 5):
                    continue
                if clip := self._download(c):
                    self._remember(c.key)
                    return clip
                tries += 1
                if tries >= 4 or self._blocked_until > time.time():
                    break
            if tries >= 4 or self._blocked_until > time.time():
                break
        # couldn't get a fresh clip: reuse one already downloaded for this video (never a blank frame)
        if self._downloaded:
            self._reuse_idx = (self._reuse_idx + 1) % len(self._downloaded)
            return self._downloaded[self._reuse_idx]
        return None
