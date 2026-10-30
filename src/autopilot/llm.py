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


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_gemini_models: list[str] = []
_gemini_idx = 0
_SKIP = ("lite", "image", "tts", "audio", "live", "embedding", "vision", "learnlm", "gemma", "robotics", "computer")


class Overloaded(LLMError):
    """Model busy (503/429/timeout): worth switching to another model."""


def _model_rank(name: str) -> tuple:
    """Higher is better: stable > preview/exp, flash > pro (free-tier quota), newer version first."""
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    version = float(m.group(1)) if m else 0.0
    stable = not any(t in name for t in ("preview", "exp"))
    flash = "flash" in name
    return (stable, flash, version, "latest" in name)


def _gemini_model_list() -> list[str]:
    """Ranked models this key can use: the configured one first (if available), then the best Flash models.

    Google retires model names regularly and the free tier is sometimes overloaded,
    so the list is read from the API once per run and used for fallback.
    """
    global _gemini_models
    if _gemini_models:
        return _gemini_models
    wanted = cfg()["llm"]["gemini_model"]
    r = requests.get(f"{GEMINI_BASE}/models", headers={"x-goog-api-key": secret("GEMINI_API_KEY")},
                     params={"pageSize": 1000}, timeout=30)
    r.raise_for_status()
    names = [m["name"].removeprefix("models/") for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    ranked = sorted((n for n in names if n.startswith("gemini") and not any(s in n for s in _SKIP)),
                    key=_model_rank, reverse=True)
    ranked = [wanted] + [n for n in ranked if n != wanted]  # configured model always tried first
    if not ranked:
        raise LLMError(f"no usable Gemini model for this key; available: {names[:20]}")
    _gemini_models = ranked[:6]
    log.info("Gemini model order: %s", ", ".join(_gemini_models))
    return _gemini_models


def _gemini(system: str, user: str) -> str:
    global _gemini_idx
    c = cfg()["llm"]
    models = _gemini_model_list()
    model = models[_gemini_idx % len(models)]
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": c["temperature"], "responseMimeType": "application/json"},
    }
    try:
        r = requests.post(f"{GEMINI_BASE}/models/{model}:generateContent",
                          headers={"x-goog-api-key": secret("GEMINI_API_KEY")}, json=body, timeout=150)
    except requests.Timeout as exc:
        _gemini_idx += 1
        raise Overloaded(f"{model} timed out") from exc
    if r.status_code == 404:
        hint = re.search(r"use models/([A-Za-z0-9.\-]+)", r.text)
        if hint and hint.group(1) not in models:
            models.insert(_gemini_idx % len(models) + 1, hint.group(1))  # Google named the replacement
            log.warning("%s retired; Google suggests %s", model, hint.group(1))
        models.pop(_gemini_idx % len(models)) if len(models) > 1 else None  # never retry a retired model
        raise Overloaded(f"{model} HTTP 404 (retired)")
    if r.status_code in (429, 500, 503, 504):
        _gemini_idx += 1  # next attempt uses the next model
        raise Overloaded(f"{model} HTTP {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400:
        raise LLMError(f"Gemini HTTP {r.status_code} ({model}): {r.text[:300]}")
    data = r.json()
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError) as exc:
        raise LLMError(f"unexpected Gemini response from {model}: {json.dumps(data)[:400]}") from exc


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


def complete_json(system: str, user: str, retries: int = 6) -> Any:
    provider = cfg()["llm"]["provider"]
    call = {"gemini": _gemini, "anthropic": _anthropic}[provider]
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return _extract_json(call(system, user))
        except (LLMError, json.JSONDecodeError, requests.RequestException) as exc:
            last = exc
            wait = 5 if isinstance(exc, Overloaded) else 10 * attempt
            log.warning("LLM attempt %d/%d failed (%s); retrying in %ds", attempt, retries, exc, wait)
            time.sleep(wait)
    raise LLMError(f"LLM failed after {retries} attempts: {last}")
