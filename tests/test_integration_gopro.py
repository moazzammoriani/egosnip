import os
from pathlib import Path

import pytest

from app.export import find_first_source_video_frame_at_or_after
from app.export import ClipExporter
from app.media import MediaLibrary
from app.models import Clip, IMUSettings, ProjectState
from app.telemetry import TelemetryCache, rebase_samples, slice_sensor


MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", ".")).resolve()
FIXTURE = MEDIA_DIR / "GX010005.MP4"


@pytest.mark.integration
@pytest.mark.skipif(not FIXTURE.is_file(), reason="GX010005.MP4 is not present in MEDIA_DIR")
def test_known_hero13_t0_and_prezero_sample() -> None:
    t0 = find_first_source_video_frame_at_or_after(FIXTURE, 120.0)
    assert t0 == pytest.approx(120.003217, abs=0.02)

    library = MediaLibrary(MEDIA_DIR)
    source = next(item for item in library.sources() if item.filename == FIXTURE.name)
    master = TelemetryCache(library).load_or_extract(source)
    accel = slice_sensor(master.accelerometer, t0, t0 + 1.0, True)
    first_relative = rebase_samples(accel, t0)[0][0]
    assert first_relative < 0
    assert first_relative == pytest.approx(-0.00008502, abs=0.002)


@pytest.mark.integration
@pytest.mark.skipif(not FIXTURE.is_file(), reason="GX010005.MP4 is not present in MEDIA_DIR")
def test_real_hero13_stream_copy_imu_and_qc(tmp_path: Path) -> None:
    library = MediaLibrary(MEDIA_DIR)
    library.exports_root = tmp_path / "exports"
    source = next(item for item in library.sources() if item.filename == FIXTURE.name)
    clip = Clip(
        id="GX010005_900",
        clip_index=900,
        task_label="Integration validation",
        requested_start_s=120.0,
        requested_end_s=122.0,
    )
    project = ProjectState(
        source_file=source.filename,
        source_id=source.source_id,
        imu_settings=IMUSettings(retain_one_pre_zero_sample=True),
        clips=[clip],
        next_clip_index=901,
    )
    cache = TelemetryCache(library)
    result = ClipExporter(library, cache).export_clip(
        source,
        project,
        clip,
        master=cache.load_or_extract(source),
    )
    manifest = result["manifest"]

    assert result["status"] == "PASS", result["message"]
    assert manifest["actual_video_start_source_s"] == pytest.approx(120.003217, abs=0.02)
    assert manifest["gpmd_preserved_in_output_mp4"] is True
    assert manifest["source_video_codec"] == manifest["output_video_codec"] == "hevc"
    assert manifest["source_audio_codec"] == manifest["output_audio_codec"] == "aac"
    assert manifest["accel_sample_count"] > 0
    assert manifest["gyro_sample_count"] > 0
    assert manifest["imu_first_clip_timestamp_s"] < 0
    assert manifest["qc"]["status"] == "PASS"
