"""Stock footage from Pexels (free API, no attribution required by the license).

Pexels free limits are 200 requests/hour and 20k/month. To stay well inside
them we search once per unique query and spread the results across shots,
instead of searching once per shot.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import WORK_DIR, Format, cfg, secret

log = logging.getLogger(__name__)
CACHE = WORK_DIR / "broll-cache"


@dataclass
class Clip:
    path: Path
    duration: float


class BrollLibrary:
    def __init__(self, fmt: Format):
        self.fmt = fmt
        self.key = secret("PEXELS_API_KEY", required=False)
        self._results: dict[str, list[dict]] = {}
        self._used: set[int] = set()
        if not self.key:
            log.warning("PEXELS_API_KEY not set — shots will use solid backgrounds")

    def _search(self, query: str) -> list[dict]:
        if query in self._results:
            return self._results[query]
        orientation = "portrait" if self.fmt.is_short else "landscape"
        try:
            r = requests.get("https://api.pexels.com/videos/search",
                             headers={"Authorization": self.key},
                             params={"query": query, "orientation": orientation, "size": "medium",
                                     "per_page": 15}, timeout=20)
            r.raise_for_status()
            vids = r.json().get("videos", [])
        except requests.RequestException as exc:
            log.warning("pexels search %r failed: %s", query, exc)
            vids = []
        self._results[query] = vids
        return vids

    def _best_file(self, video: dict) -> dict | None:
        target = self.fmt.width
        files = [f for f in video.get("video_files", [])
                 if f.get("file_type") == "video/mp4" and f.get("width") and f.get("height")]
        if self.fmt.is_short:
            files = [f for f in files if f["height"] > f["width"]]
        else:
            files = [f for f in files if f["width"] >= f["height"]]
        if not files:
            return None
        # smallest file that is still >= target width; else the largest available
        big_enough = sorted((f for f in files if f["width"] >= target), key=lambda f: f["width"])
        return big_enough[0] if big_enough else max(files, key=lambda f: f["width"])

    def _download(self, video: dict) -> Clip | None:
        f = self._best_file(video)
        if not f:
            return None
        dest = CACHE / f"{video['id']}_{f['width']}.mp4"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with requests.get(f["link"], stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(dest, "wb") as fh:
                        for chunk in r.iter_content(1 << 16):
                            fh.write(chunk)
            except requests.RequestException as exc:
                log.warning("download %s failed: %s", video["id"], exc)
                dest.unlink(missing_ok=True)
                return None
        return Clip(dest, float(video.get("duration") or 8))

    def clip_for(self, query: str, min_seconds: float) -> Clip | None:
        """Return an unused clip for the query, falling back to a generic query."""
        if not self.key:
            return None
        for q in (query, cfg()["render"]["fallback_broll_query"]):
            if not q:
                continue
            for video in self._search(q):
                if video["id"] in self._used or (video.get("duration") or 0) < min(min_seconds, 4):
                    continue
                clip = self._download(video)
                if clip:
                    self._used.add(video["id"])
                    return clip
        # library exhausted: allow reuse rather than failing the render
        for q in (query, cfg()["render"]["fallback_broll_query"]):
            for video in self._search(q or ""):
                clip = self._download(video)
                if clip:
                    return clip
        return None
