# Setup: about 60–90 minutes, one time

Everything here is free. Do the steps in order. Each one produces a secret you paste into
**GitHub → your repo → Settings → Secrets and variables → Actions → New repository secret**.

| Secret | From step |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 3 |
| `GEMINI_API_KEY` | 4 |
| `PEXELS_API_KEY` | 5 |
| `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` | 6 |

---

## 1. Create the YouTube channel (10 min)

1. Sign in to YouTube with the Google account you'll use → profile → **Create a channel**. Use a
   *brand account* (choose "use a custom name"), so you can add managers later.
2. Name: the `channel.name` in `config.yaml` (or change both). Handle: `channel.handle`.
3. **Verify by phone**: <https://www.youtube.com/verify>. This unlocks custom thumbnails, videos
   longer than 15 minutes, and pinned comments. Without it, thumbnail uploads fail.
4. Fill in the About section, links and a banner. The 30-day plan has the copy.

## 2. Create the GitHub repo (5 min)

1. Create a new repo (e.g. `yt-autopilot`) and push this folder to it.
2. **Public or private?** GitHub Actions minutes are unlimited on public repos and 2,000/month on
   private ones. This pipeline uses about 1,400–1,800 minutes a month, so private works but is tight.
   Secrets stay hidden either way. Only the scripts and config are visible in a public repo.
3. Settings → Actions → General → Workflow permissions → **Read and write**.

## 3. Telegram bot, your control panel (5 min)

1. In Telegram, message **@BotFather** → `/newbot` → pick a name → copy the token → secret `TELEGRAM_BOT_TOKEN`.
2. Open your new bot and send it `/start`.
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and find `"chat":{"id":123456789`.
   That number goes in secret `TELEGRAM_CHAT_ID`. The bot ignores everyone else.

## 4. Gemini API key, free script writer (3 min)

<https://aistudio.google.com/apikey> → Create API key → secret `GEMINI_API_KEY`.
Free-tier limits are far above what 2–3 scripts a day need. To use Claude instead, add
`ANTHROPIC_API_KEY` and set `llm.provider: anthropic` in `config.yaml`.

## 5. Pexels API key, free B-roll (2 min)

<https://www.pexels.com/api/> → sign up → copy the key → secret `PEXELS_API_KEY`.

## 6. YouTube API access (20–30 min, the fiddly part)

1. <https://console.cloud.google.com/> → **New project** (e.g. `yt-autopilot`).
2. APIs & Services → Library → enable **YouTube Data API v3** and **YouTube Analytics API**.
3. APIs & Services → **OAuth consent screen**:
   - User type **External**. Fill in the app name and your email.
   - Scopes: add `.../auth/youtube` and `.../auth/yt-analytics.readonly`.
   - **Publishing status → click "Publish app" (In production).** Ignore the "needs verification"
     notice. ⚠️ If you leave it in *Testing*, Google expires your token every 7 days and uploads
     stop silently.
4. Credentials → Create credentials → **OAuth client ID** → Application type **Desktop app** →
   download the JSON.
5. On your computer:
   ```bash
   pip install google-auth-oauthlib
   python scripts/get_refresh_token.py ~/Downloads/client_secret_XXXX.json
   ```
   Sign in, click *Advanced → Go to app (unsafe)* (it's your own app), **select your channel's
   brand account**, and allow. Paste the three printed values into secrets.

### 6b. Apply for the API compliance audit (do this on day 1)

Until Google audits your project, **every video uploaded via the API is locked to private**. The
pipeline works around this: it uploads privately and you click *Publish/Schedule* in Studio, which
takes 30 seconds. To remove that step:

- Fill in the form: <https://support.google.com/youtube/contact/yt_api_form>
  (choose "Audit and quota extension"). Describe it honestly: *"Personal tool that uploads
  my own channel's videos that I record and review; one user (me)."* Approval usually takes 2–4 weeks.
- Once approved, set `youtube.api_audited: true` in `config.yaml`. After that, `/publish S1029` in
  Telegram schedules videos for you.

## 7. Background music (5 min)

YouTube Studio → **Audio Library** → filter *Attribution not required* → download 5–10 calm
tracks → commit them into `assets/music/`.

## 8. First run

1. Actions tab → **plan** → *Run workflow* (kind: `short`). Within about a minute, a script arrives in Telegram.
2. Record it on your phone (see the recording tips below) → **reply to the script message** with the audio file.
3. Actions → **poll** → *Run workflow* (or wait up to 30 min). The render job starts automatically, and
   10–30 minutes later you get a private YouTube link.
4. Watch it. If it's good, publish it in Studio. From then on everything runs on schedule:

| When (IST) | What happens |
|---|---|
| 06:47 daily | Trend scan → script(s) sent to Telegram (long-form on Mon/Wed/Fri) |
| every 30 min, 07:37–00:07 | Bot checks for your recordings and commands → renders → uploads |
| Sunday 09:53 | Analytics report + the writer updates what it learns from |

## Recording tips (this matters more than anything else in the pipeline)

- Use any **voice-memo app that records m4a** (iPhone Voice Memos, Android "Recorder"/"Easy Voice Recorder").
  Send it to the bot as a **file** (📎 → File). Telegram voice notes are compressed to low quality.
- Record in a small room with soft furnishings (a wardrobe full of clothes is a great booth), phone 15–20 cm
  from your mouth and slightly to the side.
- Read it once silently, then record in one take. **Pauses and restarts are fine**: long pauses get cut
  automatically. If you fumble a line, pause for a second and say it again. Expect to occasionally re-record.
- Energy at 120%. A voice that sounds slightly over the top in the room sounds normal on YouTube.

## Telegram commands

```
/new short [topic]     write a new Short script now (optionally on your topic)
/new long [topic]      same for long-form
/status                list pending episodes
/skip S1029            drop a script
/redo S1029            re-render (same recording)
/publish S1029 [now|19:30|2026-11-02 18:00]   schedule (after API audit)
```

## Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| No script at 06:47 | Actions disabled after 60 days of repo inactivity (GitHub pauses cron) → re-enable in the Actions tab. Gemini 429 → it retries; try `/new short`. |
| `invalid_grant` in render logs | Refresh token expired. Consent screen was in Testing → publish the app, re-run step 6.5. |
| Upload works but the video stays private | Expected until the audit (6b). |
| Thumbnail not set | Channel not phone-verified (step 1.3). |
| Captions are off-sync | Ad-libbing heavily is fine for B-roll, but captions come from Whisper, so check the audio isn't clipped or noisy. |
| `quotaExceeded` | Default quota is ~6 uploads/day. You're nowhere near that unless re-rendering in a loop. |
