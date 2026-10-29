"""Trend discovery from free, key-less sources.

Sources:
  * Google Trends daily trending searches RSS (geo=IN)
  * Google News RSS search for each niche query (India edition)
  * Reddit top-of-day JSON for niche subreddits

Each source is best-effort. A failing source is logged and skipped, so one
outage never blocks the daily script.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote_plus

import feedparser
import requests

from .config import cfg

log = logging.getLogger(__name__)
UA = {"User-Agent": "yt-autopilot/1.0 (trend research; contact via repo)"}
TIMEOUT = 15


@dataclass(frozen=True)
class Candidate:
    title: str
    source: str
    url: str
    signal: str = ""      # e.g. "200K+ searches", "1.2k upvotes"

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "source": self.source, "url": self.url, "signal": self.signal}


def _parse(url: str):
    feed = feedparser.parse(url, request_headers=UA)
    if not feed.entries and feed.get("bozo"):
        raise RuntimeError(f"feed unavailable: {feed.get('bozo_exception')}")
    return feed


def _google_trends(geo: str) -> list[Candidate]:
    feed = _parse(f"https://trends.google.com/trending/rss?geo={geo}")
    out = []
    for e in feed.entries:
        traffic = e.get("ht_approx_traffic", "")
        news_url = e.get("ht_news_item_url") or e.get("link", "")
        news_title = e.get("ht_news_item_title", "")
        title = f"{e.title} — {news_title}" if news_title else e.title
        out.append(Candidate(title, "google_trends", news_url, f"{traffic} searches" if traffic else ""))
    return out


def _google_news(query: str) -> list[Candidate]:
    url = f"https://news.google.com/rss/search?q={quote_plus(query + ' when:3d')}&hl=en-IN&gl=IN&ceid=IN:en"
    feed = _parse(url)
    return [Candidate(e.title, "google_news", e.link, query) for e in feed.entries[:8]]


def _reddit(sub: str) -> list[Candidate]:
    r = requests.get(f"https://www.reddit.com/r/{sub}/top.json?t=day&limit=10", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for child in r.json().get("data", {}).get("children", []):
        d = child["data"]
        if d.get("stickied") or d.get("over_18"):
            continue
        out.append(Candidate(d["title"], f"reddit/r/{sub}", "https://www.reddit.com" + d["permalink"],
                             f"{d.get('ups', 0)} upvotes"))
    return out


def gather() -> list[Candidate]:
    t = cfg()["trends"]
    jobs = [("google_trends", lambda: _google_trends(t["google_trends_geo"]))]
    jobs += [(f"news:{q}", (lambda q=q: _google_news(q))) for q in t["news_queries"]]
    jobs += [(f"reddit:{s}", (lambda s=s: _reddit(s))) for s in t["reddit_subs"]]

    seen: set[str] = set()
    out: list[Candidate] = []
    for name, job in jobs:
        try:
            items = job()
        except Exception as exc:  # noqa: BLE001 - best-effort source
            log.warning("trend source %s failed: %s", name, exc)
            continue
        for c in items:
            key = c.title.lower()[:80]
            if key not in seen:
                seen.add(key)
                out.append(c)
    log.info("gathered %d trend candidates", len(out))
    return out[: int(t["max_candidates"])]
