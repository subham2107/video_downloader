# Audio Downloader (YouTube → MP3)

A small FastAPI web app. You paste a YouTube link, the server pulls out the audio with
**yt-dlp**, converts it to MP3 with **FFmpeg**, and gives you a download button.
The frontend is one HTML file served by the same FastAPI process, so there is no build
step and no CORS setup. It deploys to **Render as a native Python Web Service, without Docker**.

> **Use responsibly.** Only download content you own or have permission to download. The
> app does not bypass DRM, paywalls, sign-in, age gates or other access restrictions. Videos
> that need any of these are rejected with a clear message.

---

## How it works

```
Browser ──POST /api/download {url}──► FastAPI ── validate URL → canonical watch URL
   │                                     │
   │◄──── 202 {job_id} ──────────────────┤  background job (in-memory queue, semaphore)
   │                                     │   1. yt-dlp --dump-single-json   (metadata + policy checks)
   │── GET /api/status/{id} (polling) ──►│   2. yt-dlp --load-info-json -x --audio-format mp3
   │◄─ queued/processing/completed/failed│      (FFmpeg converts; real byte progress is parsed)
   │                                     │
   │── GET /api/download/{id} ──────────►│  audio/mpeg + safe Content-Disposition
   │── DELETE /api/jobs/{id} (optional) ►│  cancels/removes job + temp files
```

| File | Purpose |
| --- | --- |
| `main.py` | FastAPI app, routes, job manager, cleanup loop, JSON logging, security headers |
| `extractor.py` | FFmpeg/Deno discovery, shell-free subprocess runner with timeout/cancel, yt-dlp calls, error classification |
| `validation.py` | Strict YouTube URL parser and safe-filename helper |
| `static/index.html` | The whole UI (HTML + CSS + vanilla JS) |
| `tests/` | `test_validation.py`, `test_api.py`, `test_extractor.py` (no network needed) |
| `render.yaml` | Render Blueprint (native Python runtime) |

### API

| Method & path | Description |
| --- | --- |
| `GET /` | The web UI |
| `GET /healthz` | `{"status":"ok","ffmpeg":true,"js_runtime":true,"active_jobs":0}`, used as Render's health check |
| `POST /api/download` | Body `{"url": "..."}` → `202 {"job_id","status","status_url"}` |
| `GET /api/status/{job_id}` | `status`: `queued` / `processing` / `completed` / `failed`, plus `stage` (`fetching_info`, `downloading`, `converting`), `progress` (percent, or `null` when unknown), `title`, `filename`, `filesize`, `download_url`, `expires_in`, `error {code, message}` |
| `GET /api/download/{job_id}` | The MP3 (`audio/mpeg`, `Content-Disposition: attachment`) |
| `DELETE /api/jobs/{job_id}` | Cancels a running job, or deletes a finished one and its files |

Every error comes back in the same shape: `{"error": {"code": "...", "message": "user-safe text"}}`.

---

## Prerequisites

* Python **3.12+** (tested locally on 3.14; Render is pinned to 3.12.8)
* Git
* Nothing else. FFmpeg and Deno are installed **by pip** (see below), so you don't need
  `apt`, Homebrew or Docker.

## Local setup

```bash
git clone https://github.com/<you>/youtube-mp3-downloader.git
cd youtube-mp3-downloader

python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

pip install -r requirements-dev.txt     # app + test dependencies
uvicorn main:app --reload --port 8000
```

Open <http://localhost:8000>.

## FFmpeg: installation and verification

Render's native Python runtime doesn't let you `apt-get install` packages, and you can't count
on FFmpeg being there. This project handles it with the
[`imageio-ffmpeg`](https://pypi.org/project/imageio-ffmpeg/) wheel, which **bundles a static
FFmpeg binary** for Linux x86_64 (Render), macOS and Windows. `pip install -r requirements.txt`
is all you need, and it gives the same result on every machine.

When the app starts, it looks for FFmpeg in this order and runs `ffmpeg -version` on each candidate:

1. `FFMPEG_PATH` environment variable (an explicit binary)
2. `ffmpeg` on `PATH` (a system install)
3. the binary bundled with `imageio-ffmpeg`

If none of them work, the app still starts, but it logs an error, `/healthz` reports
`"ffmpeg": false`, and `POST /api/download` returns `503 ffmpeg_unavailable` instead of
accepting jobs that would fail.

To check it yourself:

```bash
python -c "import imageio_ffmpeg, subprocess; p = imageio_ffmpeg.get_ffmpeg_exe(); print(p); subprocess.run([p, '-version'])"
curl http://localhost:8000/healthz
```

### Deno (YouTube JavaScript challenges)

Current yt-dlp versions need a JavaScript runtime to handle YouTube's player challenges.
The [`deno`](https://pypi.org/project/deno/) wheel ships the Deno binary, and
`yt-dlp[default]` includes the `yt-dlp-ejs` solver scripts, so pip installs both.
The app passes Deno's path to yt-dlp explicitly. You can override it with `DENO_PATH`.
`/healthz` shows `"js_runtime": true` when Deno is found.

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests mock every yt-dlp call, so they never download real videos. Their coverage:

* valid, malformed and wrong-host URLs, including SSRF-style inputs such as `169.254.169.254`, `user@host` and ports
* API request validation, oversized bodies and malformed JSON
* invalid and unknown job IDs, plus path-traversal attempts
* processing failures, timeouts (a real subprocess gets killed), cancellation and unexpected crashes that must not leak internal details
* completed downloads: content type, `Content-Disposition`, Unicode and hostile titles
* cleanup on delete, on failure, on retention expiry, of orphaned directories, and at startup/shutdown
* missing FFmpeg and unavailable or restricted videos (error classification)

## Push to GitHub

```bash
git add .
git commit -m "Audio Downloader: FastAPI + yt-dlp + FFmpeg"
git branch -M main
git remote add origin https://github.com/<you>/youtube-mp3-downloader.git
git push -u origin main
```

(Or create the repository with the GitHub CLI: `gh repo create youtube-mp3-downloader --private --source . --push`.)

## Deploy on Render (no Docker)

### Option A: Blueprint (uses `render.yaml`)

1. Push the repository to GitHub.
2. In Render, go to **New → Blueprint** and pick the repository.
3. Render reads `render.yaml` and creates the `youtube-mp3-downloader` web service. Click **Apply**.

### Option B: create the service manually

1. In Render, go to **New → Web Service** and connect the repository.
2. **Language / Runtime:** Python 3
3. **Build command:**
   ```
   pip install -r requirements.txt
   ```
4. **Start command:**
   ```
   uvicorn main:app --host 0.0.0.0 --port $PORT
   ```
5. **Environment:** add `PYTHON_VERSION=3.12.8` (the `.python-version` file also pins it),
   plus any of the settings below.
6. **Health check path:** `/healthz`

Once it's deployed, open `https://<service>.onrender.com/healthz` and confirm that
`ffmpeg` and `js_runtime` are both `true`.

> Keep the **single Uvicorn worker** from the start command. Jobs live in process memory, so
> multiple workers or instances would each have their own job table, and status polls would
> randomly return 404.

### Configuration (environment variables)

| Variable | Default | Render blueprint | Meaning |
| --- | --- | --- | --- |
| `MAX_CONCURRENT_JOBS` | 2 | 1 | Conversions running at the same time |
| `MAX_PENDING_JOBS` | 10 | 5 | Queued + running jobs before new requests get `503 server_busy` |
| `MAX_VIDEO_DURATION` | 1800 | 1200 | Longest video accepted, in seconds |
| `MAX_FILESIZE_MB` | 200 | 200 | Largest source audio download |
| `PROBE_TIMEOUT` | 60 | 60 | Seconds allowed for the metadata lookup |
| `JOB_TIMEOUT` | 600 | 900 | Total wall-clock seconds per job; the process tree is killed when it runs out |
| `JOB_RETENTION` | 900 | 900 | Seconds a finished job and its MP3 stay available |
| `CLEANUP_INTERVAL` | 60 | 60 | How often the cleanup sweep runs |
| `MAX_BODY_BYTES` | 4096 | 4096 | Request body limit |
| `JOBS_DIR` | `<tmp>/ytmp3-jobs` | | Temp working directory |
| `FFMPEG_PATH`, `DENO_PATH` | auto | | Override binary discovery |
| `LOG_LEVEL` | INFO | INFO | Log level |

## Storage, restarts, limits and cleanup

* **Temporary storage only.** Each job gets its own directory, `<tmp>/ytmp3-jobs/<job_id>/`.
  No persistent disk is needed, and you shouldn't add one: the files only exist for minutes.
* **Retention.** A completed MP3 stays downloadable for `JOB_RETENTION` (15 min by default),
  then a background sweep deletes it. Failed jobs lose their partial files immediately but
  keep their status for polling. The **Clear** button calls `DELETE /api/jobs/{id}`, which
  cancels a running job and kills its yt-dlp/FFmpeg processes.
* **Restarts and redeploys.** The job table lives in memory. When the app starts, it wipes
  `JOBS_DIR` because those files belong to a dead process. A client polling an old job gets
  `404 job_not_found`, and the UI shows: *"This job expired or the server restarted. Please try again."*
* **Free plan behaviour.** Free instances spin down after about 15 minutes without traffic.
  The first request after that takes roughly 30–60 s while the instance wakes up, and any
  jobs or files from before are gone. Free instances also have 512 MB RAM and a small share
  of a CPU, which is why the blueprint runs one conversion at a time and caps videos at 20
  minutes. A paid instance can raise these limits.
* **Job access.** A job ID is 128 random bits (`secrets.token_hex(16)`) and works as a
  capability token: only the browser that created the job knows it. IDs are checked against
  `^[0-9a-f]{32}$`, and files are only served from inside `JOBS_DIR`.

## Security measures

* The URL must be `http(s)` on an allow-listed YouTube host (`youtube.com`, `www.`, `m.`,
  `music.`, `youtube-nocookie.com`, `youtu.be`), with no userinfo, no port, no backslashes and
  no control characters. Only `/watch?v=`, `youtu.be/ID`, `/shorts/ID`, `/embed/ID` and
  `/live/ID` paths are accepted, and the ID must match `[A-Za-z0-9_-]{11}`.
* **The user's URL is never passed to yt-dlp.** The app rebuilds
  `https://www.youtube.com/watch?v=<ID>` from the validated ID and puts it after `--`, so
  user input can't point the server at another address or inject options.
* Subprocesses run with an argument list (no shell) and `--ignore-config`, in their own
  process group, with hard timeouts and cancellation that kill the whole process tree.
* The service caps concurrent and queued jobs, request body size, video duration and
  download size.
* Download filenames are cleaned of path separators, quotes, control characters and reserved
  names. `Content-Disposition` carries an ASCII fallback plus an RFC 5987 `filename*`.
* Every response gets security headers (CSP, `nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy`), and API responses are `no-store`.
* Errors never include stack traces, paths or raw yt-dlp output. Logs are structured JSON on
  stdout, which you can read in Render's log viewer.
* There are no secrets, cookies or credentials anywhere in the project.

## Known limitations

* **YouTube may block cloud servers.** YouTube sometimes answers datacenter IPs (Render
  included) with *"Sign in to confirm you're not a bot"* or HTTP 429. The app reports this as
  `temporarily_blocked` and suggests trying later. It intentionally does **not** use account
  cookies or proxies to get around this. If it happens all the time on your instance, the
  realistic options are running the app on a machine with a residential connection, or
  waiting and retrying.
* **Single instance.** The job state lives in memory, so it doesn't scale horizontally. If
  you ever need several instances, move job state to a shared store such as Redis.
* **Progress.** The percentage only shows during the download step, and only when yt-dlp
  reports an exact total size. Metadata lookup and MP3 conversion show an indeterminate bar.
* Live streams, upcoming premieres, private, members-only, age-restricted, geo-blocked and
  DRM-protected videos are refused.
* yt-dlp has to keep up with YouTube changes. If extraction starts failing for every video,
  redeploy with **Clear build cache & deploy** so pip picks up the latest yt-dlp.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `/healthz` shows `"ffmpeg": false` | Check the build log for `imageio-ffmpeg`, and confirm the instance is x86_64 Linux. Set `FFMPEG_PATH` if you provide your own binary. |
| `"js_runtime": false` and many videos fail | Make sure `deno` installed during the build, or set `DENO_PATH`. |
| Every request says "temporarily refusing requests" | YouTube is blocking the server's IP (see Known limitations). Wait, or try another region or host. |
| Error "This job expired or the server restarted" | The instance restarted or slept, or more than `JOB_RETENTION` passed. Convert the video again. |
| `503 server_busy` | The queue is full. Wait a minute, or raise `MAX_PENDING_JOBS` / `MAX_CONCURRENT_JOBS` on a larger instance. |
| `timeout` on long videos | Raise `JOB_TIMEOUT` (and possibly the instance size), or lower `MAX_VIDEO_DURATION`. |
| Paste button does nothing | The browser denied clipboard access, or the page isn't served over HTTPS. Use Ctrl+V / ⌘V instead (the UI says so too). |
| Extraction broke for every video | Update yt-dlp: redeploy with a cleared build cache, or locally run `pip install -U "yt-dlp[default]"`. |
