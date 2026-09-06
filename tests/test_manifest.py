from pathlib import Path

import pytest

from app.export import build_manifest
from app.media import Source
from app.models import Clip, ProjectState
from app.telemetry import SensorData, SensorSample


def test_manifest_contains_identity_timing_label_and_counts(tmp_path: Path) -> None:
    source_id = "GP-000001_GX010005_A72F91C3"
    source = Source(
        file_id="a" * 20,
        filename="GX010005.MP4",
        source_id=source_id,
        source_file_stem="GX010005",
        recording_id="A72F91C3",
        recording_fingerprint="a" * 64,
        device_id="GP-000001",
        camera_serial_number="C3531325778769",
        camera_model="HERO13 Black",
        camera_firmware="H24.01.02.10.00",
        path=tmp_path / "source.mp4",
        cache_dir=tmp_path / "cache",
    )
    clip = Clip(
        id=f"{source_id}_002", clip_index=2, task_label="Cutting fabric",
        requested_start_s=120.0, requested_end_s=170.0,
    )
    accel = SensorData("ACCL", "Accelerometer", "m/s²", [SensorSample(120.0031, (1, 2, 3))], 196.9)
    gyro = SensorData("GYRO", "Gyroscope", "rad/s", [SensorSample(120.0031, (4, 5, 6))], 196.9)
    project = ProjectState(
        project_name="Claru Textile Pilot",
        source_file=source.filename,
        source_id=source.source_id,
        device_id=source.device_id,
        camera_serial_number=source.camera_serial_number,
        recording_id=source.recording_id,
        clips=[clip],
        next_clip_index=3,
    )
    manifest = build_manifest(
        source=source,
        project=project,
        clip=clip,
        source_metadata={"video_codec": "hevc", "audio_codec": "aac", "width": 3840, "height": 2160},
        output_metadata={"video_codec": "hevc", "audio_codec": "aac", "width": 3840, "height": 2160, "gpmd_present": True},
        actual_start_s=120.003217,
        video_duration_s=49.99,
        retain_pre_zero=True,
        accel=accel,
        gyro=gyro,
        imu_files=[f"{source_id}_002_imu.csv"],
        imu_format="combined",
        max_alignment_delta_s=0.0,
        qc_checks={"all": {"pass": True}},
        errors=[],
    )
    assert manifest["source_file"] == "GX010005.MP4"
    assert manifest["manifest_schema_version"] == "1.0"
    assert manifest["project_name"] == "Claru Textile Pilot"
    assert manifest["project_slug"] == "claru_textile_pilot"
    assert manifest["device_id"] == "GP-000001"
    assert manifest["camera_serial_number"] == "C3531325778769"
    assert manifest["recording_id"] == "A72F91C3"
    assert manifest["source_id"] == source_id
    assert manifest["clip_id"] == f"{source_id}_002"
    assert manifest["task_label"] == "Cutting fabric"
    assert manifest["actual_video_end_source_s"] == pytest.approx(169.993217)
    assert manifest["imu_first_clip_timestamp_s"] < 0
    assert manifest["accel_sample_count"] == manifest["gyro_sample_count"] == 1
    assert manifest["qc"]["status"] == "PASS"
