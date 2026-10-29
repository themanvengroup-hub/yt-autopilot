"""YouTube Data API v3 + Analytics API.

Auth uses a long-lived OAuth refresh token (created once with
scripts/get_refresh_token.py) stored as a GitHub secret.

IMPORTANT: until your Google Cloud project passes the YouTube API compliance
audit, Google forces every API upload to private. The pipeline handles this by
uploading as private and sending you the Studio link to publish manually. Set
youtube.api_audited: true once the audit is approved to enable /publish
scheduling from Telegram.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .config import cfg, secret

log = logging.getLogger(__name__)
SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
RETRYABLE = {500, 502, 503, 504}


def _creds() -> Credentials:
    return Credentials(
        token=None,
        refresh_token=secret("YT_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=secret("YT_CLIENT_ID"),
        client_secret=secret("YT_CLIENT_SECRET"),
        scopes=SCOPES,
    )


def _yt():
    return build("youtube", "v3", credentials=_creds(), cache_discovery=False)


def _sanitize(text: str, limit: int) -> str:
    # YouTube rejects < and > in titles/descriptions
    return text.replace("<", "‹").replace(">", "›").strip()[:limit]


def build_description(script: dict[str, Any], chapters: list[tuple[float, str]]) -> str:
    desc = script.get("description", "").strip()
    if chapters:
        lines = [f"{int(t // 60):02d}:{int(t % 60):02d} {title}" for t, title in chapters]
        desc += "\n\nChapters:\n" + "\n".join(lines)
    desc += "\n\nThis video is for education only — not financial advice."
    return _sanitize(desc, 4900)


def upload(video: Path, *, title: str, description: str, tags: list[str],
           publish_at: datetime | None = None) -> str:
    y = cfg()["youtube"]
    status: dict[str, Any] = {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": bool(y["made_for_kids"]),
        "containsSyntheticMedia": False,  # real voice + real stock footage
    }
    if publish_at and y["api_audited"]:
        status["publishAt"] = publish_at.isoformat()
    all_tags = list(dict.fromkeys([*tags, *y["default_tags"]]))
    # tag list total must stay under 500 chars
    while sum(len(t) + 2 for t in all_tags) > 480:
        all_tags.pop()
    body = {
        "snippet": {"title": _sanitize(title, 100), "description": description, "tags": all_tags,
                    "categoryId": y["category_id"], "defaultLanguage": "en", "defaultAudioLanguage": "en-IN"},
        "status": status,
    }
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = _yt().videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=True)

    response, retry = None, 0
    while response is None:
        try:
            progress, response = request.next_chunk()
            if progress:
                log.info("upload %d%%", int(progress.progress() * 100))
        except HttpError as exc:
            if exc.resp.status not in RETRYABLE or retry >= 6:
                raise
            retry += 1
            sleep = min(2 ** retry + random.random(), 60)
            log.warning("upload chunk failed (%s); retry %d in %.0fs", exc.resp.status, retry, sleep)
            time.sleep(sleep)
    log.info("uploaded video id %s", response["id"])
    return response["id"]


def set_thumbnail(video_id: str, thumb: Path) -> None:
    """Requires a phone-verified channel; failure is non-fatal."""
    try:
        _yt().thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumb), mimetype="image/jpeg")).execute()
    except HttpError as exc:
        log.warning("thumbnail upload failed (verify your channel by phone to enable): %s", exc)


def schedule(video_id: str, publish_at: datetime | None) -> None:
    """publish_at=None → public now; otherwise scheduled. Needs an audited API project."""
    status: dict[str, Any] = {"selfDeclaredMadeForKids": bool(cfg()["youtube"]["made_for_kids"])}
    if publish_at:
        status.update(privacyStatus="private", publishAt=publish_at.isoformat())
    else:
        status["privacyStatus"] = "public"
    _yt().videos().update(part="status", body={"id": video_id, "status": status}).execute()


# ── analytics ────────────────────────────────────────────────
def channel_stats() -> dict[str, int]:
    items = _yt().channels().list(part="statistics", mine=True).execute().get("items", [])
    s = items[0]["statistics"] if items else {}
    return {"subscribers": int(s.get("subscriberCount", 0)), "views": int(s.get("viewCount", 0)),
            "videos": int(s.get("videoCount", 0))}


def video_performance(days: int = 28) -> list[dict[str, Any]]:
    ya = build("youtubeAnalytics", "v2", credentials=_creds(), cache_discovery=False)
    end, start = date.today(), date.today() - timedelta(days=days)
    rep = ya.reports().query(
        ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
        metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained",
        dimensions="video", sort="-views", maxResults=50,
    ).execute()
    cols = [h["name"] for h in rep.get("columnHeaders", [])]
    return [dict(zip(cols, row)) for row in rep.get("rows", [])]


def longform_watch_hours_365() -> float | None:
    """YPP counts public long-form watch hours only (Shorts excluded)."""
    ya = build("youtubeAnalytics", "v2", credentials=_creds(), cache_discovery=False)
    end, start = date.today(), date.today() - timedelta(days=365)
    try:
        rep = ya.reports().query(ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
                                 metrics="estimatedMinutesWatched", dimensions="creatorContentType").execute()
        minutes = sum(r[1] for r in rep.get("rows", []) if r[0] in ("videoOnDemand", "VIDEO_ON_DEMAND"))
        return round(minutes / 60, 1)
    except HttpError as exc:
        log.warning("content-type breakdown unavailable: %s", exc)
        return None


def video_titles(ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(ids), 50):
        res = _yt().videos().list(part="snippet", id=",".join(ids[i:i + 50])).execute()
        out.update({v["id"]: v["snippet"]["title"] for v in res.get("items", [])})
    return out
