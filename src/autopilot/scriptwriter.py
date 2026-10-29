"""Topic selection and script generation.

Each script is structured JSON rather than prose so that:
  * the renderer knows which B-roll to pull for each segment,
  * titles, descriptions, tags and chapters are generated together with the script,
  * facts_to_verify gives you a checklist to review before recording. This
    human review is what keeps the channel clear of YouTube's "inauthentic /
    mass-produced" policy and SEBI's finfluencer rules.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import llm
from .config import cfg
from .state import read_state, recent_titles
from .trends import Candidate

log = logging.getLogger(__name__)


def _system_prompt() -> str:
    ch = cfg()["channel"]
    rules = "\n".join(f"- {g}" for g in ch["guardrails"])
    return f"""You are the head writer of the YouTube channel "{ch['name']}".
Niche: {ch['niche']}
Audience: {ch['audience']}
Voice: {ch['voice_style']}

The creator records the voiceover in their own voice, so write for speaking aloud:
contractions, short sentences, no tongue-twisters, no lists of more than three numbers in a row.

Non-negotiable rules:
{rules}

Retention craft you must apply:
- The first sentence is the hook. It must open a curiosity gap or state a surprising,
  specific fact in under 12 words. Never start with "In this video", "Hey guys" or a greeting.
- Make stakes concrete with rupee figures and relatable comparisons (₹ per chai, per auto ride).
- Open loops: tease the answer, deliver it later.
- Every segment must earn the next one; cut anything that doesn't move the story.
- Titles: under 60 characters, curiosity + specificity, no clickbait the video doesn't pay off.
Return ONLY valid JSON matching the schema the user gives you."""


def _insights_block() -> str:
    insights = read_state("insights", {})
    if not insights.get("top"):
        return "No performance data yet (new channel)."
    lines = [f"- \"{v['title']}\": {v['views']} views, {v['avg_view_pct']}% avg viewed, +{v['subs']} subs"
             for v in insights["top"][:6]]
    worst = [f"- \"{v['title']}\": {v['avg_view_pct']}% avg viewed" for v in insights.get("bottom", [])[:3]]
    return ("Our best performers (do more like these):\n" + "\n".join(lines) +
            ("\nOur weakest (avoid these angles):\n" + "\n".join(worst) if worst else ""))


SHORT_SCHEMA = """{
  "title": "string, <=60 chars, include #shorts at the end",
  "hook_text": "string, <=6 words, bold on-screen text for the first 2 seconds",
  "segments": [ {"text": "1-2 spoken sentences", "broll": "2-4 word stock-footage search query, concrete & visual, in English"} ],
  "loop_line": "final sentence that flows back into the hook so viewers rewatch",
  "description": "2-3 sentences + 3 relevant hashtags",
  "tags": ["8-12 search tags"],
  "facts_to_verify": [ {"claim": "string", "source_url": "url from the candidates or 'common knowledge'"} ],
  "chosen_candidate": "the headline you picked",
  "why_it_will_work": "one sentence"
}"""

LONG_SCHEMA = """{
  "title": "string, <=60 chars",
  "thumbnail_text": "string, 2-4 punchy words for the thumbnail",
  "sections": [
    {"heading": "chapter name, <=5 words",
     "segments": [ {"text": "1-3 spoken sentences", "broll": "2-4 word stock-footage search query"} ]}
  ],
  "description": "3 short paragraphs: hook, what they'll learn, CTA to subscribe. Then 5 hashtags. Do NOT include timestamps.",
  "tags": ["10-15 search tags"],
  "pinned_comment": "a question that invites debate in the comments",
  "shorts_spinoffs": ["3 Short ideas cut from this video"],
  "facts_to_verify": [ {"claim": "string", "source_url": "url or 'common knowledge'"} ],
  "chosen_candidate": "the headline you picked",
  "why_it_will_work": "one sentence"
}"""


def write_script(kind: str, candidates: list[Candidate], forced_topic: str | None = None) -> dict[str, Any]:
    c = cfg()["content"]
    avoid = "\n".join(f"- {t}" for t in recent_titles() if t) or "- (none yet)"
    cands = json.dumps([x.as_dict() for x in candidates], ensure_ascii=False, indent=1)

    if kind == "short":
        spec = (f"Write a YouTube Short of ~{c['shorts']['target_seconds']} seconds "
                f"(max {c['shorts']['max_words']} words across all segments, 5-9 segments).\n"
                f"Schema:\n{SHORT_SCHEMA}")
    else:
        spec = (f"Write a long-form YouTube video of ~{c['longform']['target_minutes']} minutes "
                f"(max {c['longform']['max_words']} words, at least {c['longform']['min_sections']} sections; "
                f"the first section is the hook + promise and must be under 45 seconds; add a natural "
                f"re-hook at the start of every section; ask for the subscribe around 60% through, in one line).\n"
                f"Schema:\n{LONG_SCHEMA}")

    topic_rule = (f"The creator asked for this specific topic: {forced_topic!r}. Use it; use candidates only as context."
                  if forced_topic else
                  "Pick the ONE candidate with the best mix of (a) fits our niche, (b) curiosity/virality for "
                  "Indian viewers right now, (c) can be explained accurately from what's known. "
                  "If none fit the niche, pick an evergreen topic from the niche instead and say so in chosen_candidate.")

    user = f"""{spec}

{topic_rule}

Trending candidates (last ~72h):
{cands}

Recent titles on our channel (do NOT repeat these topics):
{avoid}

{_insights_block()}
"""
    last: Exception | None = None
    for _ in range(2):  # one retry if the model returns a malformed script
        script = llm.complete_json(_system_prompt(), user)
        try:
            _validate(kind, script)
            return script
        except llm.LLMError as exc:
            last = exc
            log.warning("invalid script, regenerating: %s", exc)
    raise llm.LLMError(str(last))


def _validate(kind: str, s: dict[str, Any]) -> None:
    required = ["title", "description", "tags", "facts_to_verify"]
    required += ["segments", "hook_text"] if kind == "short" else ["sections", "thumbnail_text"]
    missing = [k for k in required if k not in s or (k != "facts_to_verify" and not s[k])]
    if missing:
        raise llm.LLMError(f"script missing fields: {missing}")
    segs = iter_segments(kind, s)
    if not segs or any(not seg.get("text") for seg in segs):
        raise llm.LLMError("script has empty segments")


def iter_segments(kind: str, script: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten into [{text, broll, section}] in speaking order."""
    if kind == "short":
        segs = [dict(s, section=None) for s in script["segments"]]
        if script.get("loop_line"):
            segs.append({"text": script["loop_line"], "broll": segs[0].get("broll", ""), "section": None})
        return segs
    out = []
    for sec in script["sections"]:
        for i, seg in enumerate(sec["segments"]):
            out.append(dict(seg, section=sec["heading"] if i == 0 else None))
    return out


def teleprompter_text(kind: str, script: dict[str, Any]) -> str:
    """Plain script for reading aloud: one segment per paragraph, section headings in caps."""
    parts = []
    for seg in iter_segments(kind, script):
        if seg.get("section"):
            parts.append(f"\n[{seg['section'].upper()}]")
        parts.append(seg["text"])
    return "\n\n".join(parts).strip()
