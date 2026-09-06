import subprocess
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import ProxyJobs, create_app
from app.media import MediaLibrary


def make_video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=160x90:rate=10", "-t", "0.5", "-c:v", "libx264",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_streamed_upload_uses_dedicated_library_and_rejects_duplicates(tmp_path: Path) -> None:
    upload_source = tmp_path / "input.mp4"
    make_video(upload_source)
    media_dir = tmp_path / "media"
    client = TestClient(create_app(media_dir))

    with upload_source.open("rb") as handle:
        response = client.post(
            "/api/uploads",
            params={"filename": "UPLOAD001.MP4"},
            content=handle.read(),
            headers={"Content-Type": "video/mp4"},
        )
    assert response.status_code == 201, response.text
    assert response.json()["file"]["filename"] == "UPLOAD001.MP4"
    assert (media_dir / "UPLOAD001.MP4").is_file()

    with upload_source.open("rb") as handle:
        duplicate = client.post(
            "/api/uploads",
            params={"filename": "UPLOAD001.MP4"},
            content=handle.read(),
            headers={"Content-Type": "video/mp4"},
        )
    assert duplicate.status_code == 409


def test_proxy_jobs_deduplicate_a_running_source(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    source_path = media / "GXTEST01.MP4"
    source_path.write_bytes(b"placeholder")
    library = MediaLibrary(media)
    jobs = ProxyJobs(library)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def fake_ensure_proxy(source, callback):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2)
        return source.cache_dir / "proxy.mp4"

    library.ensure_proxy = fake_ensure_proxy  # type: ignore[method-assign]
    file_id = library.sources()[0].file_id
    first = jobs.start(file_id)
    assert started.wait(timeout=2)
    second = jobs.start(file_id)
    release.set()

    assert first["state"] in {"queued", "generating"}
    assert second["state"] in {"queued", "generating"}
    assert calls == 1
