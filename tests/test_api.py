import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from extractor import Cancelled, ExtractionError, ExtractionResult, Toolchain
from main import Settings, create_app

VALID_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
FAKE_MP3 = b"ID3" + b"\x00" * 64 + b"fake-mp3-audio"


def fake_extract(title="Test Song"):
    def extract(url, workdir: Path, toolchain, limits, cancel, progress):
        progress(stage="downloading", title=title, downloaded=10, total=100)
        out = workdir / "audio.mp3"
        out.write_bytes(FAKE_MP3)
        return ExtractionResult(path=out, title=title, duration=60, filesize=len(FAKE_MP3))

    return extract


def blocking_extract(release: threading.Event, started: threading.Event):
    """Simulates a long download that stops when released or cancelled."""

    def extract(url, workdir, toolchain, limits, cancel, progress):
        progress(stage="downloading", downloaded=25, total=100)
        (workdir / "audio.webm.part").write_bytes(b"partial")
        started.set()
        while not release.is_set():
            if cancel.is_set():
                raise Cancelled()
            time.sleep(0.01)
        out = workdir / "audio.mp3"
        out.write_bytes(FAKE_MP3)
        return ExtractionResult(path=out, title="Slow", duration=1, filesize=len(FAKE_MP3))

    return extract


def failing_extract(exc: Exception):
    def extract(url, workdir, toolchain, limits, cancel, progress):
        (workdir / "audio.webm.part").write_bytes(b"partial")
        raise exc

    return extract


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def factory(extract=None, ffmpeg="ffmpeg", **overrides):
        settings = Settings(jobs_dir=tmp_path / "jobs", **overrides)
        app = create_app(settings, Toolchain(ffmpeg=ffmpeg, deno=None), extract or fake_extract())
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


def wait_for(client, job_id, statuses=("completed", "failed"), timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/status/{job_id}").json()
        if data.get("status") in statuses:
            return data
        time.sleep(0.02)
    raise AssertionError(f"job did not reach {statuses}: {data}")


def submit(client, url=VALID_URL):
    res = client.post("/api/download", json={"url": url})
    assert res.status_code == 202, res.text
    return res.json()["job_id"]


# ------------------------------------------------------------------ basic pages


def test_index_served_with_security_headers(make_client):
    client = make_client()
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "Audio Downloader" in res.text
    assert res.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]


def test_healthz_reports_toolchain(make_client):
    assert make_client().get("/healthz").json() == {
        "status": "ok",
        "ffmpeg": True,
        "js_runtime": False,
        "active_jobs": 0,
    }


def test_unknown_route_returns_json_error(make_client):
    res = make_client().get("/nope")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "http_error"


# ------------------------------------------------------------ request validation


def test_successful_job_flow(make_client):
    client = make_client()
    res = client.post("/api/download", json={"url": VALID_URL})
    assert res.status_code == 202
    body = res.json()
    assert len(body["job_id"]) == 32 and body["status"] in ("queued", "processing")
    assert body["status_url"] == f"/api/status/{body['job_id']}"

    data = wait_for(client, body["job_id"])
    assert data["status"] == "completed"
    assert data["filename"] == "Test Song.mp3"
    assert data["filesize"] == len(FAKE_MP3)
    assert data["download_url"] == f"/api/download/{body['job_id']}"
    assert data["error"] is None
    assert 0 < data["expires_in"] <= 15 * 60


def test_canonical_url_passed_to_extractor(make_client):
    seen = {}

    def extract(url, workdir, *args):
        seen["url"] = url
        return fake_extract()(url, workdir, *args)

    client = make_client(extract=extract)
    job_id = submit(client, "youtu.be/dQw4w9WgXcQ?si=tracking&list=PL1")
    wait_for(client, job_id)
    assert seen["url"] == VALID_URL


@pytest.mark.parametrize(
    "url",
    ["not a url", "https://evil.com/watch?v=dQw4w9WgXcQ", "http://169.254.169.254/", "https://youtu.be/"],
)
def test_invalid_url_rejected(make_client, url):
    res = make_client().post("/api/download", json={"url": url})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_url"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"json": {}},
        {"json": {"link": VALID_URL}},
        {"json": {"url": 123}},
        {"json": [VALID_URL]},
        {"content": b"not json", "headers": {"content-type": "application/json"}},
    ],
)
def test_malformed_body_rejected(make_client, kwargs):
    res = make_client().post("/api/download", **kwargs)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_request"


def test_oversized_body_rejected(make_client):
    res = make_client().post("/api/download", json={"url": VALID_URL + "&x=" + "a" * 5000})
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "payload_too_large"


def test_concurrency_limit(make_client):
    release, started = threading.Event(), threading.Event()
    client = make_client(extract=blocking_extract(release, started), max_pending_jobs=1)
    submit(client)
    res = client.post("/api/download", json={"url": VALID_URL})
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "server_busy"
    assert res.headers["retry-after"] == "60"
    release.set()


def test_jobs_wait_for_a_free_slot(make_client):
    release, started = threading.Event(), threading.Event()
    client = make_client(extract=blocking_extract(release, started), max_concurrent_jobs=1)
    first = submit(client)
    assert started.wait(2)
    second = submit(client)
    assert client.get(f"/api/status/{second}").json()["status"] == "queued"
    status = client.get(f"/api/status/{first}").json()
    assert status["status"] == "processing" and status["progress"] == 25.0
    release.set()
    assert wait_for(client, first)["status"] == "completed"
    assert wait_for(client, second)["status"] == "completed"


# ----------------------------------------------------------------- job IDs


@pytest.mark.parametrize("job_id", ["abc", "0" * 31, "g" * 32, "A" * 32, "0" * 33, "..%2F..%2Fetc%2Fpasswd"])
def test_invalid_job_ids(make_client, job_id):
    client = make_client()
    for method, path in (
        ("GET", f"/api/status/{job_id}"),
        ("GET", f"/api/download/{job_id}"),
        ("DELETE", f"/api/jobs/{job_id}"),
    ):
        res = client.request(method, path)
        assert res.status_code in (400, 404), (method, path)
        assert "error" in res.json()


def test_unknown_job_id_is_not_found(make_client):
    client = make_client()
    res = client.get("/api/status/" + "a" * 32)
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "job_not_found"
    assert client.get("/api/download/" + "a" * 32).status_code == 404


def test_path_traversal_segments_never_reach_files(make_client):
    client = make_client()
    for path in ("/api/download/../../main.py", "/api/download/%2e%2e%2fmain.py", "/static/../main.py"):
        res = client.get(path)
        assert res.status_code in (400, 404)
        assert b"create_app" not in res.content


# --------------------------------------------------------------- failures


def test_processing_failure_reported_and_files_removed(make_client, tmp_path):
    exc = ExtractionError("video_unavailable", "This video is unavailable. Check the link and try again.")
    client = make_client(extract=failing_extract(exc))
    job_id = submit(client)
    data = wait_for(client, job_id)
    assert data["status"] == "failed"
    assert data["error"] == {"code": "video_unavailable", "message": exc.message}
    assert data["download_url"] is None
    assert not (tmp_path / "jobs" / job_id).exists()
    res = client.get(f"/api/download/{job_id}")
    assert res.status_code == 409


def test_timeout_reported(make_client):
    exc = ExtractionError("timeout", "Processing took too long and was stopped. Try a shorter video.")
    client = make_client(extract=failing_extract(exc))
    data = wait_for(client, submit(client))
    assert data["error"]["code"] == "timeout"


def test_unexpected_crash_hides_internals(make_client):
    client = make_client(extract=failing_extract(RuntimeError("secret /srv/path stack detail")))
    data = wait_for(client, submit(client))
    assert data["status"] == "failed"
    assert data["error"]["code"] == "processing_failed"
    assert "secret" not in data["error"]["message"] and "/srv" not in data["error"]["message"]


def test_missing_ffmpeg_rejects_jobs(make_client):
    client = make_client(ffmpeg=None)
    assert client.get("/healthz").json()["ffmpeg"] is False
    res = client.post("/api/download", json={"url": VALID_URL})
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "ffmpeg_unavailable"


def test_download_before_completion_conflicts(make_client):
    release, started = threading.Event(), threading.Event()
    client = make_client(extract=blocking_extract(release, started))
    job_id = submit(client)
    assert started.wait(2)
    res = client.get(f"/api/download/{job_id}")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "not_ready"
    release.set()


# --------------------------------------------------------------- downloads


def test_completed_download_response(make_client):
    client = make_client()
    job_id = submit(client)
    wait_for(client, job_id)
    res = client.get(f"/api/download/{job_id}")
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/mpeg"
    assert res.headers["content-disposition"] == (
        "attachment; filename=\"Test Song.mp3\"; filename*=UTF-8''Test%20Song.mp3"
    )
    assert res.headers["cache-control"] == "no-store"
    assert res.content == FAKE_MP3


def test_unicode_and_hostile_titles_get_safe_disposition(make_client):
    client = make_client(extract=fake_extract(title='Café "Live"/..\\evil\r\nX-Injected: 1'))
    job_id = submit(client)
    data = wait_for(client, job_id)
    assert "/" not in data["filename"] and "\\" not in data["filename"] and '"' not in data["filename"]
    res = client.get(f"/api/download/{job_id}")
    disposition = res.headers["content-disposition"]
    assert "x-injected" not in {k.lower() for k in res.headers}
    assert "\r" not in disposition and "\n" not in disposition
    assert "filename*=UTF-8''Caf%C3%A9" in disposition
    assert 'filename="Caf Live .. evil X-Injected 1.mp3"' in disposition


def test_file_outside_jobs_dir_is_never_served(make_client, tmp_path):
    client = make_client()
    job_id = submit(client)
    wait_for(client, job_id)
    outside = tmp_path / "secret.mp3"
    outside.write_bytes(b"secret")
    client.app.state.manager.jobs[job_id].file_path = outside
    res = client.get(f"/api/download/{job_id}")
    assert res.status_code == 410
    assert b"secret" not in res.content


def test_missing_file_returns_gone(make_client):
    client = make_client()
    job_id = submit(client)
    wait_for(client, job_id)
    os.remove(client.app.state.manager.jobs[job_id].file_path)
    res = client.get(f"/api/download/{job_id}")
    assert res.status_code == 410
    assert res.json()["error"]["code"] == "file_gone"


# ----------------------------------------------------------------- cleanup


def test_delete_completed_job_removes_files(make_client, tmp_path):
    client = make_client()
    job_id = submit(client)
    wait_for(client, job_id)
    workdir = tmp_path / "jobs" / job_id
    assert (workdir / "audio.mp3").exists()
    res = client.delete(f"/api/jobs/{job_id}")
    assert res.status_code == 200 and res.json() == {"deleted": True}
    assert not workdir.exists()
    assert client.get(f"/api/status/{job_id}").status_code == 404
    assert client.delete(f"/api/jobs/{job_id}").status_code == 404


def test_delete_running_job_cancels_and_cleans_up(make_client, tmp_path):
    release, started = threading.Event(), threading.Event()
    client = make_client(extract=blocking_extract(release, started))
    job_id = submit(client)
    assert started.wait(2)
    workdir = tmp_path / "jobs" / job_id
    assert workdir.exists()
    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    deadline = time.time() + 3
    while workdir.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert not workdir.exists()
    assert client.get("/healthz").json()["active_jobs"] == 0


def test_delete_queued_job(make_client, tmp_path):
    release, started = threading.Event(), threading.Event()
    client = make_client(extract=blocking_extract(release, started), max_concurrent_jobs=1)
    submit(client)
    assert started.wait(2)
    queued = submit(client)
    assert client.delete(f"/api/jobs/{queued}").status_code == 200
    deadline = time.time() + 3
    while (tmp_path / "jobs" / queued).exists() and time.time() < deadline:
        time.sleep(0.02)
    assert not (tmp_path / "jobs" / queued).exists()
    release.set()


def test_sweep_removes_expired_jobs_and_orphans(make_client, tmp_path):
    client = make_client(job_retention=60)
    manager = client.app.state.manager
    job_id = submit(client)
    wait_for(client, job_id)
    orphan = tmp_path / "jobs" / "orphan"
    orphan.mkdir()
    old = time.time() - 3600
    os.utime(orphan, (old, old))

    assert manager.sweep() == 1  # only the orphan; job is still within retention
    assert not orphan.exists()
    assert (tmp_path / "jobs" / job_id).exists()

    manager.jobs[job_id].finished_at = time.time() - 61
    assert manager.sweep() == 1
    assert not (tmp_path / "jobs" / job_id).exists()
    assert client.get(f"/api/status/{job_id}").status_code == 404


def test_startup_wipes_stale_files_and_shutdown_cleans(tmp_path):
    jobs_dir = tmp_path / "jobs"
    (jobs_dir / "stale").mkdir(parents=True)
    (jobs_dir / "stale" / "audio.mp3").write_bytes(b"old")
    app = create_app(Settings(jobs_dir=jobs_dir), Toolchain("ffmpeg", None), fake_extract())
    with TestClient(app) as client:
        assert not (jobs_dir / "stale").exists()
        wait_for(client, submit(client))
    assert not jobs_dir.exists()
