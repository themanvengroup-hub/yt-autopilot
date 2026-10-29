# yt-autopilot

A YouTube channel pipeline where everything except your voice is automated: trend research, scripts,
editing, captions, thumbnails, upload and analytics. It runs free on GitHub Actions. You control it from Telegram.

```
06:47 IST  ┌──────────────┐   ┌───────────────┐   ┌──────────────┐
cron ────▶ │ trend scan   │──▶│ script writer │──▶│ Telegram bot │──▶ you (phone)
           │ GTrends/News │   │ Gemini/Claude │   │ teleprompter │       │ record 2–10 min
           └──────────────┘   └───────────────┘   └──────────────┘       ▼
                                   ▲ insights                     reply with audio
                                   │                                      │
Sun cron ─▶ analytics ─────────────┘                    every 30 min: poll ▼
                                                    ┌───────────────────────────────────┐
                                                    │ render                            │
                                                    │ denoise+tighten → Whisper words   │
                                                    │ → shot plan → Pexels B-roll       │
                                                    │ → karaoke captions → music duck   │
                                                    │ → thumbnail → YouTube upload      │
                                                    └───────────────────────────────────┘
                                                                    │
                                                 private link to you → publish/schedule
```

**State** is stored as `content/<episode>/episode.json` and `state/*.json`, committed by the workflows
(git history = audit log). No DB, no server, nothing to host.

- Setup: [`docs/SETUP.md`](docs/SETUP.md)
- Config: [`config.yaml`](config.yaml) (niche, schedule, guardrails, render style)
- Tests: `PYTHONPATH=src pytest -q`
- Local render: `PYTHONPATH=src python -m autopilot render --episode <id>` (needs ffmpeg + secrets in env)

## Design decisions

- **Your own voice instead of TTS.** It passes YouTube's inauthentic/mass-produced content review, gets
  higher retention, and costs nothing. Everything else is automated.
- **Upload private → you publish.** Forced by Google until the API audit passes. It also acts as a
  quality gate, which is a good thing on a new channel.
- **Python, not .NET.** The media ecosystem (faster-whisper, ffmpeg tooling, Pillow) is far better in Python.
  The code is small and typed, so it ports easily if you ever need it in C#.
- **Position-based B-roll sync.** Shots map to script segments by relative word position, not exact text,
  so ad-libs never break a render.
- **Fails soft.** A dead trend source is skipped, Whisper failure falls back to estimated timings, a missing
  Pexels clip becomes a plain background, and a failed render is retried once, then reported to you.
