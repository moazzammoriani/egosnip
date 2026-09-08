from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class IdentityError(RuntimeError):
    pass


class DeviceRegistryError(IdentityError):
    pass


def normalize_camera_serial(serial: str) -> str:
    normalized = serial.strip().upper()
    if not normalized or len(normalized) > 128 or any(ord(char) < 32 for char in normalized):
        raise IdentityError("GoPro camera serial is empty or invalid")
    return normalized


def source_stem_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_-")
    return safe or "source"


def format_device_id(number: int) -> str:
    if number < 1:
        raise ValueError("Device number must be positive")
    return f"GP-{number:06d}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class DeviceRegistry:
    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, path: Path, now: Callable[[], str] = _utc_now):
        self.path = path.resolve()
        self.now = now

    @classmethod
    def _lock_for(cls, path: Path) -> threading.RLock:
        key = os.fspath(path)
        with cls._locks_guard:
            return cls._locks.setdefault(key, threading.RLock())

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"schema_version": 1, "next_device_number": 1, "devices": {}}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeviceRegistryError(f"Device registry is unreadable: {self.path}") from exc
        if (
            payload.get("schema_version") != 1
            or not isinstance(payload.get("next_device_number"), int)
            or payload["next_device_number"] < 1
            or not isinstance(payload.get("devices"), dict)
        ):
            raise DeviceRegistryError("Device registry has an invalid schema")
        seen_ids: set[str] = set()
        maximum_number = 0
        for serial, entry in payload["devices"].items():
            try:
                normalized_serial = normalize_camera_serial(serial) if isinstance(serial, str) else None
            except IdentityError as exc:
                raise DeviceRegistryError(
                    "Device registry contains a non-normalized serial"
                ) from exc
            if normalized_serial != serial:
                raise DeviceRegistryError("Device registry contains a non-normalized serial")
            if not isinstance(entry, dict):
                raise DeviceRegistryError("Device registry contains an invalid device entry")
            device_id = entry.get("device_id")
            match = re.fullmatch(r"GP-(\d{6,})", device_id or "")
            if not match or int(match.group(1)) < 1 or device_id in seen_ids:
                raise DeviceRegistryError("Device registry violates one-to-one device identity")
            seen_ids.add(device_id)
            maximum_number = max(maximum_number, int(match.group(1)))
        if payload["next_device_number"] <= maximum_number:
            raise DeviceRegistryError("Device registry would reuse an allocated device ID")
        return payload

    @contextmanager
    def _process_lock(self):
        """Serialize registry updates between local EgoSnip processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        with lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                if lock_path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temp.replace(self.path)
        finally:
            if temp.exists():
                temp.unlink()

    def register(
        self,
        serial: str,
        *,
        camera_model: str | None = None,
        camera_firmware: str | None = None,
    ) -> str:
        normalized = normalize_camera_serial(serial)
        with self._lock_for(self.path):
            with self._process_lock():
                payload = self._load()
                now = self.now()
                entry = payload["devices"].get(normalized)
                if entry is None:
                    number = payload["next_device_number"]
                    device_id = format_device_id(number)
                    entry = {
                        "device_id": device_id,
                        "camera_model": camera_model,
                        "camera_firmware": camera_firmware,
                        "first_seen": now,
                        "last_seen": now,
                    }
                    payload["devices"][normalized] = entry
                    payload["next_device_number"] = number + 1
                else:
                    device_id = entry["device_id"]
                    entry["last_seen"] = now
                    if camera_model:
                        entry["camera_model"] = camera_model
                    if camera_firmware:
                        entry["camera_firmware"] = camera_firmware
                self._write(payload)
                return device_id


@dataclass(frozen=True)
class GoProHeaderMetadata:
    camera_serial_number: str
    camera_model: str | None
    camera_firmware: str | None
    media_id: str | None
    camera_creation_timestamp: int | None


@dataclass(frozen=True)
class MP4Box:
    key: str
    offset: int
    size: int
    header_size: int


def _find_mp4_boxes(
    handle: Any,
    offset: int,
    size: int,
    box_path: list[str],
) -> list[MP4Box]:
    """Walk bounded MP4 atoms, including 64-bit and end-of-file sizes."""
    boxes: list[MP4Box] = []
    end = offset + size
    while offset < end:
        remaining = end - offset
        if remaining < 8:
            raise IdentityError("GoPro MP4 contains a truncated box header")
        handle.seek(offset)
        header = handle.read(8)
        if len(header) != 8:
            raise IdentityError("GoPro MP4 ended while reading a box header")
        size_32 = int.from_bytes(header[:4], "big")
        try:
            key = header[4:8].decode("ascii")
        except UnicodeDecodeError as exc:
            raise IdentityError("GoPro MP4 contains an invalid box type") from exc
        header_size = 8
        if size_32 == 1:
            extended = handle.read(8)
            if len(extended) != 8:
                raise IdentityError("GoPro MP4 contains a truncated extended-size box")
            box_size = int.from_bytes(extended, "big")
            header_size = 16
        elif size_32 == 0:
            box_size = remaining
        else:
            box_size = size_32
        if box_size < header_size or box_size > remaining:
            raise IdentityError(f"GoPro MP4 contains an invalid {key!r} box size")
        box = MP4Box(key, offset, box_size, header_size)
        if key == box_path[0]:
            if len(box_path) == 1:
                boxes.append(box)
            else:
                boxes.extend(
                    _find_mp4_boxes(
                        handle,
                        offset + header_size,
                        box_size - header_size,
                        box_path[1:],
                    )
                )
        offset += box_size
    return boxes


def _gpmf_payload_bytes(handle: Any, box: Any) -> bytes:
    handle.seek(box.offset + 8)
    return handle.read(box.struct_size * box.repeat)


def _gpmf_ascii(handle: Any, box: Any) -> str:
    return _gpmf_payload_bytes(handle, box).decode("ascii").strip("\x00 \t\r\n")


def extract_gopro_header_metadata(
    path: Path, source_filename: str | None = None
) -> GoProHeaderMetadata:
    """Read the GoPro global-settings GPMF header from the original MP4."""
    try:
        from telemetrik.parser import get_gpmf_boxes
    except ImportError as exc:
        raise IdentityError("telemetrik is required to read GoPro identity metadata") from exc

    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            gpmf_atoms = _find_mp4_boxes(handle, 0, size, ["moov", "udta", "GPMF"])
            for atom in gpmf_atoms:
                devices = get_gpmf_boxes(
                    handle,
                    atom.offset + atom.header_size,
                    atom.size - atom.header_size,
                    ["DEVC"],
                )
                for device in devices:
                    names = get_gpmf_boxes(handle, device.offset, device.size, ["DEVC", "DVNM"])
                    if not names or _gpmf_ascii(handle, names[0]).lower() != "global settings":
                        continue
                    serial_boxes = get_gpmf_boxes(
                        handle, device.offset, device.size, ["DEVC", "CASN"]
                    )
                    if not serial_boxes:
                        continue
                    serial = normalize_camera_serial(_gpmf_ascii(handle, serial_boxes[0]))

                    def ascii_value(key: str) -> str | None:
                        boxes = get_gpmf_boxes(
                            handle, device.offset, device.size, ["DEVC", key]
                        )
                        return _gpmf_ascii(handle, boxes[0]) if boxes else None

                    media_boxes = get_gpmf_boxes(
                        handle, device.offset, device.size, ["DEVC", "MUID"]
                    )
                    media_id = _gpmf_payload_bytes(handle, media_boxes[0]).hex().upper() if media_boxes else None
                    creation_boxes = get_gpmf_boxes(
                        handle, device.offset, device.size, ["DEVC", "CDAT"]
                    )
                    creation = (
                        int.from_bytes(_gpmf_payload_bytes(handle, creation_boxes[0]), "big")
                        if creation_boxes
                        else None
                    )
                    return GoProHeaderMetadata(
                        camera_serial_number=serial,
                        camera_model=ascii_value("MINF"),
                        camera_firmware=ascii_value("FMWR"),
                        media_id=media_id,
                        camera_creation_timestamp=creation,
                    )
    except IdentityError:
        raise
    except Exception as exc:
        raise IdentityError(f"Could not parse GoPro identity metadata from {path.name}: {exc}") from exc
    raise IdentityError(
        f"GoPro camera serial (CASN) is unavailable in the original file "
        f"{source_filename or path.name}; device identity is unresolved"
    )


@dataclass(frozen=True)
class SourceIdentity:
    device_id: str
    camera_serial_number: str
    camera_model: str | None
    camera_firmware: str | None
    source_file: str
    source_file_stem: str
    recording_id: str
    recording_fingerprint: str
    source_id: str
    file_id: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SourceIdentity":
        try:
            identity = cls(**payload)
        except (TypeError, KeyError) as exc:
            raise IdentityError("Cached source identity is invalid") from exc
        if not re.fullmatch(r"GP-(\d{6,})", identity.device_id):
            raise IdentityError("Cached device ID is invalid")
        if int(identity.device_id.removeprefix("GP-")) < 1:
            raise IdentityError("Cached device ID is invalid")
        if normalize_camera_serial(identity.camera_serial_number) != identity.camera_serial_number:
            raise IdentityError("Cached camera serial is invalid")
        if (
            identity.source_file in {"", ".", ".."}
            or Path(identity.source_file).name != identity.source_file
            or Path(identity.source_file).suffix.lower() != ".mp4"
        ):
            raise IdentityError("Cached original filename is invalid")
        expected_stem = source_stem_from_filename(identity.source_file)
        if identity.source_file_stem != expected_stem:
            raise IdentityError("Cached source filename stem is invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", identity.recording_fingerprint):
            raise IdentityError("Cached recording fingerprint is invalid")
        expected_recording_id = identity.recording_fingerprint[:8].upper()
        if identity.recording_id != expected_recording_id:
            raise IdentityError("Cached recording ID is invalid")
        if identity.file_id != identity.recording_fingerprint[:20]:
            raise IdentityError("Cached browser source ID is invalid")
        expected_source_id = f"{identity.device_id}_{expected_stem}_{expected_recording_id}"
        if identity.source_id != expected_source_id:
            raise IdentityError("Cached source ID is invalid")
        return identity


def recording_fingerprint(
    path: Path,
    filename: str,
    header: GoProHeaderMetadata,
    *,
    chunk_size: int = 1024 * 1024,
) -> str:
    """Hash stable GoPro metadata plus fixed first/last source chunks, never mtime."""
    size = path.stat().st_size
    canonical = {
        "schema": 1,
        "camera_serial_number": header.camera_serial_number,
        "source_file": filename,
        "source_size": size,
        "media_id": header.media_id,
        "camera_creation_timestamp": header.camera_creation_timestamp,
    }
    digest = hashlib.sha256()
    digest.update(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    with path.open("rb") as handle:
        digest.update(handle.read(chunk_size))
        if size > chunk_size:
            handle.seek(max(0, size - chunk_size))
            digest.update(handle.read(chunk_size))
    return digest.hexdigest()


def resolve_source_identity(
    path: Path,
    filename: str,
    registry: DeviceRegistry,
) -> SourceIdentity:
    header = extract_gopro_header_metadata(path, filename)
    device_id = registry.register(
        header.camera_serial_number,
        camera_model=header.camera_model,
        camera_firmware=header.camera_firmware,
    )
    fingerprint = recording_fingerprint(path, filename, header)
    recording_id = fingerprint[:8].upper()
    stem = source_stem_from_filename(filename)
    source_id = f"{device_id}_{stem}_{recording_id}"
    return SourceIdentity(
        device_id=device_id,
        camera_serial_number=header.camera_serial_number,
        camera_model=header.camera_model,
        camera_firmware=header.camera_firmware,
        source_file=filename,
        source_file_stem=stem,
        recording_id=recording_id,
        recording_fingerprint=fingerprint,
        source_id=source_id,
        file_id=fingerprint[:20],
    )
