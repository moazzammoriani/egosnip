import json
import subprocess
from pathlib import Path

from app.export import ClipExporter, clip_basename
from app.media import MediaLibrary, Source
from app.models import Clip, ProjectState
from app.telemetry import TelemetryCache


def test_stream_copy_export_and_failure_manifest_without_gpmd(tmp_path: Path) -> None:
    source_path = tmp_path / "SYNTH001.MP4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
            "-t", "3", "-c:v", "libx264", "-g", "30", "-c:a", "aac",
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    library = MediaLibrary(tmp_path)
    source_id = "GP-000001_SYNTH001_A72F91C3"
    source = Source(
        file_id="a" * 20,
        filename="SYNTH001.MP4",
        source_id=source_id,
        source_file_stem="SYNTH001",
        recording_id="A72F91C3",
        recording_fingerprint="a" * 64,
        device_id="GP-000001",
        camera_serial_number="C0000000000001",
        camera_model="Synthetic GoPro",
        camera_firmware="TEST",
        path=source_path,
        cache_dir=tmp_path / ".snipper_cache" / source_id,
    )
    clip = Clip(
        id=f"{source_id}_001", clip_index=1, task_label="Synthetic task",
        requested_start_s=0.5, requested_end_s=2.0,
    )
    project = ProjectState(
        project_name="Synthetic Project", source_file=source.filename, source_id=source.source_id,
        device_id=source.device_id, camera_serial_number=source.camera_serial_number,
        recording_id=source.recording_id,
        clips=[clip], next_clip_index=2,
    )
    exporter = ClipExporter(library, TelemetryCache(library))
    result = exporter.export_clip(
        source, project, clip,
        telemetry_error="Source has no gpmd stream; external ACCL/GYRO telemetry is unavailable",
    )

    base = clip_basename(source, clip)
    output_dir = tmp_path / result["output_dir"]
    assert output_dir == tmp_path / "exports" / "synthetic_project" / source_id / f"{source_id}_001"
    assert (output_dir / f"{base}.mp4").is_file()
    manifest = json.loads((output_dir / f"{base}.json").read_text(encoding="utf-8"))
    assert manifest["source_file"] == "SYNTH001.MP4"
    assert manifest["project_name"] == "Synthetic Project"
    assert manifest["project_slug"] == "synthetic_project"
    assert manifest["task_label"] == "Synthetic task"
    assert manifest["output_video_codec"] == manifest["source_video_codec"] == "h264"
    assert manifest["output_audio_codec"] == manifest["source_audio_codec"] == "aac"
    assert manifest["ffmpeg_video_mode"] == "stream_copy"
    assert manifest["qc"]["status"] == result["status"] == "FAIL"
    assert "no gpmd" in result["message"]
