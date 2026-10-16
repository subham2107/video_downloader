"""Audio Downloader: a small FastAPI service that converts YouTube videos to MP3.

Job tracking is in memory on purpose (no Redis/DB). That means:
  * run a single Uvicorn worker process;
  * a restart or redeploy forgets every job and wipes the temp directory -- clients
    receive 404 ``job_not_found`` and are asked to start again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

import extractor
from extractor import Cancelled, ExtractionError, ExtractionResult, Limits, Toolchain
from validation import InvalidURLError, parse_youtube_url, safe_filename

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


# --------------------------------------------------------------------- settings


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    max_concurrent_jobs: int = _env_int("MAX_CONCURRENT_JOBS", 2)
    max_pending_jobs: int = _env_int("MAX_PENDING_JOBS", 10)
    max_video_duration: int = _env_int("MAX_VIDEO_DURATION", 30 * 60)
    max_filesize_mb: int = _env_int("MAX_FILESIZE_MB", 200)
    probe_timeout: int = _env_int("PROBE_TIMEOUT", 60)
    job_timeout: int = _env_int("JOB_TIMEOUT", 10 * 60)
    job_retention: int = _env_int("JOB_RETENTION", 15 * 60)
    cleanup_interval: int = _env_int("CLEANUP_INTERVAL", 60)
    max_body_bytes: int = _env_int("MAX_BODY_BYTES", 4096)
    jobs_dir: Path = Path(os.environ.get("JOBS_DIR") or Path(tempfile.gettempdir()) / "ytmp3-jobs")

    @property
    def limits(self) -> Limits:
        return Limits(
            max_duration=self.max_video_duration,
            max_filesize_mb=self.max_filesize_mb,
            probe_timeout=self.probe_timeout,
            job_timeout=self.job_timeout,
        )


# ---------------------------------------------------------------------- logging


class JsonFormatter(logging.Formatter):
    """One JSON object per line -- easy to search in Render's log viewer."""

    _RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    root = logging.getLogger("ytmp3")
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
        root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
        root.propagate = False


log = logging.getLogger("ytmp3.app")


# ------------------------------------------------------------------------- jobs

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

ACTIVE = ("queued", "processing")
FINISHED = ("completed", "failed")


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        self.status, self.code, self.message, self.headers = status, code, message, headers


@dataclass
class Job:
    id: str
    video_id: str
    url: str
    workdir: Path
    status: str = "queued"
    stage: str | None = None
    title: str | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    file_path: Path | None = None
    filename: str | None = None
    filesize: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task | None = None

    @property
    def progress(self) -> float | None:
        """Percent downloaded -- only when yt-dlp reports an exact total size."""
        if self.stage != "downloading" or not self.total_bytes or self.downloaded_bytes is None:
            return None
        return round(min(100.0, 100.0 * self.downloaded_bytes / self.total_bytes), 1)

    def public(self, retention: int) -> dict[str, Any]:
        data: dict[str, Any] = {
            "job_id": self.id,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "title": self.title,
            "filename": None,
            "filesize": None,
            "download_url": None,
            "expires_in": None,
            "error": None,
        }
        if self.status == "completed":
            data.update(
                filename=self.filename,
                filesize=self.filesize,
                download_url=f"/api/download/{self.id}",
            )
        if self.finished_at is not None:
            data["expires_in"] = max(0, int(self.finished_at + retention - time.time()))
        if self.status == "failed":
            data["error"] = {"code": self.error_code, "message": self.error_message}
        return data


ExtractFn = Callable[..., ExtractionResult]


class DownloadRequest(BaseModel):
    url: str


class JobManager:
    def __init__(self, settings: Settings, toolchain: Toolchain, extract: ExtractFn = extractor.extract_mp3):
        self.settings = settings
        self.toolchain = toolchain
        self.extract = extract
        self.jobs: dict[str, Job] = {}
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self.root = settings.jobs_dir.resolve()

    # -- lifecycle -----------------------------------------------------------

    def prepare_storage(self) -> None:
        """Start from an empty jobs directory; anything left belongs to a dead process."""
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)

    async def shutdown(self) -> None:
        for job in self.jobs.values():
            job.cancel.set()
        tasks = [j.task for j in self.jobs.values() if j.task and not j.task.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=10)
        self.jobs.clear()
        shutil.rmtree(self.root, ignore_errors=True)

    # -- queries -------------------------------------------------------------

    def active_count(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status in ACTIVE)

    def get(self, job_id: str) -> Job:
        if not JOB_ID_RE.fullmatch(job_id):
            raise APIError(400, "invalid_job_id", "Invalid job ID.")
        job = self.jobs.get(job_id)
        if job is None:
            raise APIError(
                404, "job_not_found", "This job has expired or doesn't exist. Please start again."
            )
        return job

    # -- commands ------------------------------------------------------------

    def submit(self, video_id: str, url: str) -> Job:
        if not self.toolchain.ffmpeg:
            raise APIError(
                503, "ffmpeg_unavailable", "Audio conversion is temporarily unavailable on this server."
            )
        if self.active_count() >= self.settings.max_pending_jobs:
            raise APIError(
                503,
                "server_busy",
                "The server is busy right now. Please try again in a minute.",
                headers={"Retry-After": "60"},
            )
        job_id = secrets.token_hex(16)
        workdir = self.root / job_id
        workdir.mkdir(parents=True, exist_ok=False)
        job = Job(id=job_id, video_id=video_id, url=url, workdir=workdir)
        self.jobs[job_id] = job
        job.task = asyncio.create_task(self._run(job))
        log.info("job queued", extra={"job_id": job_id, "video_id": video_id})
        return job

    def delete(self, job_id: str) -> None:
        job = self.get(job_id)
        job.cancel.set()
        self.jobs.pop(job.id, None)
        if job.status == "queued" and job.task is not None:
            job.task.cancel()  # still waiting for a slot; nothing external to kill
        if job.task is None or job.task.done():
            self._remove_files(job)
        # Otherwise the running task removes the files once its process is killed.
        log.info("job deleted", extra={"job_id": job.id, "status": job.status})

    def resolve_download(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status != "completed":
            raise APIError(409, "not_ready", "The MP3 isn't ready yet.")
        path = job.file_path
        if path is None or not self._inside_root(path) or not path.is_file():
            raise APIError(410, "file_gone", "The file is no longer available. Please convert it again.")
        return job

    # -- internals -----------------------------------------------------------

    def _inside_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
        except ValueError:
            return False
        return True

    def _remove_files(self, job: Job) -> None:
        if self._inside_root(job.workdir) and job.workdir.resolve() != self.root:
            shutil.rmtree(job.workdir, ignore_errors=True)

    def _progress(self, job: Job) -> Callable[..., None]:
        def update(stage: str | None = None, title: str | None = None, downloaded=None, total=None):
            if stage:
                job.stage = stage
            if title:
                job.title = title
            if downloaded is not None:
                job.downloaded_bytes = downloaded
            if total is not None:
                job.total_bytes = total

        return update

    async def _run(self, job: Job) -> None:
        started = time.monotonic()
        try:
            async with self.semaphore:
                if job.cancel.is_set():
                    raise Cancelled()
                job.status = "processing"
                job.stage = "fetching_info"
                result = await asyncio.to_thread(
                    self.extract,
                    job.url,
                    job.workdir,
                    self.toolchain,
                    self.settings.limits,
                    job.cancel,
                    self._progress(job),
                )
            stem = safe_filename(result.title or job.title, fallback=f"audio-{job.video_id}")
            job.title = result.title or job.title
            job.file_path = result.path
            job.filename = f"{stem}.mp3"
            job.filesize = result.filesize
            job.status = "completed"
            job.stage = None
            log.info(
                "job completed",
                extra={
                    "job_id": job.id,
                    "video_id": job.video_id,
                    "seconds": round(time.monotonic() - started, 1),
                    "bytes": result.filesize,
                },
            )
        except Cancelled:
            job.status, job.stage = "failed", None
            job.error_code, job.error_message = "cancelled", "The job was cancelled."
            log.info("job cancelled", extra={"job_id": job.id})
        except ExtractionError as exc:
            job.status, job.stage = "failed", None
            job.error_code, job.error_message = exc.code, exc.message
            log.warning("job failed", extra={"job_id": job.id, "video_id": job.video_id, "code": exc.code})
        except Exception:
            job.status, job.stage = "failed", None
            job.error_code = "processing_failed"
            job.error_message = "We couldn't process this video. Please try again later."
            log.exception("job crashed", extra={"job_id": job.id, "video_id": job.video_id})
        finally:
            job.finished_at = time.time()
            if job.status == "failed" or job.id not in self.jobs:
                # Failed jobs keep their status for polling but not their partial files.
                self._remove_files(job)

    def sweep(self) -> int:
        """Delete expired jobs and orphaned directories. Returns the number removed."""
        now = time.time()
        removed = 0
        for job in list(self.jobs.values()):
            expired = job.finished_at is not None and now - job.finished_at > self.settings.job_retention
            stuck = job.status in ACTIVE and now - job.created_at > self.settings.job_timeout * 2
            if stuck:
                job.cancel.set()
            if expired:
                self.jobs.pop(job.id, None)
                self._remove_files(job)
                removed += 1
        if self.root.is_dir():
            for child in self.root.iterdir():
                if child.name in self.jobs:
                    continue
                try:
                    age = now - child.stat().st_mtime
                except OSError:
                    continue
                if age > self.settings.job_retention:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                    removed += 1
        if removed:
            log.info("cleanup", extra={"removed": removed, "jobs": len(self.jobs)})
        return removed


# -------------------------------------------------------------------------- app


async def _cleanup_loop(manager: JobManager) -> None:
    while True:
        await asyncio.sleep(manager.settings.cleanup_interval)
        try:
            manager.sweep()
        except Exception:
            log.exception("cleanup failed")


def create_app(
    settings: Settings | None = None,
    toolchain: Toolchain | None = None,
    extract: ExtractFn | None = None,
) -> FastAPI:
    configure_logging()
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tools = toolchain or await asyncio.to_thread(Toolchain.detect)
        manager = JobManager(settings, tools, extract or extractor.extract_mp3)
        manager.prepare_storage()
        app.state.manager = manager
        log.info(
            "startup",
            extra={
                "ffmpeg": tools.ffmpeg,
                "deno": tools.deno,
                "jobs_dir": str(manager.root),
                "max_concurrent_jobs": settings.max_concurrent_jobs,
            },
        )
        if not tools.ffmpeg:
            log.error("FFmpeg not found -- conversion requests will be rejected")
        if not tools.deno:
            log.warning("Deno not found -- some YouTube videos may fail to extract")
        cleaner = asyncio.create_task(_cleanup_loop(manager))
        try:
            yield
        finally:
            cleaner.cancel()
            await manager.shutdown()

    app = FastAPI(title="Audio Downloader", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    # -- error handling: always the same JSON shape, never internal details ----

    def error_response(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
        return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)

    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError):
        return error_response(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, __: RequestValidationError):
        return error_response(422, "invalid_request", 'Send JSON like {"url": "https://youtu.be/..."}.')

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        messages = {404: "Not found.", 405: "Method not allowed."}
        return error_response(exc.status_code, "http_error", messages.get(exc.status_code, "Request failed."))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled error", exc_info=exc)
        return error_response(500, "internal_error", "Something went wrong. Please try again.")

    # -- middleware -----------------------------------------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            length = request.headers.get("content-length")
            if length is None or not length.isdigit():
                return error_response(411, "length_required", "Content-Length header is required.")
            if int(length) > settings.max_body_bytes:
                return error_response(413, "payload_too_large", "Request body is too large.")
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # -- routes ---------------------------------------------------------------

    def manager_of(request: Request) -> JobManager:
        return request.app.state.manager

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/healthz")
    async def health(request: Request):
        manager = manager_of(request)
        return {
            "status": "ok",
            "ffmpeg": bool(manager.toolchain.ffmpeg),
            "js_runtime": bool(manager.toolchain.deno),
            "active_jobs": manager.active_count(),
        }

    @app.post("/api/download", status_code=202)
    async def create_job(body: DownloadRequest, request: Request):
        try:
            video = parse_youtube_url(body.url)
        except InvalidURLError as exc:
            raise APIError(422, "invalid_url", str(exc)) from None
        job = manager_of(request).submit(video.video_id, video.canonical_url)
        return {
            "job_id": job.id,
            "status": job.status,
            "status_url": f"/api/status/{job.id}",
        }

    @app.get("/api/status/{job_id}")
    async def job_status(job_id: str, request: Request):
        manager = manager_of(request)
        return manager.get(job_id).public(manager.settings.job_retention)

    @app.get("/api/download/{job_id}")
    async def download_file(job_id: str, request: Request):
        job = manager_of(request).resolve_download(job_id)
        filename = job.filename or "audio.mp3"
        ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "").strip() or "audio.mp3"
        if not ascii_name.lower().endswith(".mp3"):
            ascii_name = "audio.mp3"
        disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"
        return FileResponse(
            job.file_path,
            media_type="audio/mpeg",
            headers={"Content-Disposition": disposition, "Cache-Control": "no-store"},
        )

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str, request: Request):
        manager_of(request).delete(job_id)
        return {"deleted": True}

    return app


app = create_app()
