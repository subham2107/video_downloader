"""yt-dlp + FFmpeg audio extraction.

Everything here is synchronous and is run from a worker thread by the job manager.
External programs are always started with an argument list (never a shell), with a
hard deadline and a cancellation flag that kills the whole process tree.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("ytmp3.extractor")

OUTPUT_STEM = "audio"
INFO_FILE = "info.json"
PROGRESS_PREFIX = "[ytmp3-progress]"


# --------------------------------------------------------------------------- errors


class ExtractionError(Exception):
    """A failure with a stable machine-readable code and a user-safe message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Cancelled(Exception):
    """Raised when a job is cancelled while an external process is running."""


GENERIC_FAILURE = ExtractionError(
    "processing_failed", "We couldn't process this video. Please try again later."
)

# Ordered (pattern, code, message) rules applied to yt-dlp's stderr.
_ERROR_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"confirm you.?re not a bot|http error 429|too many requests", re.I),
        "temporarily_blocked",
        "YouTube is temporarily refusing requests from this server. Please try again later.",
    ),
    (re.compile(r"private video", re.I), "private_video", "This video is private."),
    (
        re.compile(r"confirm your age|age[- ]restricted|inappropriate for some users", re.I),
        "age_restricted",
        "This video is age-restricted and can't be downloaded without signing in.",
    ),
    (
        re.compile(r"members[- ]only|join this channel|requires payment|premium", re.I),
        "access_restricted",
        "This video requires a membership or purchase and can't be downloaded.",
    ),
    (re.compile(r"\bdrm\b", re.I), "drm_protected", "This video is DRM-protected and can't be downloaded."),
    (
        re.compile(r"available in your country|geo[- ]?restrict", re.I),
        "geo_restricted",
        "This video isn't available in the region this service runs in.",
    ),
    (
        re.compile(r"live event|is live|premieres? in|is_live|post_live", re.I),
        "live_stream",
        "Live streams and upcoming premieres can't be converted.",
    ),
    (
        re.compile(r"sign in|login required|cookies", re.I),
        "login_required",
        "This video requires signing in and can't be downloaded.",
    ),
    (
        re.compile(
            r"\bunavailable\b|not available|has been removed|does not exist|"
            r"account .* terminated|no longer available|incomplete youtube id|"
            r"requested format is not available",
            re.I,
        ),
        "video_unavailable",
        "This video is unavailable. Check the link and try again.",
    ),
]


def classify_error(stderr: str) -> ExtractionError:
    """Map yt-dlp error output to a user-safe ExtractionError."""
    # Prefer explicit ERROR lines so warnings don't produce false matches.
    errors = "\n".join(line for line in stderr.splitlines() if "ERROR" in line) or stderr
    for pattern, code, message in _ERROR_RULES:
        if pattern.search(errors):
            return ExtractionError(code, message)
    return ExtractionError(GENERIC_FAILURE.code, GENERIC_FAILURE.message)


# ------------------------------------------------------------------------ toolchain


def _runs(cmd: list[str]) -> bool:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def find_ffmpeg() -> str | None:
    """Locate a working FFmpeg binary.

    Order: ``FFMPEG_PATH`` env var, ``ffmpeg`` on PATH, then the static build bundled
    with the ``imageio-ffmpeg`` wheel (which is how it is provisioned on Render).
    """
    candidates: list[str | None] = [os.environ.get("FFMPEG_PATH"), shutil.which("ffmpeg")]
    try:
        import imageio_ffmpeg

        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # package missing or no binary for this platform
        pass
    for path in candidates:
        if path and Path(path).is_file() and _runs([path, "-hide_banner", "-version"]):
            return path
    return None


def find_deno() -> str | None:
    """Locate Deno, which yt-dlp uses to solve YouTube's JavaScript challenges."""
    candidates: list[str | None] = [os.environ.get("DENO_PATH"), shutil.which("deno")]
    try:
        import deno

        candidates.append(deno.find_deno_bin())
    except Exception:
        pass
    for path in candidates:
        if path and Path(path).is_file() and _runs([path, "--version"]):
            return path
    return None


@dataclass(frozen=True)
class Toolchain:
    ffmpeg: str | None
    deno: str | None

    @classmethod
    def detect(cls) -> "Toolchain":
        return cls(ffmpeg=find_ffmpeg(), deno=find_deno())


# ------------------------------------------------------------------ process runner


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_command(
    args: list[str],
    *,
    timeout: float,
    cwd: Path,
    cancel: threading.Event,
    on_line: Callable[[str], None] | None = None,
    keep_stdout: bool = True,
) -> ProcessResult:
    """Run ``args`` without a shell, enforcing ``timeout`` and honouring ``cancel``.

    Raises ``ExtractionError('timeout', ...)`` on timeout and ``Cancelled`` on cancel.
    """
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True  # own process group -> killpg works
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs,
    )
    stdout_chunks: list[str] = []
    stderr_tail: deque[str] = deque(maxlen=200)

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if keep_stdout:
                stdout_chunks.append(line)
            if on_line is not None:
                try:
                    on_line(line.rstrip("\r\n"))
                except Exception:  # never let a progress callback kill the reader
                    log.exception("progress callback failed")

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_tail.append(line)

    readers = [threading.Thread(target=fn, daemon=True) for fn in (read_stdout, read_stderr)]
    for t in readers:
        t.start()

    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel.is_set():
                _kill_tree(proc)
                raise Cancelled()
            if time.monotonic() >= deadline:
                _kill_tree(proc)
                raise ExtractionError(
                    "timeout", "Processing took too long and was stopped. Try a shorter video."
                )
    finally:
        if proc.poll() is None:
            _kill_tree(proc)
        for t in readers:
            t.join(timeout=5)

    return ProcessResult(proc.returncode, "".join(stdout_chunks), "".join(stderr_tail))


# ------------------------------------------------------------------- extraction


@dataclass(frozen=True)
class Limits:
    max_duration: int  # seconds of video
    max_filesize_mb: int
    probe_timeout: float
    job_timeout: float  # total wall clock for probe + download + conversion
    audio_quality: str = "192K"


@dataclass
class ExtractionResult:
    path: Path
    title: str | None
    duration: int | None
    filesize: int


ProgressCallback = Callable[..., None]


def _base_args(toolchain: Toolchain) -> list[str]:
    args = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-cache-dir",
        "--no-playlist",
        "--no-color",
        "--socket-timeout",
        "20",
        "--retries",
        "3",
    ]
    if toolchain.deno:
        args += ["--js-runtimes", f"deno:{toolchain.deno}"]
    return args


def _parse_int(value: str) -> int | None:
    try:
        return int(float(value))
    except ValueError:
        return None


def probe(
    video_url: str, workdir: Path, toolchain: Toolchain, limits: Limits, cancel: threading.Event
) -> dict:
    """Fetch video metadata, enforce policy limits and save it for the download step."""
    args = _base_args(toolchain) + ["--dump-single-json", "--skip-download", "--", video_url]
    result = run_command(args, timeout=limits.probe_timeout, cwd=workdir, cancel=cancel)
    if result.returncode != 0:
        log.warning("probe failed", extra={"stderr_tail": result.stderr[-2000:]})
        raise classify_error(result.stderr)
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("probe returned invalid JSON")
        raise ExtractionError(GENERIC_FAILURE.code, GENERIC_FAILURE.message) from None
    if not isinstance(info, dict) or info.get("_type", "video") != "video":
        raise ExtractionError("video_unavailable", "This link doesn't point to a single video.")

    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise ExtractionError("live_stream", "Live streams and upcoming premieres can't be converted.")
    if info.get("availability") in ("needs_auth", "premium_only", "subscriber_only"):
        raise ExtractionError(
            "access_restricted", "This video requires signing in or a membership and can't be downloaded."
        )
    duration = info.get("duration")
    if isinstance(duration, (int, float)) and duration > limits.max_duration:
        raise ExtractionError(
            "too_long",
            f"This video is longer than the {limits.max_duration // 60}-minute limit.",
        )

    (workdir / INFO_FILE).write_text(json.dumps(info), encoding="utf-8")
    return info


def download_audio(
    workdir: Path,
    toolchain: Toolchain,
    limits: Limits,
    cancel: threading.Event,
    timeout: float,
    progress: ProgressCallback,
) -> Path:
    """Download the best audio stream from the probed info and convert it to MP3."""
    assert toolchain.ffmpeg is not None
    template = (
        f"download:{PROGRESS_PREFIX} %(progress.downloaded_bytes)s "
        "%(progress.total_bytes)s %(progress.status)s"
    )
    args = _base_args(toolchain) + [
        "--load-info-json",
        str(workdir / INFO_FILE),
        "--format",
        "bestaudio/best",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        limits.audio_quality,
        "--ffmpeg-location",
        toolchain.ffmpeg,
        "--max-filesize",
        f"{limits.max_filesize_mb}M",
        "--newline",
        "--progress-template",
        template,
        "--no-mtime",
        "--paths",
        str(workdir),
        "--output",
        f"{OUTPUT_STEM}.%(ext)s",
    ]

    def on_line(line: str) -> None:
        if line.startswith(PROGRESS_PREFIX):
            fields = line[len(PROGRESS_PREFIX) :].split()
            if len(fields) >= 2:
                progress(
                    stage="downloading",
                    downloaded=_parse_int(fields[0]),
                    total=_parse_int(fields[1]),
                )
        elif line.startswith("[ExtractAudio]"):
            progress(stage="converting")

    result = run_command(
        args, timeout=timeout, cwd=workdir, cancel=cancel, on_line=on_line, keep_stdout=False
    )
    if result.returncode != 0:
        log.warning("download failed", extra={"stderr_tail": result.stderr[-2000:]})
        raise classify_error(result.stderr)

    output = workdir / f"{OUTPUT_STEM}.mp3"
    if not output.is_file() or output.stat().st_size == 0:
        if "max-filesize" in result.stderr.lower() or "larger than max" in result.stderr.lower():
            raise ExtractionError(
                "too_large", f"The audio is larger than the {limits.max_filesize_mb} MB limit."
            )
        log.warning("download produced no mp3", extra={"stderr_tail": result.stderr[-2000:]})
        raise ExtractionError(GENERIC_FAILURE.code, GENERIC_FAILURE.message)
    return output


def extract_mp3(
    video_url: str,
    workdir: Path,
    toolchain: Toolchain,
    limits: Limits,
    cancel: threading.Event,
    progress: ProgressCallback,
) -> ExtractionResult:
    """Full pipeline: probe -> download best audio -> convert to MP3."""
    if not toolchain.ffmpeg:
        raise ExtractionError(
            "ffmpeg_unavailable", "Audio conversion is temporarily unavailable on this server."
        )
    started = time.monotonic()
    progress(stage="fetching_info")
    info = probe(video_url, workdir, toolchain, limits, cancel)
    progress(stage="downloading", title=info.get("title"))

    remaining = limits.job_timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise ExtractionError("timeout", "Processing took too long and was stopped. Try a shorter video.")
    path = download_audio(workdir, toolchain, limits, cancel, remaining, progress)

    duration = info.get("duration")
    return ExtractionResult(
        path=path,
        title=info.get("title"),
        duration=int(duration) if isinstance(duration, (int, float)) else None,
        filesize=path.stat().st_size,
    )
