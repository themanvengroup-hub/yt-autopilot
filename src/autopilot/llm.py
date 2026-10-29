"""Minimal LLM client returning parsed JSON.

Two providers sit behind one function, so switching providers only means
editing config.yaml:
  * gemini    — free tier via Google AI Studio key (GEMINI_API_KEY)
  * anthropic — Claude via ANTHROPIC_API_KEY
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import requests

from .config import cfg, secret

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> Any:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise LLMError(f"no JSON in model output: {text[:200]!r}")
    return json.loads(text[start:])


def _gemini(system: str, user: str) -> str:
    c = cfg()["llm"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{c['gemini_model']}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": c["temperature"], "responseMimeType": "application/json"},
    }
    r = requests.post(url, params={"key": secret("GEMINI_API_KEY")}, json=body, timeout=120)
    if r.status_code == 429:
        raise LLMError("rate limited (429)")
    r.raise_for_status()
    data = r.json()
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError) as exc:
        raise LLMError(f"unexpected Gemini response: {json.dumps(data)[:400]}") from exc


def _anthropic(system: str, user: str) -> str:
    c = cfg()["llm"]
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": secret("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"},
        json={"model": c["anthropic_model"], "max_tokens": 8000, "temperature": c["temperature"],
              "system": system, "messages": [{"role": "user", "content": user}]},
        timeout=180,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")


def complete_json(system: str, user: str, retries: int = 3) -> Any:
    provider = cfg()["llm"]["provider"]
    call = {"gemini": _gemini, "anthropic": _anthropic}[provider]
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return _extract_json(call(system, user))
        except (LLMError, json.JSONDecodeError, requests.RequestException) as exc:
            last = exc
            wait = 10 * attempt
            log.warning("LLM attempt %d/%d failed (%s); retrying in %ds", attempt, retries, exc, wait)
            time.sleep(wait)
    raise LLMError(f"LLM failed after {retries} attempts: {last}")
