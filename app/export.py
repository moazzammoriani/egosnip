from __future__ import annotations

import csv
import json
import math
import os
from pathlib import Path
from typing import Any

from .media import MediaError, MediaLibrary, Source, detect_streams, ffprobe_json, probe_media, run_command
from .models import Clip, ProjectState
from .projects import ExportProjectCatalog, project_slug
from .telemetry import (
    MasterTelemetry,
    SensorData,
    TelemetryCache,
    TelemetryError,
    rebase_samples,
    slice_sensor,
)


TIMESTAMP_ALIGNMENT_TOLERANCE_S = 0.0001


def clip_basename(source: Source, clip: Clip) -> str:
    return f"{source.source_id}_{clip.clip_index:03d}"


def find_first_source_video_frame_at_or_after(source_path: Path, requested_start_s: float) -> float:
    """Read decoded frame timestamps near the cut and return the first frame at/after S."""
    for radius in (1.0, 3.0, 8.0):
        interval_start = max(0.0, requested_start_s - radius)
        interval_duration = requested_start_s - interval_start + radius
        probe = ffprobe_json(
            source_path,
            [
                "-select_streams", "v:0",
                "-read_intervals", f"{interval_start:.9f}%+{interval_duration:.9f}",
                "-show_frames",
                "-show_entries", "frame=best_effort_timestamp_time,pts_time,media_type",
            ],
        )
        timestamps: list[float] = []
        for frame in probe.get("frames", []):
            if frame.get("media_type") not in (None, "video"):
                continue
            raw = frame.get("best_effort_timestamp_time") or frame.get("pts_time")
            if raw is None:
                continue
            timestamp = float(raw)
            if timestamp + 1e-9 >= requested_start_s:
                timestamps.append(timestamp)
        if timestamps:
            return min(timestamps)
    raise MediaError(f"Could not find a displayed video frame at or after {requested_start_s:.6f}s")


def output_video_duration(path: Path) -> float:
    probe = ffprobe_json(path, ["-select_streams", "v:0", "-show_streams"])
    streams = probe.get("streams", [])
    if not streams:
        raise MediaError("Exported file has no video stream")
    stream = streams[0]
    if stream.get("duration") is not None:
        return float(stream["duration"])
    if stream.get("duration_ts") is not None and stream.get("time_base"):
        numerator, denominator = stream["time_base"].split("/", 1)
        return float(stream["duration_ts"]) * float(numerator) / float(denominator)
    raise MediaError("FFprobe did not report an output video stream duration")


def timestamps_align(accel: SensorData, gyro: SensorData) -> tuple[bool, float | None]:
    if len(accel.samples) != len(gyro.samples):
        return False, None
    if not accel.samples:
        return False, None
    deltas = [
        abs(a.source_timestamp_s - g.source_timestamp_s)
        for a, g in zip(accel.samples, gyro.samples)
    ]
    maximum = max(deltas, default=0.0)
    return maximum <= TIMESTAMP_ALIGNMENT_TOLERANCE_S, maximum


def _write_combined_csv(path: Path, accel: SensorData, gyro: SensorData, origin_s: float) -> None:
    accel_rows = rebase_samples(accel, origin_s)
    gyro_rows = rebase_samples(gyro, origin_s)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp_s", "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z"])
        for (timestamp, accel_values), (_, gyro_values) in zip(accel_rows, gyro_rows):
            writer.writerow([f"{timestamp:.9f}", *accel_values, *gyro_values])


def _write_sensor_csv(path: Path, sensor: SensorData, origin_s: float, prefix: str) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp_s", f"{prefix}_x", f"{prefix}_y", f"{prefix}_z"])
        for timestamp, values in rebase_samples(sensor, origin_s):
            writer.writerow([f"{timestamp:.9f}", *values])


def _is_monotonic(sensor: SensorData) -> bool:
    return all(
        later.source_timestamp_s > earlier.source_timestamp_s
        for earlier, later in zip(sensor.samples, sensor.samples[1:])
    )


def build_manifest(
    *,
    source: Source,
    project: ProjectState,
    clip: Clip,
    source_metadata: dict[str, Any],
    output_metadata: dict[str, Any],
    actual_start_s: float,
    video_duration_s: float,
    retain_pre_zero: bool,
    accel: SensorData | None,
    gyro: SensorData | None,
    imu_files: list[str],
    imu_format: str | None,
    max_alignment_delta_s: float | None,
    qc_checks: dict[str, dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    sensors = [sensor for sensor in (accel, gyro) if sensor is not None]
    all_samples = [sample for sensor in sensors for sample in sensor.samples]
    first_source = min((s.source_timestamp_s for s in all_samples), default=None)
    relative = [s.source_timestamp_s - actual_start_s for s in all_samples]
    return {
        "manifest_schema_version": "1.0",
        "project_name": project.project_name,
        "project_slug": project_slug(project.project_name),
        "device_id": source.device_id,
        "camera_serial_number": source.camera_serial_number,
        "camera_model": source.camera_model,
        "camera_firmware": source.camera_firmware,
        "source_file": source.filename,
        "source_file_stem": source.source_file_stem,
        "recording_id": source.recording_id,
        "source_id": source.source_id,
        "clip_id": clip.id,
        "clip_index": clip.clip_index,
        "task_label": clip.task_label,
        "requested_start_s": clip.requested_start_s,
        "requested_end_s": clip.requested_end_s,
        "actual_video_start_source_s": actual_start_s,
        "video_duration_s": video_duration_s,
        "actual_video_end_source_s": actual_start_s + video_duration_s,
        "retain_one_pre_zero_imu_sample": retain_pre_zero,
        "imu_first_source_timestamp_s": first_source,
        "imu_first_clip_timestamp_s": min(relative, default=None),
        "imu_last_clip_timestamp_s": max(relative, default=None),
        "accel_sample_count": len(accel.samples) if accel else 0,
        "gyro_sample_count": len(gyro.samples) if gyro else 0,
        "accel_estimated_sample_rate_hz": accel.estimated_sample_rate_hz if accel else None,
        "gyro_estimated_sample_rate_hz": gyro.estimated_sample_rate_hz if gyro else None,
        "source_video_codec": source_metadata.get("video_codec"),
        "output_video_codec": output_metadata.get("video_codec"),
        "source_audio_codec": source_metadata.get("audio_codec"),
        "output_audio_codec": output_metadata.get("audio_codec"),
        "source_resolution": [source_metadata.get("width"), source_metadata.get("height")],
        "output_resolution": [output_metadata.get("width"), output_metadata.get("height")],
        "gpmd_preserved_in_output_mp4": bool(output_metadata.get("gpmd_present")),
        "telemetry_source": "original_source_mp4",
        "telemetry_parser": "telemetrik",
        "telemetry_timestamp_source": "pts_data",
        "imu_files": imu_files,
        "imu_format": imu_format,
        "combined_csv_timestamp_source": "accelerometer" if imu_format == "combined" else None,
        "max_accel_gyro_timestamp_delta_s": max_alignment_delta_s,
        "sensor_metadata": {
            "accelerometer": {
                "sensor_name": accel.name if accel else None,
                "units": accel.units if accel else None,
                "axis_order": "parser order; physical orientation not independently validated",
            },
            "gyroscope": {
                "sensor_name": gyro.name if gyro else None,
                "units": gyro.units if gyro else None,
                "axis_order": "parser order; physical orientation not independently validated",
            },
        },
        "ffmpeg_video_mode": "stream_copy",
        "qc": {
            "status": "PASS" if all(check["pass"] for check in qc_checks.values()) and not errors else "FAIL",
            "checks": qc_checks,
            "errors": errors,
        },
    }


class ClipExporter:
    def __init__(self, library: MediaLibrary, telemetry_cache: TelemetryCache):
        self.library = library
        self.telemetry_cache = telemetry_cache
        self.project_catalog = ExportProjectCatalog(library.exports_root)

    def _output_dir(self, source: Source, project: ProjectState, clip: Clip) -> tuple[Path, str]:
        base = clip_basename(source, clip)
        exports_root = self.library.exports_root.resolve()
        namespace = self.project_catalog.ensure(project.project_name)
        output_dir = (exports_root / namespace["project_slug"] / source.source_id / base).resolve()
        try:
            output_dir.relative_to(exports_root)
        except ValueError as exc:
            raise ValueError("Export path escapes the configured exports directory") from exc
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir, base

    def _reported_output_dir(self, output_dir: Path) -> str:
        try:
            return os.fspath(output_dir.relative_to(self.library.media_dir))
        except ValueError:
            return os.fspath(output_dir)

    def _ffmpeg_export(
        self,
        source: Source,
        clip: Clip,
        metadata: dict[str, Any],
        output_path: Path,
    ) -> list[str]:
        duration = clip.requested_end_s - clip.requested_start_s
        args = [
            "ffmpeg", "-y", "-ss", f"{clip.requested_start_s:.9f}",
            "-i", os.fspath(source.path),
            "-map", f"0:{metadata['video_stream_index']}",
        ]
        if metadata.get("audio_stream_index") is not None:
            args.extend(["-map", f"0:{metadata['audio_stream_index']}"])
        if metadata.get("gpmd_stream_index") is not None:
            args.extend(["-map", f"0:{metadata['gpmd_stream_index']}"])
        args.extend(["-c", "copy", "-copy_unknown"])
        if metadata.get("gpmd_stream_index") is not None:
            args.extend(["-tag:d:0", "gpmd"])
        args.extend(["-t", f"{duration:.9f}", os.fspath(output_path)])
        run_command(args)
        return args

    def export_clip(
        self,
        source: Source,
        project: ProjectState,
        clip: Clip,
        master: MasterTelemetry | None = None,
        telemetry_error: str | None = None,
    ) -> dict[str, Any]:
        source_metadata = self.library.metadata(source)
        if not (0 <= clip.requested_start_s < clip.requested_end_s <= source_metadata["duration"] + 1e-6):
            raise ValueError("Clip interval is outside the source duration")
        output_dir, base = self._output_dir(source, project, clip)
        video_path = output_dir / f"{base}.mp4"
        actual_start = find_first_source_video_frame_at_or_after(source.path, clip.requested_start_s)
        ffmpeg_args = self._ffmpeg_export(source, clip, source_metadata, video_path)
        video_duration = output_video_duration(video_path)
        actual_end = actual_start + video_duration
        output_metadata = probe_media(video_path)

        errors: list[str] = []
        accel: SensorData | None = None
        gyro: SensorData | None = None
        imu_files: list[str] = []
        imu_format: str | None = None
        max_delta: float | None = None
        if telemetry_error:
            errors.append(telemetry_error)
        elif master is None:
            errors.append("Master telemetry was not provided")
        else:
            retain = project.imu_settings.retain_one_pre_zero_sample
            accel = slice_sensor(master.accelerometer, actual_start, actual_end, retain)
            gyro = slice_sensor(master.gyroscope, actual_start, actual_end, retain)
            aligned, max_delta = timestamps_align(accel, gyro)
            if aligned:
                imu_path = output_dir / f"{base}_imu.csv"
                _write_combined_csv(imu_path, accel, gyro, actual_start)
                imu_files = [imu_path.name]
                imu_format = "combined"
            else:
                accel_path = output_dir / f"{base}_accelerometer.csv"
                gyro_path = output_dir / f"{base}_gyroscope.csv"
                _write_sensor_csv(accel_path, accel, actual_start, "accel")
                _write_sensor_csv(gyro_path, gyro, actual_start, "gyro")
                imu_files = [accel_path.name, gyro_path.name]
                imu_format = "separate_unaligned_timestamps"

        retain = project.imu_settings.retain_one_pre_zero_sample
        expected_duration = clip.requested_end_s - clip.requested_start_s
        qc: dict[str, dict[str, Any]] = {
            "output_exists": {"pass": video_path.is_file() and video_path.stat().st_size > 0},
            "video_stream_exists": {"pass": output_metadata.get("video_codec") is not None},
            "video_codec_unchanged": {"pass": output_metadata.get("video_codec") == source_metadata.get("video_codec")},
            "resolution_unchanged": {"pass": (
                output_metadata.get("width"), output_metadata.get("height")
            ) == (source_metadata.get("width"), source_metadata.get("height"))},
            "audio_preserved_if_present": {"pass": (
                source_metadata.get("audio_codec") is None
                or output_metadata.get("audio_codec") == source_metadata.get("audio_codec")
            )},
            "gpmd_preserved_if_present": {"pass": (
                not source_metadata.get("gpmd_present") or output_metadata.get("gpmd_present")
            )},
            "video_duration_plausible": {
                "pass": video_duration > 0 and abs(video_duration - expected_duration) <= max(1.0, expected_duration * 0.02),
                "value_s": video_duration,
                "expected_s": expected_duration,
            },
            "stream_copy_requested": {"pass": "copy" in ffmpeg_args},
            "accelerometer_samples_present": {"pass": bool(accel and accel.samples)},
            "gyroscope_samples_present": {"pass": bool(gyro and gyro.samples)},
            "accelerometer_timestamps_monotonic": {"pass": bool(accel and _is_monotonic(accel))},
            "gyroscope_timestamps_monotonic": {"pass": bool(gyro and _is_monotonic(gyro))},
        }
        for key, sensor in (("accelerometer", accel), ("gyroscope", gyro)):
            relative = rebase_samples(sensor, actual_start) if sensor else []
            negative = [timestamp for timestamp, _ in relative if timestamp < 0]
            nonnegative = [timestamp for timestamp, _ in relative if timestamp >= 0]
            bounds_ok = all(timestamp < video_duration + 1e-8 for timestamp in nonnegative)
            if retain:
                prezero_ok = len(negative) <= 1
            else:
                prezero_ok = not negative
            qc[f"{key}_prezero_policy"] = {"pass": prezero_ok, "negative_sample_count": len(negative)}
            qc[f"{key}_within_video_interval"] = {"pass": bounds_ok}
            original_sensor = None
            if master is not None:
                original_sensor = master.accelerometer if key == "accelerometer" else master.gyroscope
            preceding = [
                sample.source_timestamp_s
                for sample in (original_sensor.samples if original_sensor else [])
                if sample.source_timestamp_s < actual_start
            ]
            expected_prezero = max(preceding, default=None)
            actual_prezero = (
                actual_start + negative[0]
                if len(negative) == 1
                else None
            )
            closest_ok = (
                (not retain and actual_prezero is None)
                or (
                    retain
                    and (
                        (expected_prezero is None and actual_prezero is None)
                        or (
                            expected_prezero is not None
                            and actual_prezero is not None
                            and math.isclose(actual_prezero, expected_prezero, abs_tol=1e-9)
                        )
                    )
                )
            )
            qc[f"{key}_closest_prezero_sample"] = {
                "pass": closest_ok,
                "expected_source_timestamp_s": expected_prezero if retain else None,
                "actual_source_timestamp_s": actual_prezero,
            }

        manifest = build_manifest(
            source=source,
            project=project,
            clip=clip,
            source_metadata=source_metadata,
            output_metadata=output_metadata,
            actual_start_s=actual_start,
            video_duration_s=video_duration,
            retain_pre_zero=retain,
            accel=accel,
            gyro=gyro,
            imu_files=imu_files,
            imu_format=imu_format,
            max_alignment_delta_s=max_delta,
            qc_checks=qc,
            errors=errors,
        )
        manifest_path = output_dir / f"{base}.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
        failed_checks = [name for name, check in qc.items() if not check["pass"]]
        if errors:
            message = "; ".join(errors)
        elif failed_checks:
            message = f"QC failed: {', '.join(failed_checks)}"
        else:
            message = "QC passed"
        return {
            "clip_id": clip.id,
            "clip_index": clip.clip_index,
            "status": manifest["qc"]["status"],
            "message": message,
            "output_dir": self._reported_output_dir(output_dir),
            "manifest": manifest,
        }

    def write_failure_manifest(
        self, source: Source, project: ProjectState, clip: Clip, message: str
    ) -> dict[str, Any]:
        output_dir, base = self._output_dir(source, project, clip)
        manifest = {
            "manifest_schema_version": "1.0",
            "project_name": project.project_name,
            "project_slug": project_slug(project.project_name),
            "device_id": source.device_id,
            "camera_serial_number": source.camera_serial_number,
            "camera_model": source.camera_model,
            "camera_firmware": source.camera_firmware,
            "source_file": source.filename,
            "source_file_stem": source.source_file_stem,
            "recording_id": source.recording_id,
            "source_id": source.source_id,
            "clip_id": clip.id,
            "clip_index": clip.clip_index,
            "task_label": clip.task_label,
            "requested_start_s": clip.requested_start_s,
            "requested_end_s": clip.requested_end_s,
            "telemetry_source": "original_source_mp4",
            "telemetry_parser": "telemetrik",
            "qc": {"status": "FAIL", "checks": {}, "errors": [message]},
        }
        (output_dir / f"{base}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return {
            "clip_id": clip.id,
            "clip_index": clip.clip_index,
            "status": "FAIL",
            "message": message,
            "output_dir": self._reported_output_dir(output_dir),
            "manifest": manifest,
        }
