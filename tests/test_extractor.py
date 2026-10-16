import json
import sys
import threading
import time
from pathlib import Path

import pytest

import extractor
from extractor import (
    Cancelled,
    ExtractionError,
    Limits,
    ProcessResult,
    Toolchain,
    classify_error,
    download_audio,
    extract_mp3,
    probe,
    run_command,
)

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
LIMITS = Limits(max_duration=600, max_filesize_mb=50, probe_timeout=5, job_timeout=30)
TOOLS = Toolchain(ffmpeg="/opt/ffmpeg", deno="/opt/deno")


# -------------------------------------------------------------- classification


@pytest.mark.parametrize(
    "stderr, code",
    [
        ("ERROR: [youtube] abc: Video unavailable", "video_unavailable"),
        ("ERROR: [youtube] aaaaaaaaaaa: This video is unavailable", "video_unavailable"),
        ("ERROR: [youtube] abc: This video has been removed by the uploader", "video_unavailable"),
        ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", "private_video"),
        ("ERROR: [youtube] abc: Sign in to confirm your age.", "age_restricted"),
        ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot.", "temporarily_blocked"),
        ("ERROR: unable to download webpage: HTTP Error 429: Too Many Requests", "temporarily_blocked"),
        ("ERROR: [youtube] abc: Join this channel to get access to members-only content", "access_restricted"),
        ("ERROR: [youtube] abc: This video is DRM protected", "drm_protected"),
        ("ERROR: [youtube] abc: The uploader has not made this video available in your country", "geo_restricted"),
        ("ERROR: [youtube] abc: This live event will begin in 3 hours.", "live_stream"),
        ("ERROR: something entirely new went wrong", "processing_failed"),
        ("", "processing_failed"),
    ],
)
def test_classify_error(stderr, code):
    err = classify_error(stderr)
    assert err.code == code
    assert "ERROR" not in err.message and "[youtube]" not in err.message


def test_classify_prefers_error_lines_over_warnings():
    stderr = "WARNING: [youtube] Sign in to confirm your age fallback\nERROR: [youtube] abc: Private video"
    assert classify_error(stderr).code == "private_video"


# ------------------------------------------------------------ process runner


def test_run_command_captures_output(tmp_path):
    lines = []
    result = run_command(
        [sys.executable, "-c", "import sys; print('a'); print('b'); print('err', file=sys.stderr)"],
        timeout=10,
        cwd=tmp_path,
        cancel=threading.Event(),
        on_line=lines.append,
    )
    assert result.returncode == 0
    assert lines == ["a", "b"]
    assert result.stdout.split() == ["a", "b"]
    assert "err" in result.stderr


def test_run_command_does_not_use_a_shell(tmp_path):
    result = run_command(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "$(echo pwned); rm -rf /"],
        timeout=10,
        cwd=tmp_path,
        cancel=threading.Event(),
    )
    assert result.stdout.strip() == "$(echo pwned); rm -rf /"


def test_run_command_times_out(tmp_path):
    started = time.monotonic()
    with pytest.raises(ExtractionError) as info:
        run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.5,
            cwd=tmp_path,
            cancel=threading.Event(),
        )
    assert info.value.code == "timeout"
    assert time.monotonic() - started < 10


def test_run_command_cancel(tmp_path):
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=30,
            cwd=tmp_path,
            cancel=cancel,
        )
    assert time.monotonic() - started < 10


# ------------------------------------------------------------------- probe


def fake_runner(monkeypatch, *, returncode=0, stdout="", stderr="", lines=(), on_call=None):
    calls = []

    def run(args, *, timeout, cwd, cancel, on_line=None, keep_stdout=True):
        calls.append(args)
        if on_call:
            on_call(args, cwd)
        for line in lines:
            on_line(line)
        return ProcessResult(returncode, stdout, stderr)

    monkeypatch.setattr(extractor, "run_command", run)
    return calls


def test_probe_builds_safe_command_and_saves_info(monkeypatch, tmp_path):
    info = {"_type": "video", "id": "dQw4w9WgXcQ", "title": "Song", "duration": 200, "live_status": "not_live"}
    calls = fake_runner(monkeypatch, stdout=json.dumps(info))
    assert probe(URL, tmp_path, TOOLS, LIMITS, threading.Event())["title"] == "Song"
    args = calls[0]
    assert args[:3] == [sys.executable, "-m", "yt_dlp"]
    assert "--ignore-config" in args and "--no-playlist" in args
    assert args[-2:] == ["--", URL]  # URL can never be parsed as an option
    assert args[args.index("--js-runtimes") + 1] == "deno:/opt/deno"
    assert json.loads((tmp_path / "info.json").read_text())["id"] == "dQw4w9WgXcQ"


def test_probe_unavailable_video(monkeypatch, tmp_path):
    fake_runner(monkeypatch, returncode=1, stderr="ERROR: [youtube] x: Video unavailable")
    with pytest.raises(ExtractionError) as info:
        probe(URL, tmp_path, TOOLS, LIMITS, threading.Event())
    assert info.value.code == "video_unavailable"


@pytest.mark.parametrize(
    "info, code",
    [
        ({"is_live": True}, "live_stream"),
        ({"live_status": "is_upcoming"}, "live_stream"),
        ({"availability": "subscriber_only"}, "access_restricted"),
        ({"duration": 601}, "too_long"),
        ({"_type": "playlist"}, "video_unavailable"),
    ],
)
def test_probe_policy_rejections(monkeypatch, tmp_path, info, code):
    fake_runner(monkeypatch, stdout=json.dumps({"_type": "video", "title": "x", **info}))
    with pytest.raises(ExtractionError) as exc:
        probe(URL, tmp_path, TOOLS, LIMITS, threading.Event())
    assert exc.value.code == code


def test_probe_invalid_json(monkeypatch, tmp_path):
    fake_runner(monkeypatch, stdout="<html>not json")
    with pytest.raises(ExtractionError) as exc:
        probe(URL, tmp_path, TOOLS, LIMITS, threading.Event())
    assert exc.value.code == "processing_failed"


# ----------------------------------------------------------------- download


def test_download_reports_progress_and_returns_mp3(monkeypatch, tmp_path):
    (tmp_path / "info.json").write_text("{}")
    events = []
    calls = fake_runner(
        monkeypatch,
        lines=[
            "[ytmp3-progress] 1024 4096 downloading",
            "[ytmp3-progress] 4096 4096 finished",
            "[ytmp3-progress] 10 NA downloading",
            "[ExtractAudio] Destination: audio.mp3",
        ],
        on_call=lambda args, cwd: (cwd / "audio.mp3").write_bytes(b"mp3"),
    )
    out = download_audio(tmp_path, TOOLS, LIMITS, threading.Event(), 30, lambda **k: events.append(k))
    assert out == tmp_path / "audio.mp3"
    assert events == [
        {"stage": "downloading", "downloaded": 1024, "total": 4096},
        {"stage": "downloading", "downloaded": 4096, "total": 4096},
        {"stage": "downloading", "downloaded": 10, "total": None},
        {"stage": "converting"},
    ]
    args = calls[0]
    assert args[args.index("--audio-format") + 1] == "mp3"
    assert args[args.index("--ffmpeg-location") + 1] == "/opt/ffmpeg"
    assert args[args.index("--max-filesize") + 1] == "50M"


def test_download_without_output_fails(monkeypatch, tmp_path):
    fake_runner(monkeypatch)
    with pytest.raises(ExtractionError) as exc:
        download_audio(tmp_path, TOOLS, LIMITS, threading.Event(), 30, lambda **k: None)
    assert exc.value.code == "processing_failed"


def test_download_too_large(monkeypatch, tmp_path):
    fake_runner(monkeypatch, stderr="[download] File is larger than max-filesize (60000000 bytes > 50M). Skipping")
    with pytest.raises(ExtractionError) as exc:
        download_audio(tmp_path, TOOLS, LIMITS, threading.Event(), 30, lambda **k: None)
    assert exc.value.code == "too_large"


def test_extract_requires_ffmpeg(monkeypatch, tmp_path):
    calls = fake_runner(monkeypatch)
    with pytest.raises(ExtractionError) as exc:
        extract_mp3(URL, tmp_path, Toolchain(ffmpeg=None, deno=None), LIMITS, threading.Event(), lambda **k: None)
    assert exc.value.code == "ffmpeg_unavailable"
    assert calls == []  # nothing was downloaded


def test_extract_full_pipeline_mocked(monkeypatch, tmp_path):
    info = {"_type": "video", "title": "Song", "duration": 12.5}

    def on_call(args, cwd):
        if "--load-info-json" in args:
            (cwd / "audio.mp3").write_bytes(b"x" * 10)

    fake_runner(monkeypatch, stdout=json.dumps(info), on_call=on_call)
    result = extract_mp3(URL, tmp_path, TOOLS, LIMITS, threading.Event(), lambda **k: None)
    assert (result.title, result.duration, result.filesize) == ("Song", 12, 10)


# ---------------------------------------------------------------- toolchain


def test_find_ffmpeg_missing(monkeypatch):
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    monkeypatch.setattr(extractor.shutil, "which", lambda name: None)
    import imageio_ffmpeg

    def no_binary():
        raise RuntimeError("no ffmpeg for this platform")

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", no_binary)
    assert extractor.find_ffmpeg() is None


def test_find_ffmpeg_ignores_broken_binary(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg"
    fake.write_text("not a binary")
    monkeypatch.setenv("FFMPEG_PATH", str(fake))
    monkeypatch.setattr(extractor.shutil, "which", lambda name: None)
    import imageio_ffmpeg

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: str(tmp_path / "missing"))
    assert extractor.find_ffmpeg() is None
