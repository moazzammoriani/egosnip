from __future__ import annotations

import gzip
import json
import math
import statistics
import threading
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from .identity import _find_mp4_boxes
from .media import MediaLibrary, Source


class TelemetryError(RuntimeError):
    pass


@dataclass(frozen=True)
class SensorSample:
    source_timestamp_s: float
    values: tuple[float, float, float]


@dataclass
class SensorData:
    key: str
    name: str | None
    units: str | None
    samples: list[SensorSample]
    estimated_sample_rate_hz: float | None = None


@dataclass
class MasterTelemetry:
    accelerometer: SensorData
    gyroscope: SensorData
    parser: str = "telemetrik"


def _read_uint(handle: BinaryIO, byte_count: int) -> int:
    raw = handle.read(byte_count)
    if len(raw) != byte_count:
        raise TelemetryError("GoPro MP4 ended while reading a telemetry sample table")
    return int.from_bytes(raw, "big")


def _telemetrik_get_samples(handle: BinaryIO, stbl: Any) -> list[Any]:
    """Read GPMF samples from 32- or 64-bit MP4 chunk tables.

    telemetrik 0.1.0 only understands ``stco`` and treats each chunk offset as
    one sample offset. Large GoPro files use ``co64``; honor ``stsc`` as well so
    multiple samples in a chunk resolve to their actual byte positions.
    """
    from telemetrik.parser import Sample

    size_boxes = _find_mp4_boxes(handle, stbl.offset, stbl.size, ["stbl", "stsz"])
    if not size_boxes:
        raise TelemetryError("GPMF track has no sample-size table")
    size_box = size_boxes[0]
    handle.seek(size_box.offset + size_box.header_size + 4)
    fixed_size = _read_uint(handle, 4)
    sample_count = _read_uint(handle, 4)
    if fixed_size:
        sample_sizes = [fixed_size] * sample_count
    else:
        sample_sizes = [_read_uint(handle, 4) for _ in range(sample_count)]

    offset_boxes = _find_mp4_boxes(handle, stbl.offset, stbl.size, ["stbl", "stco"])
    offset_width = 4
    if not offset_boxes:
        offset_boxes = _find_mp4_boxes(handle, stbl.offset, stbl.size, ["stbl", "co64"])
        offset_width = 8
    if not offset_boxes:
        raise TelemetryError("GPMF track has no chunk-offset table")
    offset_box = offset_boxes[0]
    handle.seek(offset_box.offset + offset_box.header_size + 4)
    chunk_count = _read_uint(handle, 4)
    chunk_offsets = [_read_uint(handle, offset_width) for _ in range(chunk_count)]

    stsc_boxes = _find_mp4_boxes(handle, stbl.offset, stbl.size, ["stbl", "stsc"])
    if not stsc_boxes:
        raise TelemetryError("GPMF track has no sample-to-chunk table")
    stsc = stsc_boxes[0]
    handle.seek(stsc.offset + stsc.header_size + 4)
    entry_count = _read_uint(handle, 4)
    chunk_layout = [
        (_read_uint(handle, 4), _read_uint(handle, 4), _read_uint(handle, 4))
        for _ in range(entry_count)
    ]
    if not chunk_layout or chunk_layout[0][0] != 1:
        raise TelemetryError("GPMF sample-to-chunk table is invalid")

    sample_offsets: list[int] = []
    size_index = 0
    layout_index = 0
    for chunk_number, chunk_offset in enumerate(chunk_offsets, start=1):
        while (
            layout_index + 1 < len(chunk_layout)
            and chunk_layout[layout_index + 1][0] <= chunk_number
        ):
            layout_index += 1
        samples_per_chunk = chunk_layout[layout_index][1]
        sample_offset = chunk_offset
        for _ in range(samples_per_chunk):
            if size_index >= len(sample_sizes):
                raise TelemetryError("GPMF chunk table contains too many samples")
            sample_offsets.append(sample_offset)
            sample_offset += sample_sizes[size_index]
            size_index += 1
    if size_index != len(sample_sizes):
        raise TelemetryError("GPMF chunk table does not contain every sample")

    sample_durations: list[int] = []
    stts_boxes = _find_mp4_boxes(handle, stbl.offset, stbl.size, ["stbl", "stts"])
    if stts_boxes:
        stts = stts_boxes[0]
        handle.seek(stts.offset + stts.header_size + 4)
        for _ in range(_read_uint(handle, 4)):
            count = _read_uint(handle, 4)
            delta = _read_uint(handle, 4)
            sample_durations.extend([delta] * count)

    composition_offsets = [0] * len(sample_sizes)
    ctts_boxes = _find_mp4_boxes(handle, stbl.offset, stbl.size, ["stbl", "ctts"])
    if ctts_boxes:
        ctts = ctts_boxes[0]
        handle.seek(ctts.offset + ctts.header_size)
        version = _read_uint(handle, 1)
        handle.read(3)
        composition_index = 0
        for _ in range(_read_uint(handle, 4)):
            count = _read_uint(handle, 4)
            raw_offset = _read_uint(handle, 4)
            if version == 1 and raw_offset >= 2**31:
                raw_offset -= 2**32
            for _ in range(count):
                if composition_index < len(composition_offsets):
                    composition_offsets[composition_index] = raw_offset
                    composition_index += 1

    samples = []
    current_dts = 0
    for index, (sample_offset, sample_size) in enumerate(zip(sample_offsets, sample_sizes)):
        samples.append(
            Sample(
                sample_offset,
                sample_size,
                pts=current_dts + composition_offsets[index],
                dts=current_dts,
            )
        )
        if index < len(sample_durations):
            current_dts += sample_durations[index]
    return samples


def _extract_all_telemetry(path: Path) -> dict[str, Any]:
    """Run telemetrik with large-file-safe MP4 table readers."""
    try:
        from telemetrik import parser as telemetrik_parser
    except ImportError as exc:
        raise TelemetryError("telemetrik is not installed; run `uv sync`") from exc
    telemetrik_parser.get_boxes = _find_mp4_boxes
    telemetrik_parser.get_samples = _telemetrik_get_samples
    return telemetrik_parser.extract_all_telemetry(path, streams=["ACCL", "GYRO"])


def estimated_sample_rate(samples: Iterable[SensorSample]) -> float | None:
    timestamps = [sample.source_timestamp_s for sample in samples]
    if len(timestamps) < 2:
        return None
    deltas = [b - a for a, b in zip(timestamps, timestamps[1:]) if b > a]
    if not deltas:
        return None
    median = statistics.median(deltas)
    return 1.0 / median if median > 0 else None


def slice_sensor(
    sensor: SensorData,
    start_s: float,
    end_s: float,
    retain_one_pre_zero: bool,
) -> SensorData:
    """Slice strict [start, end), with at most the closest preceding sample."""
    timestamps = [sample.source_timestamp_s for sample in sensor.samples]
    first = bisect_left(timestamps, start_s)
    last = bisect_left(timestamps, end_s)
    selected = sensor.samples[first:last]
    if retain_one_pre_zero and first > 0:
        selected = [sensor.samples[first - 1], *selected]
    return SensorData(
        key=sensor.key,
        name=sensor.name,
        units=sensor.units,
        samples=selected,
        estimated_sample_rate_hz=estimated_sample_rate(selected),
    )


def rebase_samples(sensor: SensorData, origin_s: float) -> list[tuple[float, tuple[float, float, float]]]:
    return [
        (sample.source_timestamp_s - origin_s, sample.values)
        for sample in sensor.samples
    ]


class TelemetryCache:
    _locks_guard = threading.Lock()
    _locks: dict[str, threading.Lock] = {}

    def __init__(self, library: MediaLibrary):
        self.library = library

    @classmethod
    def _lock_for(cls, key: str) -> threading.Lock:
        with cls._locks_guard:
            return cls._locks.setdefault(key, threading.Lock())

    def load_or_extract(self, source: Source) -> MasterTelemetry:
        lock = self._lock_for(source.file_id)
        with lock:
            cached = self._load(source)
            if cached is not None:
                return cached
            return self._extract(source)

    def _metadata_path(self, source: Source) -> Path:
        return source.cache_dir / "telemetry_metadata.json"

    def _sensor_path(self, source: Source, key: str) -> Path:
        names = {"ACCL": "accelerometer.json.gz", "GYRO": "gyroscope.json.gz"}
        return source.cache_dir / names[key]

    def _load(self, source: Source) -> MasterTelemetry | None:
        metadata_path = self._metadata_path(source)
        if not metadata_path.exists():
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("fingerprint") != self.library.fingerprint(source):
                return None
            sensors: dict[str, SensorData] = {}
            for key in ("ACCL", "GYRO"):
                sensor_meta = metadata["sensors"][key]
                with gzip.open(self._sensor_path(source, key), "rt", encoding="utf-8") as handle:
                    raw_samples = json.load(handle)
                samples = [
                    SensorSample(float(item[0]), tuple(float(v) for v in item[1]))
                    for item in raw_samples
                ]
                sensors[key] = SensorData(
                    key=key,
                    name=sensor_meta.get("name"),
                    units=sensor_meta.get("units"),
                    samples=samples,
                    estimated_sample_rate_hz=sensor_meta.get("estimated_sample_rate_hz"),
                )
            return MasterTelemetry(sensors["ACCL"], sensors["GYRO"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _extract(self, source: Source) -> MasterTelemetry:
        try:
            raw = _extract_all_telemetry(source.path)
        except TelemetryError:
            raise
        except Exception as exc:
            raise TelemetryError(f"telemetrik could not parse {source.filename}: {exc}") from exc

        sensors: dict[str, SensorData] = {}
        for key in ("ACCL", "GYRO"):
            stream = raw.get(key)
            if stream is None:
                raise TelemetryError(f"{key} telemetry is unavailable in {source.filename}")
            pts_data = getattr(stream, "pts_data", None)
            if not pts_data:
                raise TelemetryError(f"{key} has no presentation-timeline timestamps")
            samples: list[SensorSample] = []
            for timestamp, values in pts_data:
                if len(values) != 3:
                    raise TelemetryError(f"{key} returned a non-3-axis sample")
                sample = SensorSample(float(timestamp), tuple(float(v) for v in values))
                if not math.isfinite(sample.source_timestamp_s) or not all(
                    math.isfinite(value) for value in sample.values
                ):
                    raise TelemetryError(f"{key} returned a non-finite sample")
                samples.append(sample)
            samples.sort(key=lambda item: item.source_timestamp_s)
            sensor = SensorData(
                key=key,
                name=getattr(stream, "name", None),
                units=getattr(stream, "units", None),
                samples=samples,
            )
            sensor.estimated_sample_rate_hz = estimated_sample_rate(samples)
            sensors[key] = sensor

        source.cache_dir.mkdir(parents=True, exist_ok=True)
        for key, sensor in sensors.items():
            temp = self._sensor_path(source, key).with_suffix(".tmp")
            with gzip.open(temp, "wt", encoding="utf-8", compresslevel=6) as handle:
                json.dump(
                    [[sample.source_timestamp_s, sample.values] for sample in sensor.samples],
                    handle,
                    separators=(",", ":"),
                )
            temp.replace(self._sensor_path(source, key))

        metadata = {
            "fingerprint": self.library.fingerprint(source),
            "parser": "telemetrik",
            "timestamp_clock": "pts_data_seconds",
            "axis_order": "preserved_as_returned_by_parser; physical orientation not independently validated",
            "sensors": {
                key: {
                    "name": sensor.name,
                    "units": sensor.units,
                    "sample_count": len(sensor.samples),
                    "estimated_sample_rate_hz": sensor.estimated_sample_rate_hz,
                }
                for key, sensor in sensors.items()
            },
        }
        temp_metadata = self._metadata_path(source).with_suffix(".tmp")
        temp_metadata.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        temp_metadata.replace(self._metadata_path(source))
        return MasterTelemetry(sensors["ACCL"], sensors["GYRO"])
