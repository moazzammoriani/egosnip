import hashlib
import subprocess
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import ProxyJobs, create_app
from app.identity import SourceIdentity
from app.media import MediaLibrary, Source


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


def install_fake_gopro_identity(app) -> None:
    library = app.state.library

    def fake_identity(
        path: Path,
        filename: str,
        *,
        use_cache: bool = True,
        write_cache: bool = True,
    ) -> SourceIdentity:
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
        recording_id = fingerprint[:8].upper()
        return SourceIdentity(
            device_id="GP-000001",
            camera_serial_number="C0000000000001",
            camera_model="Synthetic GoPro",
            camera_firmware="TEST",
            source_file=filename,
            source_file_stem=Path(filename).stem,
            recording_id=recording_id,
            recording_fingerprint=fingerprint,
            source_id=f"GP-000001_{Path(filename).stem}_{recording_id}",
            file_id=fingerprint[:20],
        )

    library._identity_for = fake_identity


def test_streamed_upload_uses_dedicated_library_and_rejects_duplicates(tmp_path: Path) -> None:
    upload_source = tmp_path / "input.mp4"
    make_video(upload_source)
    media_dir = tmp_path / "media"
    app = create_app(media_dir)
    install_fake_gopro_identity(app)
    client = TestClient(app)

    with upload_source.open("rb") as handle:
        response = client.post(
            "/api/uploads",
            params={"filename": "UPLOAD001.MP4"},
            content=handle.read(),
            headers={"Content-Type": "video/mp4"},
        )
    assert response.status_code == 201, response.text
    assert response.json()["file"]["filename"] == "UPLOAD001.MP4"
    uploaded = response.json()["file"]
    stored = media_dir / "sources" / uploaded["device_id"] / uploaded["source_id"] / "UPLOAD001.MP4"
    assert stored.is_file()

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
    source = Source(
        file_id="a" * 20,
        filename="GXTEST01.MP4",
        source_id="GP-000001_GXTEST01_A72F91C3",
        source_file_stem="GXTEST01",
        recording_id="A72F91C3",
        recording_fingerprint="a" * 64,
        device_id="GP-000001",
        camera_serial_number="C0000000000001",
        camera_model="Synthetic GoPro",
        camera_firmware="TEST",
        path=source_path,
        cache_dir=media / ".snipper_cache" / "source",
    )
    library.get = lambda _: source  # type: ignore[method-assign]
    file_id = source.file_id
    first = jobs.start(file_id)
    assert started.wait(timeout=2)
    second = jobs.start(file_id)
    release.set()

    assert first["state"] in {"queued", "generating"}
    assert second["state"] in {"queued", "generating"}
    assert calls == 1


def test_project_api_requires_persists_and_edits_project_name(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    make_video(media / "GXTEST01.MP4")
    app = create_app(media)
    install_fake_gopro_identity(app)
    client = TestClient(app)
    file_id = client.get("/api/files").json()["files"][0]["id"]

    initial = client.get(f"/api/projects/{file_id}").json()
    assert initial["project_name"] == ""

    missing_name = dict(initial)
    missing_name.pop("project_name")
    assert client.put(f"/api/projects/{file_id}", json=missing_name).status_code == 422

    initial["project_name"] = "Claru Textile Pilot"
    assert client.put(f"/api/projects/{file_id}", json=initial).status_code == 200
    assert client.get(f"/api/projects/{file_id}").json()["project_name"] == "Claru Textile Pilot"

    initial["project_name"] = "Claru Production"
    assert client.put(f"/api/projects/{file_id}", json=initial).status_code == 200
    reloaded = client.get(f"/api/projects/{file_id}").json()
    assert reloaded["project_name"] == "Claru Production"
