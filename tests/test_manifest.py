from pathlib import Path

import pytest

from app.export import build_manifest
from app.media import Source
from app.models import Clip
from app.telemetry import SensorData, SensorSample


def test_manifest_contains_identity_timing_label_and_counts(tmp_path: Path) -> None:
    source = Source("a" * 20, "GX010005.MP4", "GX010005", tmp_path / "source.mp4", tmp_path / "cache")
    clip = Clip(
        id="GX010005_002", clip_index=2, task_label="Cutting fabric",
        requested_start_s=120.0, requested_end_s=170.0,
    )
    accel = SensorData("ACCL", "Accelerometer", "m/s²", [SensorSample(120.0031, (1, 2, 3))], 196.9)
    gyro = SensorData("GYRO", "Gyroscope", "rad/s", [SensorSample(120.0031, (4, 5, 6))], 196.9)
    manifest = build_manifest(
        source=source,
        clip=clip,
        source_metadata={"video_codec": "hevc", "audio_codec": "aac", "width": 3840, "height": 2160},
        output_metadata={"video_codec": "hevc", "audio_codec": "aac", "width": 3840, "height": 2160, "gpmd_present": True},
        actual_start_s=120.003217,
        video_duration_s=49.99,
        retain_pre_zero=True,
        accel=accel,
        gyro=gyro,
        imu_files=["GX010005_002_cutting_fabric_imu.csv"],
        imu_format="combined",
        max_alignment_delta_s=0.0,
        qc_checks={"all": {"pass": True}},
        errors=[],
    )
    assert manifest["source_file"] == "GX010005.MP4"
    assert manifest["clip_id"] == "GX010005_002"
    assert manifest["task_label"] == "Cutting fabric"
    assert manifest["actual_video_end_source_s"] == pytest.approx(169.993217)
    assert manifest["imu_first_clip_timestamp_s"] < 0
    assert manifest["accel_sample_count"] == manifest["gyro_sample_count"] == 1
    assert manifest["qc"]["status"] == "PASS"
