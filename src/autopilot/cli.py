"""Entry points used by the GitHub Actions workflows.

    python -m autopilot plan [--kind short|long] [--topic "..."]
    python -m autopilot poll
    python -m autopilot render [--episode ID]
    python -m autopilot analytics
    python -m autopilot status
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import scriptwriter, telegram as tg, trends
from .config import WORK_DIR, cfg
from .state import Episode, all_episodes, episodes_with, find_by_message, find_by_ref, now_local, read_state, write_state

log = logging.getLogger("autopilot")

HELP = """<b>Commands</b>
/new short [topic]: new Short script now (topic optional)
/new long [topic]: new long-form script now
/publish REF [now | 19:30 | 2026-11-02 18:00]: schedule an uploaded video
/redo REF: re-render with the same recording
/skip REF: drop a script you won't record
/status: what's pending
<b>Recording</b>: reply to any script message with your audio. Send it as a <i>file</i>, not a voice note, for the best quality."""


# ── plan ─────────────────────────────────────────────────────
def kinds_due_today() -> list[str]:
    s = cfg()["schedule"]
    kinds = ["short"] * int(s["shorts_per_day"])
    if now_local().weekday() in s["longform_days"]:
        kinds.insert(0, "long")
    return kinds


def create_and_send(kind: str, candidates: list[trends.Candidate], topic: str | None = None) -> Episode:
    script = scriptwriter.write_script(kind, candidates, topic)
    picked = script.get("chosen_candidate", "")
    sources = [c.as_dict() for c in candidates if c.title == picked][:1]
    ep = Episode.create(kind, script, sources)
    send_script(ep)
    return ep


def send_script(ep: Episode) -> None:
    s, e = ep.script, tg.esc
    label = "SHORT" if ep.kind == "short" else "LONG-FORM"
    facts = "\n".join(f"• {e(f.get('claim', ''))} ({e(f.get('source_url', ''))})" for f in s.get("facts_to_verify", []))
    header = [
        f"<b>{label} · {ep.ref}</b>\n<b>{e(s['title'])}</b>",
        f"<i>Why this topic:</i> {e(s.get('why_it_will_work', ''))}",
        f"<b>Check these facts before you record:</b>\n{facts or '• (none)'}",
    ]
    if ep.kind == "long":
        header.append(f"<i>Thumbnail text:</i> {e(s.get('thumbnail_text', ''))}")
    body = [f"<b>SCRIPT</b> (read naturally; small ad-libs are fine)"]
    body += [e(p) for p in scriptwriter.teleprompter_text(ep.kind, s).split("\n\n")]
    footer = [f"<b>Reply to this message with your recording for {ep.ref}.</b>\n"
              f"Want changes? /new {ep.kind} &lt;your topic&gt; · Drop it: /skip {ep.ref}"]
    ep.telegram_message_ids = tg.send_long(header) + tg.send_long(body) + tg.send_long(footer)
    ep.save()


def cmd_plan(args: argparse.Namespace) -> None:
    kinds = [args.kind] if args.kind else kinds_due_today()
    candidates = trends.gather()
    for kind in kinds:
        try:
            create_and_send(kind, candidates, args.topic)
        except Exception as exc:  # noqa: BLE001
            log.exception("planning %s failed", kind)
            tg.send(f"Could not write today's {kind} script: <code>{tg.esc(str(exc))[:500]}</code>\n"
                    f"Try /new {kind} to retry.")
    stale = [ep for ep in episodes_with("scripted")
             if datetime.fromisoformat(ep.created_at) < now_local() - timedelta(days=2)]
    if stale:
        tg.send("Still waiting on recordings for: " + ", ".join(f"{ep.ref} ({tg.esc(ep.script['title'])})" for ep in stale)
                + "\nRecord them or /skip REF.")


# ── poll ─────────────────────────────────────────────────────
def _parse_publish_time(arg: str, kind: str) -> datetime | None:
    now = now_local()
    arg = arg.strip().lower()
    if arg == "now":
        return None
    if not arg:
        slot = cfg()["schedule"]["shorts_slot" if kind == "short" else "longform_slot"]
        arg = slot
    if re.fullmatch(r"\d{1,2}:\d{2}", arg):
        h, m = map(int, arg.split(":"))
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return t if t > now + timedelta(minutes=15) else t + timedelta(days=1)
    t = datetime.strptime(arg, "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo)
    if t <= now:
        raise ValueError("that time is in the past")
    return t


def _handle_command(text: str, msg_id: int) -> None:
    parts = text.split(maxsplit=2)
    cmd = parts[0].split("@")[0].lower()
    arg1 = parts[1] if len(parts) > 1 else ""
    rest = parts[2] if len(parts) > 2 else ""

    if cmd in ("/help", "/start"):
        tg.send(HELP)
    elif cmd == "/status":
        lines = [f"{ep.ref} · {ep.status} · {tg.esc(ep.script['title'])}"
                 for ep in all_episodes() if ep.status in ("scripted", "recorded", "uploaded", "failed")]
        tg.send("<b>Pending</b>\n" + ("\n".join(lines) if lines else "Nothing pending."))
    elif cmd == "/new":
        kind = arg1 if arg1 in ("short", "long") else "short"
        topic = rest if arg1 in ("short", "long") else " ".join(parts[1:]) or None
        tg.send(f"Writing a {kind} script{' on ' + tg.esc(topic) if topic else ''}…", reply_to=msg_id)
        create_and_send(kind, trends.gather(), topic)
    elif cmd in ("/skip", "/redo", "/publish"):
        ep = find_by_ref(arg1) if arg1 else None
        if not ep:
            tg.send(f"Unknown episode {tg.esc(arg1)!r}. Use /status to see refs.", reply_to=msg_id)
            return
        if cmd == "/skip":
            ep.transition("skipped", "via telegram")
            tg.send(f"Skipped {ep.ref}.", reply_to=msg_id)
        elif cmd == "/redo":
            if not ep.voice_file_id:
                tg.send(f"{ep.ref} has no recording yet.", reply_to=msg_id)
                return
            ep.attempts = 0
            ep.transition("recorded", "redo requested")
            tg.send(f"Re-rendering {ep.ref}. The new private upload link is on its way.", reply_to=msg_id)
        else:
            _publish(ep, rest if rest else "", msg_id)
    else:
        tg.send("Unknown command.\n\n" + HELP, reply_to=msg_id)


def _publish(ep: Episode, when: str, msg_id: int) -> None:
    from . import youtube

    if not ep.youtube_id:
        tg.send(f"{ep.ref} isn't uploaded yet.", reply_to=msg_id)
        return
    if not cfg()["youtube"]["api_audited"]:
        tg.send(f"Your API project isn't audited yet, so Google blocks publishing through the API.\n"
                f"Publish it in Studio: https://studio.youtube.com/video/{ep.youtube_id}/edit", reply_to=msg_id)
        return
    t = _parse_publish_time(when, ep.kind)
    youtube.schedule(ep.youtube_id, t)
    ep.publish_at = t.isoformat() if t else now_local().isoformat()
    ep.transition("scheduled" if t else "published")
    tg.send(f"{ep.ref} {'scheduled for ' + t.strftime('%a %d %b %H:%M') if t else 'is live'}: "
            f"https://youtu.be/{ep.youtube_id}", reply_to=msg_id)


def _handle_audio(msg: dict, file_id: str, size: int) -> None:
    ep = None
    reply = msg.get("reply_to_message")
    if reply:
        ep = find_by_message(reply["message_id"])
    if not ep and msg.get("caption"):
        m = re.search(r"\b([SL]\d{4}[a-z]?)\b", msg["caption"], re.I)
        ep = find_by_ref(m.group(1)) if m else None
    if not ep:
        waiting = episodes_with("scripted")
        if len(waiting) == 1:
            ep = waiting[0]
    if not ep:
        tg.send("I couldn't tell which script this recording is for. Reply to the script message with it, "
                "or put the ref (e.g. S1029) in the caption.", reply_to=msg["message_id"])
        return
    if size > 20 * 1024 * 1024:
        tg.send("That file is over Telegram's 20 MB bot limit. Export it as m4a/AAC at 96–128 kbps and resend.",
                reply_to=msg["message_id"])
        return
    ep.voice_file_id = file_id
    ep.attempts = 0
    ep.transition("recorded", "voice received")
    tg.send(f"Got it. Editing {ep.ref} now; you'll get a private YouTube link in ~10–30 min.",
            reply_to=msg["message_id"])


def cmd_poll(_: argparse.Namespace) -> None:
    offset = read_state("telegram_offset", 0)
    me = tg.chat_id()
    updates = tg.get_updates(offset)
    for upd in updates:
        offset = max(offset, upd["update_id"] + 1)
        msg = upd.get("message")
        if not msg or msg.get("chat", {}).get("id") != me:
            continue  # ignore strangers
        try:
            audio = tg.audio_file_id(msg)
            if audio:
                _handle_audio(msg, *audio)
            elif (msg.get("text") or "").startswith("/"):
                _handle_command(msg["text"], msg["message_id"])
        except Exception as exc:  # noqa: BLE001
            log.exception("handling update failed")
            tg.send(f"Error: <code>{tg.esc(str(exc))[:500]}</code>", reply_to=msg.get("message_id"))
        write_state("telegram_offset", offset)  # persist progress per update
    write_state("telegram_offset", offset)
    _set_output("render", "true" if episodes_with("recorded") else "false")


# ── render ───────────────────────────────────────────────────
def cmd_render(args: argparse.Namespace) -> None:
    from . import youtube
    from .render import render_episode

    targets = [Episode.load(args.episode)] if args.episode else episodes_with("recorded")
    for ep in targets:
        ep.attempts += 1
        ep.save()
        try:
            # keep the download OUTSIDE the render work dir: render_episode() wipes that folder first
            voice = tg.download_file(ep.voice_file_id, WORK_DIR / "voices" / f"{ep.id}.audio")
            result = render_episode(ep, voice)
            desc = youtube.build_description(ep.script, result.chapters)
            vid = youtube.upload(result.video, title=ep.script["title"], description=desc,
                                 tags=ep.script.get("tags", []))
            if result.thumbnail:
                youtube.set_thumbnail(vid, result.thumbnail)
            ep.youtube_id, ep.error = vid, None
            ep.transition("uploaded", f"{result.duration:.0f}s")
            _notify_uploaded(ep, result.duration)
            _set_output("video_path", str(result.video))
        except Exception as exc:  # noqa: BLE001
            log.exception("render %s failed", ep.id)
            msg = str(exc)
            ep.error = msg if len(msg) <= 1500 else msg[:300] + " … " + msg[-1200:]  # keep the real ffmpeg error at the end
            ep.transition("failed" if ep.attempts >= 2 else "recorded", f"attempt {ep.attempts}")
            tg.send(f"Render of {ep.ref} failed (attempt {ep.attempts}): <code>{tg.esc(str(exc))[:600]}</code>"
                    + ("\nI'll retry on the next run." if ep.status == "recorded" else f"\nFix it, then /redo {ep.ref}."))


def _notify_uploaded(ep: Episode, seconds: float) -> None:
    s = ep.script
    audited = cfg()["youtube"]["api_audited"]
    slot = cfg()["schedule"]["shorts_slot" if ep.kind == "short" else "longform_slot"]
    lines = [
        f"<b>{ep.ref} uploaded (private)</b>: {tg.esc(s['title'])}",
        f"Length {int(seconds // 60)}:{int(seconds % 60):02d}",
        f"Review: https://studio.youtube.com/video/{ep.youtube_id}/edit",
    ]
    if s.get("pinned_comment"):
        lines.append(f"Pin this comment after publishing: <i>{tg.esc(s['pinned_comment'])}</i>")
    if audited:
        lines.append(f"/publish {ep.ref} schedules it for {slot} IST · /publish {ep.ref} now")
    else:
        lines.append(f"Watch it, then in Studio set Visibility → Schedule → {slot} IST.")
    tg.send("\n".join(lines), reply_to=ep.telegram_message_ids[-1] if ep.telegram_message_ids else None)


# ── analytics ────────────────────────────────────────────────
def cmd_analytics(_: argparse.Namespace) -> None:
    from . import youtube

    stats = youtube.channel_stats()
    perf = youtube.video_performance(28)
    titles = youtube.video_titles([p["video"] for p in perf])
    rows = [{"id": p["video"], "title": titles.get(p["video"], p["video"]), "views": int(p["views"]),
             "avg_view_pct": round(float(p["averageViewPercentage"]), 1), "subs": int(p["subscribersGained"]),
             "minutes": int(p["estimatedMinutesWatched"])} for p in perf]
    scored = [r for r in rows if r["views"] >= 50]
    # rank by retention × reach, the two signals the algorithm weights most
    scored.sort(key=lambda r: r["avg_view_pct"] * (r["views"] ** 0.5), reverse=True)
    hours = youtube.longform_watch_hours_365()
    insights = {"updated": now_local().isoformat(timespec="seconds"), "top": scored[:6],
                "bottom": scored[-3:] if len(scored) > 6 else [], "stats": stats, "longform_hours": hours}
    history = read_state("growth_history", [])
    history.append({"date": now_local().date().isoformat(), **stats, "longform_hours": hours})
    write_state("growth_history", history[-120:])
    write_state("insights", insights)

    pace = ""
    if len(history) >= 2:
        prev = history[-2]
        days = max((datetime.fromisoformat(history[-1]["date"]) - datetime.fromisoformat(prev["date"])).days, 1)
        per_day = (stats["subscribers"] - prev["subscribers"]) / days
        if per_day > 0 and stats["subscribers"] < 1000:
            pace = f"\nAt the current pace (+{per_day:.1f} subs/day), you reach 1,000 subs in ~{(1000 - stats['subscribers']) / per_day:.0f} days."
    top = "\n".join(f"• {tg.esc(r['title'])}: {r['views']} views, {r['avg_view_pct']}% viewed, +{r['subs']} subs"
                    for r in scored[:3]) or "• not enough data yet"
    tg.send(f"<b>Weekly report</b>\n"
            f"Subscribers: <b>{stats['subscribers']}</b> / 1,000\n"
            f"Long-form watch hours (365d): <b>{hours if hours is not None else 'n/a'}</b> / 4,000\n"
            f"Videos: {stats['videos']} · Lifetime views: {stats['views']}{pace}\n\n"
            f"<b>Top performers (28d)</b>\n{top}\n\nThe script writer now leans toward what's working.")


def cmd_status(_: argparse.Namespace) -> None:
    for ep in all_episodes():
        print(f"{ep.ref:7} {ep.status:10} {ep.kind:5} {ep.script.get('title', '')}")


def _set_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            fh.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="autopilot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("plan"); sp.add_argument("--kind", choices=["short", "long"]); sp.add_argument("--topic")
    sp.set_defaults(fn=cmd_plan)
    sub.add_parser("poll").set_defaults(fn=cmd_poll)
    sr = sub.add_parser("render"); sr.add_argument("--episode"); sr.set_defaults(fn=cmd_render)
    sub.add_parser("analytics").set_defaults(fn=cmd_analytics)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main(sys.argv[1:])
