"""Telegram bot: your phone is the control panel for the channel.

What it does:
  * sends you each day's script, split into teleprompter-sized messages
  * receives your recording: reply to any script message with an audio file or voice note
  * sends you the private YouTube link to review
  * handles commands: /new, /skip, /redo, /publish, /status, /help

Security: updates from any chat other than TELEGRAM_CHAT_ID are ignored.
"""
from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

import requests

from .config import secret

log = logging.getLogger(__name__)
API = "https://api.telegram.org"
MAX_MSG = 3900  # Telegram hard limit is 4096; leave headroom for HTML tags


def _token() -> str:
    return secret("TELEGRAM_BOT_TOKEN")


def chat_id() -> int:
    return int(secret("TELEGRAM_CHAT_ID"))


def _call(method: str, **params: Any) -> Any:
    r = requests.post(f"{API}/bot{_token()}/{method}", json=params, timeout=60)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method} failed: {data.get('description')}")
    return data["result"]


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def send(text_html: str, reply_to: int | None = None) -> int:
    params: dict[str, Any] = {"chat_id": chat_id(), "text": text_html, "parse_mode": "HTML",
                              "disable_web_page_preview": True}
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return _call("sendMessage", **params)["message_id"]


def send_long(text_html_parts: list[str]) -> list[int]:
    """Send paragraphs packed into as few messages as possible. Returns message ids."""
    ids, buf = [], ""
    for part in text_html_parts:
        if len(buf) + len(part) + 2 > MAX_MSG and buf:
            ids.append(send(buf))
            buf = ""
        buf = f"{buf}\n\n{part}" if buf else part
    if buf:
        ids.append(send(buf))
    return ids


def send_document(path: Path, caption: str = "") -> int:
    with open(path, "rb") as fh:
        r = requests.post(f"{API}/bot{_token()}/sendDocument",
                          data={"chat_id": chat_id(), "caption": caption[:1000], "parse_mode": "HTML"},
                          files={"document": (path.name, fh)}, timeout=120)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram sendDocument failed: {data.get('description')}")
    return data["result"]["message_id"]


def get_updates(offset: int) -> list[dict[str, Any]]:
    return _call("getUpdates", offset=offset, timeout=0,
                 allowed_updates=["message"])


def download_file(file_id: str, dest: Path) -> Path:
    """Bot API download limit is 20 MB; a 10-min 128 kbps m4a is ~10 MB."""
    info = _call("getFile", file_id=file_id)
    url = f"{API}/file/bot{_token()}/{info['file_path']}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
    return dest


def audio_file_id(message: dict[str, Any]) -> tuple[str, int] | None:
    """Accept voice notes, audio files, or audio sent as a document (best quality)."""
    for key in ("audio", "voice"):
        if key in message:
            return message[key]["file_id"], message[key].get("file_size", 0)
    doc = message.get("document")
    if doc and (doc.get("mime_type", "").startswith("audio/") or
                doc.get("file_name", "").lower().endswith((".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac"))):
        return doc["file_id"], doc.get("file_size", 0)
    return None
