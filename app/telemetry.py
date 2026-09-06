from __future__ import annotations

import gzip
import json
import math
import statistics
import threading
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

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
            from telemetrik import extract_all_telemetry
        except ImportError as exc:
            raise TelemetryError("telemetrik is not installed; run `uv sync`") from exc
        try:
            raw = extract_all_telemetry(source.path, streams=["ACCL", "GYRO"])
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
