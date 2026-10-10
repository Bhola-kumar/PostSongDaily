# Lyric Video Generator (Flask)

Inputs (mood, genre, language, era, energy, keyword, artist) → song → lyrics → AI artwork → animated lyric video → title / description / tags.

## Setup

```bash
pip install -r requirements.txt
# ffmpeg: put ffmpeg(.exe) + ffprobe(.exe) in ./ffmpeg/ (or install it on PATH)
cp .env.example .env      # add keys (all optional, see below)
python run.py             # http://localhost:5000
python scripts/smoke_test.py   # offline self-test (no keys, no network)
```

## Deploy as a video-generation API

`render.yaml` and `Dockerfile` define a Render web service with system ffmpeg, one web worker, a persistent output disk, and `/api/healthz` as its readiness check. Connect this repository in Render and deploy the Blueprint. Render generates an `API_KEY`; copy it from the service environment settings and send it as `Authorization: Bearer <API_KEY>` on API requests. The lightweight `/api/healthz` route is public for Render checks. Set optional provider credentials in Render environment settings as needed.

The Blueprint uses a paid Starter service and a 10 GB persistent disk so generated files and job records survive restarts. Keep one instance: this file-backed queue is intentionally single-instance. Increase disk capacity or add an object-storage integration before sustained production use.

Render's health check verifies that the process is ready; it does not keep a Free service awake. Free web services can spin down after 15 minutes without inbound traffic and lose local files on restart/redeploy. To keep a Free instance receiving traffic, configure an external cron/uptime monitor to `GET https://YOUR-SERVICE.onrender.com/api/keepalive` every 10 minutes. It returns HTTP 200 and needs no API key. Set the monitor to alert on failures. This can reduce idle spin-down, but cannot prevent Render maintenance, missed cron runs, or a cold start if a ping arrives after sleep; use the paid always-on service in `render.yaml` for dependable availability.

Example cron schedule: `*/10 * * * *` (every 10 minutes). Use an external scheduler such as cron-job.org, EasyCron, or your own server's cron; a cron job hosted inside the sleeping web service cannot wake that service.

### Submit one video or a batch

```bash
curl -X POST "https://YOUR-SERVICE.onrender.com/api/jobs" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"count":3,"mood":"chill","genre":"lofi","language":"english","era":"any","energy":"low","keyword":"rainy night","aspect":"story","clip_seconds":30}'
```

The API returns HTTP `202`, a `batch_id`, links for batch and per-video status, and each `job_id`. `count` is optional (default `1`) and limited by `MAX_BATCH_SIZE`; each requested video counts toward `DAILY_LIMIT`. Videos are queued and processed one at a time.

Poll `GET /api/batches/{batch_id}` for aggregate `counts` (`queued`, `running`, `done`, `error`), `completed`, and average `progress`, plus the current state of every video. Poll `GET /api/jobs/{job_id}` for detailed stage/progress/message, errors, and download URLs. Once done, follow the returned `urls.video`, `urls.image`, `urls.audio`, or `urls.metadata` links using the same Bearer token. Status and records are written to `OUTPUT_DIR/.jobs`; finished files are under `OUTPUT_DIR/{job_id}`. `GET /api/health` reports today's accepted video count and current in-memory job counts.

Requests without a valid API key receive `401` when `API_KEY` is configured. For local development, leave it empty or set it in `.env` and include the same Bearer header.

On managed Windows devices, Application Control may block an ffmpeg executable. Install or obtain an executable approved by your organization, set `FFMPEG_PATH` in `.env` to its file path (or containing folder), and restart the app. `/api/health` reports whether the selected executable can start.

## Pipeline & modules (`app/modules/`)

| Step | Module | Free source |
|---|---|---|
| 1. Pick song | `selector.py` | builds queries from inputs, scores candidates (artist/keyword/era/genre match + jitter), skips songs already used |
| 2. Music | `music.py` | iTunes Search, Deezer (30s previews, no key), Jamendo (full royalty-free tracks, free key) |
| 3. Lyrics | `lyrics.py` | LRCLIB (synced) → lyrics.ovh fallback |
| 4. Artwork | `imagegen.py` | Pollinations → Hugging Face → offline procedural fallback |
| 5. Video | `video.py` | Pillow frames piped into ffmpeg: slow zoom + word-by-word lyric highlight |
| 6. Metadata | `metadata.py`, `tags.py` | Gemini REST + trending hashtags (Trendgetter, Bright Data, your own endpoint), template fallback |
| Orchestration | `pipeline.py`, `jobs.py` | single-worker job queue, daily cap (`DAILY_LIMIT`) |

Each step has its own endpoint, so you can reuse modules separately.

## API

- `POST /api/jobs` `{mood, genre, language, era, energy, keyword, artist, aspect, clip_seconds, royalty_free_only}` → `{job_id}`
- `GET /api/jobs/<id>` → status, progress, result (+ file URLs when done)
- `GET /api/files/<id>/<name>` → `video.mp4`, `image.jpg`, `clip.mp3`, `metadata.json`
- `POST /api/steps/song | lyrics | image | metadata` → run one module alone
- `GET /api/steps/tags?refresh=1` → trending hashtags + per-provider errors
- `GET /api/health` (shows which ffmpeg/ffprobe it found), `GET /api/options`

`aspect`: `portrait` (1080×1350, Instagram 4:5), `square`, `story` (1080×1920).

## How lyrics are timed

- **Jamendo (full track) + synced lyrics:** the clip starts at the chorus and lines follow the real timestamps.
- **iTunes/Deezer 30s previews:** previews start at an unknown point of the song, so exact sync isn't possible. The song's hook (most repeated) lines are spread evenly over the clip instead.

## Trending hashtags

Providers run in the order of `TRENDING_PROVIDERS` and stop once 15 tags are found:

1. **Trendgetter** (free, no key, `trendgetter.vercel.app`): X trending hashtags + hashtags from trending TikTok videos.
2. **Bright Data** (free tier 5K records/month): needs `BRIGHTDATA_API_KEY` and the `BRIGHTDATA_DATASET_ID` of a hashtag scraper from your dashboard. The scraper returns posts, so hashtags are counted across them.
3. **Custom** endpoint (`TRENDING_TAGS_URL`).
4. A static seed list fills any gap, and results are cached for `TRENDING_CACHE_HOURS`.

Run `python scripts/check_tags.py` to test each provider and see exact errors.

## Notes

- Non-Latin lyrics (Hindi, Korean, …) need a font with those glyphs: drop a Noto Sans `.ttf` into `assets/fonts/` or set `FONT_PATH`.
- Rendering is CPU-bound: roughly a minute for a 30s clip on a small machine.
- Outputs are written to `outputs/<job_id>/`; `history.json` prevents repeating a song.
- **Copyright:** commercial previews and lyrics are copyrighted. Posting them can trigger takedowns or demonetisation. For anything you publish publicly, use `royalty_free_only` (Jamendo) or check the licence.
- Free-tier APIs change; every provider failure is caught and the chain moves on.
