import hashlib
import json
from pathlib import Path

from app.identity import SourceIdentity
from app.media import MediaLibrary, Source
from app.migrate_identity import migrate_legacy_source


def make_source(library: MediaLibrary) -> Source:
    identity = SourceIdentity(
        device_id="GP-000001",
        camera_serial_number="C3531325778769",
        camera_model="HERO13 Black",
        camera_firmware="H24.01.02.10.00",
        source_file="GX010078.MP4",
        source_file_stem="GX010078",
        recording_id="DF23F8AB",
        recording_fingerprint="df23f8ab" + ("c" * 56),
        source_id="GP-000001_GX010078_DF23F8AB",
        file_id="df23f8ab" + ("c" * 12),
    )
    return library._source_from_identity(library.media_dir / identity.source_file, identity)


def test_migrates_project_cache_and_exports_without_changing_media_payloads(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "media")
    source = make_source(library)
    old_file_id = hashlib.sha256(source.filename.encode("utf-8")).hexdigest()
    old_cache = library.cache_root / f"GX010078_{old_file_id[:8]}"
    old_cache.mkdir(parents=True)
    (old_cache / "proxy.mp4").write_bytes(b"proxy bytes")
    (old_cache / "project.json").write_text(json.dumps({
        "project_name": "Claru Pilot",
        "source_file": source.filename,
        "source_id": "GX010078",
        "imu_settings": {"retain_one_pre_zero_sample": False},
        "clips": [{
            "id": "GX010078_001",
            "clip_index": 1,
            "task_label": "Sewing pillowcases",
            "requested_start_s": 14.0,
            "requested_end_s": 414.7,
        }],
        "next_clip_index": 2,
    }), encoding="utf-8")

    old_clip = library.exports_root / "claru_pilot" / "GX010078" / "GX010078_001"
    old_clip.mkdir(parents=True)
    video_bytes = b"exported video bytes"
    imu_bytes = b"timestamp_s,accel_x\n"
    (old_clip / "GX010078_001.mp4").write_bytes(video_bytes)
    (old_clip / "GX010078_001_imu.csv").write_bytes(imu_bytes)
    (old_clip / "GX010078_001.json").write_text(json.dumps({
        "manifest_schema_version": "1.0",
        "project_name": "Claru Pilot",
        "project_slug": "claru_pilot",
        "source_file": source.filename,
        "source_id": "GX010078",
        "clip_id": "GX010078_001",
        "clip_index": 1,
        "task_label": "Sewing pillowcases",
        "imu_files": ["GX010078_001_imu.csv"],
        "qc": {"status": "PASS", "checks": {}, "errors": []},
    }), encoding="utf-8")

    result = migrate_legacy_source(library, source)

    assert result and result["clips"] == 1
    assert not old_cache.exists()
    project = json.loads((source.cache_dir / "project.json").read_text(encoding="utf-8"))
    assert project["device_id"] == "GP-000001"
    assert project["source_id"] == source.source_id
    assert project["clips"][0]["id"] == f"{source.source_id}_001"
    assert project["clips"][0]["task_label"] == "Sewing pillowcases"
    assert (source.cache_dir / "proxy.mp4").read_bytes() == b"proxy bytes"

    new_clip_id = f"{source.source_id}_001"
    new_clip = library.exports_root / "claru_pilot" / source.source_id / new_clip_id
    assert (new_clip / f"{new_clip_id}.mp4").read_bytes() == video_bytes
    assert (new_clip / f"{new_clip_id}_imu.csv").read_bytes() == imu_bytes
    manifest = json.loads((new_clip / f"{new_clip_id}.json").read_text(encoding="utf-8"))
    assert manifest["device_id"] == "GP-000001"
    assert manifest["camera_serial_number"] == "C3531325778769"
    assert manifest["recording_id"] == "DF23F8AB"
    assert manifest["source_id"] == source.source_id
    assert manifest["clip_id"] == new_clip_id
    assert manifest["task_label"] == "Sewing pillowcases"
    assert manifest["imu_files"] == [f"{new_clip_id}_imu.csv"]
