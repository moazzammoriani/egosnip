import json
import os
import re
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from app.export import ClipExporter
from app.identity import (
    DeviceRegistry,
    DeviceRegistryError,
    IdentityError,
    extract_gopro_header_metadata,
    format_device_id,
    normalize_camera_serial,
    resolve_source_identity,
)
from app.media import DEFAULT_DEVICE_REGISTRY_PATH, MediaLibrary, safe_file_id
from app.models import Clip, ProjectState
from app.telemetry import TelemetryCache


def mp4_box(key: str, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + key.encode("ascii") + payload


def extended_mp4_box(key: str, payload: bytes) -> bytes:
    return b"\x00\x00\x00\x01" + key.encode("ascii") + (len(payload) + 16).to_bytes(8, "big") + payload


def gpmf_klv(key: str, type_char: str | None, payload: bytes, struct_size: int = 1) -> bytes:
    repeat = len(payload) // struct_size
    header = key.encode("ascii") + bytes([0 if type_char is None else ord(type_char), struct_size])
    encoded = header + repeat.to_bytes(2, "big") + payload
    return encoded + (b"\x00" * ((-len(encoded)) % 4))


def write_gopro_header(
    path: Path,
    *,
    serial: str | None = "C0000000000001",
    model: str = "HERO13 Black",
    firmware: str = "H24.01.02.10.00",
    media_byte: int = 1,
) -> None:
    global_settings = b"".join(
        [
            gpmf_klv("DVID", "L", (1).to_bytes(4, "big"), 4),
            gpmf_klv("DVNM", "c", b"Global Settings\x00"),
            gpmf_klv("FMWR", "c", firmware.encode("ascii")),
            gpmf_klv("MINF", "c", model.encode("ascii") + b"\x00"),
            *([gpmf_klv("CASN", "c", serial.encode("ascii") + b"\x00")] if serial else []),
            gpmf_klv("MUID", "B", bytes([media_byte]) * 32),
            gpmf_klv("CDAT", "J", (1_700_000_000 + media_byte).to_bytes(8, "big"), 8),
        ]
    )
    gpmf = mp4_box("GPMF", gpmf_klv("DEVC", None, global_settings))
    contents = mp4_box("ftyp", b"mp41\x00\x00\x00\x00mp41") + mp4_box("moov", mp4_box("udta", gpmf))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)


def project_for(source, clip: Clip) -> ProjectState:
    return ProjectState(
        project_name="Identity Test",
        source_file=source.filename,
        source_id=source.source_id,
        device_id=source.device_id,
        camera_serial_number=source.camera_serial_number,
        recording_id=source.recording_id,
        clips=[clip],
        next_clip_index=2,
    )


def test_serial_normalization_and_synthetic_header_extraction(tmp_path: Path) -> None:
    path = tmp_path / "GX010005.MP4"
    write_gopro_header(path, serial=" cAb123 ")
    metadata = extract_gopro_header_metadata(path)
    assert normalize_camera_serial(" cAb123 \n") == "CAB123"
    assert metadata.camera_serial_number == "CAB123"
    assert metadata.camera_model == "HERO13 Black"
    assert metadata.camera_firmware == "H24.01.02.10.00"


def test_header_extraction_skips_extended_size_mp4_atoms(tmp_path: Path) -> None:
    path = tmp_path / "GX010005.MP4"
    write_gopro_header(path)
    original = path.read_bytes()
    first_box_size = int.from_bytes(original[:4], "big")
    path.write_bytes(
        original[:first_box_size]
        + extended_mp4_box("mdat", b"large-file-layout")
        + original[first_box_size:]
    )

    metadata = extract_gopro_header_metadata(path)

    assert metadata.camera_serial_number == "C0000000000001"


def test_missing_casn_is_explicitly_unresolved(tmp_path: Path) -> None:
    path = tmp_path / "GX010005.MP4"
    write_gopro_header(path, serial=None)
    with pytest.raises(IdentityError, match="CASN.*unresolved"):
        extract_gopro_header_metadata(path)


def test_source_discovery_omits_unresolved_device_and_reports_reason(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "media")
    path = library.media_dir / "GX010005.MP4"
    write_gopro_header(path, serial=None)

    assert library.sources() == []
    assert library.discovery_errors == [{
        "filename": "GX010005.MP4",
        "identity_status": "unresolved",
        "error": (
            "GoPro camera serial (CASN) is unavailable in the original file "
            "GX010005.MP4; device identity is unresolved"
        ),
    }]


def test_media_library_keeps_device_registry_at_repository_root(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "media")
    assert library.device_registry.path == DEFAULT_DEVICE_REGISTRY_PATH.resolve()
    assert library.identity_cache_root == library.media_dir / ".egosnip" / "source_identities"


def test_identity_work_for_different_sources_does_not_share_one_lock(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "media")
    first = library.media_dir / "first.mp4"
    second = library.media_dir / "second.mp4"
    assert library._identity_lock_for(first) is library._identity_lock_for(first)
    assert library._identity_lock_for(first) is not library._identity_lock_for(second)


def test_device_registry_allocates_reuses_and_never_recycles(tmp_path: Path) -> None:
    registry_path = tmp_path / ".egosnip" / "devices.json"
    registry = DeviceRegistry(registry_path, now=lambda: "2026-09-06T18:00:00Z")
    assert registry.register(" c0001 ") == "GP-000001"
    assert registry.register("C0002") == "GP-000002"
    assert registry.register("C0001") == "GP-000001"

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    del payload["devices"]["C0002"]
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    assert registry.register("C0003") == "GP-000003"
    assert payload["next_device_number"] == 3


def test_device_id_padding_continues_past_six_digits(tmp_path: Path) -> None:
    assert format_device_id(1) == "GP-000001"
    assert format_device_id(99_999) == "GP-099999"
    assert format_device_id(999_999) == "GP-999999"
    assert format_device_id(1_000_000) == "GP-1000000"
    path = tmp_path / "devices.json"
    path.write_text(json.dumps({"schema_version": 1, "next_device_number": 1_000_000, "devices": {}}))
    assert DeviceRegistry(path).register("C1000000") == "GP-1000000"


def test_registry_write_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replaced = []
    original_replace = Path.replace

    def replace(source: Path, target: Path):
        replaced.append((source, target))
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    path = tmp_path / ".egosnip" / "devices.json"
    DeviceRegistry(path).register("C0001")
    assert replaced and replaced[-1][1] == path.resolve()
    assert not list(path.parent.glob("*.tmp"))


def test_device_registry_uses_windows_file_locking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    fake_msvcrt = SimpleNamespace(
        LK_LOCK=1,
        LK_UNLCK=2,
        locking=lambda file_descriptor, mode, size: calls.append(
            (file_descriptor, mode, size)
        ),
    )
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    registry = DeviceRegistry(tmp_path / "devices.json")

    with registry._process_lock():
        pass

    assert [mode for _, mode, _ in calls] == [fake_msvcrt.LK_LOCK, fake_msvcrt.LK_UNLCK]
    assert all(size == 1 for _, _, size in calls)


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        json.dumps({"schema_version": 1, "next_device_number": 2, "devices": {" c1 ": {"device_id": "GP-000001"}}}),
        json.dumps({"schema_version": 1, "next_device_number": 1, "devices": {"C0": {"device_id": "GP-000000"}}}),
        json.dumps({"schema_version": 1, "next_device_number": 1, "devices": {"C1": {"device_id": "GP-000001"}}}),
        json.dumps({
            "schema_version": 1,
            "next_device_number": 3,
            "devices": {
                "C1": {"device_id": "GP-000001"},
                "C2": {"device_id": "GP-000001"},
            },
        }),
    ],
)
def test_malformed_registry_fails_without_reset(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "devices.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(DeviceRegistryError):
        DeviceRegistry(path).register("C3")
    assert path.read_text(encoding="utf-8") == payload


def test_recording_ids_are_stable_specific_and_uppercase(tmp_path: Path) -> None:
    first = tmp_path / "first" / "GX010005.MP4"
    copy = tmp_path / "copy" / "GX010005.MP4"
    other = tmp_path / "other" / "GX010005.MP4"
    write_gopro_header(first, media_byte=1)
    copy.parent.mkdir()
    copy.write_bytes(first.read_bytes())
    write_gopro_header(other, media_byte=2)
    registry = DeviceRegistry(tmp_path / "devices.json")

    first_identity = resolve_source_identity(first, first.name, registry)
    copy_identity = resolve_source_identity(copy, copy.name, registry)
    other_identity = resolve_source_identity(other, other.name, registry)

    assert first_identity.recording_id == copy_identity.recording_id
    assert first_identity.file_id == copy_identity.file_id
    assert first_identity.recording_id != other_identity.recording_id
    assert first_identity.source_id != other_identity.source_id
    assert re.fullmatch(r"[A-F0-9]{8}", first_identity.recording_id)
    assert first_identity.file_id == first_identity.recording_fingerprint[:20]


def test_old_filename_only_safe_id_is_not_accepted() -> None:
    fingerprint = "a" * 64
    assert safe_file_id(fingerprint) == "a" * 20
    with pytest.raises(ValueError, match="recording fingerprint"):
        safe_file_id("GX010005.MP4")


def test_identity_metadata_is_cached_by_source_size_and_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = MediaLibrary(tmp_path / "media", tmp_path / "devices.json")
    source_path = library.media_dir / "GX010005.MP4"
    write_gopro_header(source_path)
    calls = 0

    from app import media as media_module

    original = media_module.resolve_source_identity

    def counted_resolve(path: Path, filename: str, registry: DeviceRegistry):
        nonlocal calls
        calls += 1
        return original(path, filename, registry)

    monkeypatch.setattr(media_module, "resolve_source_identity", counted_resolve)
    first = library.sources()[0]
    second = library.sources()[0]

    assert first.source_id == second.source_id
    assert calls == 1


def test_corrupt_cached_identity_cannot_create_unsafe_cache_path(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "media", tmp_path / "devices.json")
    source_path = library.media_dir / "GX010005.MP4"
    write_gopro_header(source_path)
    original = library.sources()[0]
    cache_path = library._identity_cache_path(source_path)
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["identity"]["source_id"] = "../../outside"
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    resolved = library.sources()[0]

    assert resolved.source_id == original.source_id
    assert resolved.cache_dir.is_relative_to(library.media_dir)


def test_identical_filenames_from_two_devices_have_isolated_storage_and_exports(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "media", tmp_path / "devices.json")
    first_upload = library.media_dir / ".uploads" / "first.partial"
    second_upload = library.media_dir / ".uploads" / "second.partial"
    write_gopro_header(first_upload, serial="CAMERA-A", media_byte=1)
    write_gopro_header(second_upload, serial="CAMERA-B", media_byte=2)
    first = library.ingest_upload(first_upload, "GX010005.MP4")
    second = library.ingest_upload(second_upload, "GX010005.MP4")

    assert first.device_id == "GP-000001"
    assert second.device_id == "GP-000002"
    assert first.source_id != second.source_id
    assert first.path != second.path
    assert first.cache_dir != second.cache_dir
    assert first.path.name == second.path.name == "GX010005.MP4"
    assert first.path.is_relative_to(library.media_dir)
    assert second.path.is_relative_to(library.media_dir)

    first_clip = Clip(
        id=f"{first.source_id}_001", clip_index=1, task_label="Same label",
        requested_start_s=0, requested_end_s=1,
    )
    second_clip = Clip(
        id=f"{second.source_id}_001", clip_index=1, task_label="Same label",
        requested_start_s=0, requested_end_s=1,
    )
    exporter = ClipExporter(library, TelemetryCache(library))
    first_export, _ = exporter._output_dir(first, project_for(first, first_clip), first_clip)
    second_export, _ = exporter._output_dir(second, project_for(second, second_clip), second_clip)
    assert first_clip.id != second_clip.id
    assert first_export != second_export


def test_same_device_reused_filename_has_distinct_recording_storage(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "media", tmp_path / "devices.json")
    first_upload = library.media_dir / ".uploads" / "first.partial"
    second_upload = library.media_dir / ".uploads" / "second.partial"
    write_gopro_header(first_upload, serial="CAMERA-A", media_byte=1)
    write_gopro_header(second_upload, serial="CAMERA-A", media_byte=2)
    first = library.ingest_upload(first_upload, "GX010005.MP4")
    second = library.ingest_upload(second_upload, "GX010005.MP4")
    assert first.device_id == second.device_id == "GP-000001"
    assert first.recording_id != second.recording_id
    assert first.source_id != second.source_id
    assert first.path != second.path
    assert first.cache_dir != second.cache_dir

    first_clip = Clip(
        id=f"{first.source_id}_001", clip_index=1, task_label="First recording",
        requested_start_s=0, requested_end_s=1,
    )
    second_clip = Clip(
        id=f"{second.source_id}_001", clip_index=1, task_label="Later recording",
        requested_start_s=0, requested_end_s=1,
    )
    exporter = ClipExporter(library, TelemetryCache(library))
    first_export, _ = exporter._output_dir(first, project_for(first, first_clip), first_clip)
    second_export, _ = exporter._output_dir(second, project_for(second, second_clip), second_clip)
    assert first_clip.id != second_clip.id
    assert first_export != second_export


def test_exact_duplicate_upload_never_overwrites_source(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "media", tmp_path / "devices.json")
    first_upload = library.media_dir / ".uploads" / "first.partial"
    duplicate_upload = library.media_dir / ".uploads" / "duplicate.partial"
    write_gopro_header(first_upload)
    duplicate_upload.parent.mkdir(parents=True, exist_ok=True)
    duplicate_upload.write_bytes(first_upload.read_bytes())
    source = library.ingest_upload(first_upload, "GX010005.MP4")
    original = source.path.read_bytes()
    with pytest.raises(Exception, match="not overwritten"):
        library.ingest_upload(duplicate_upload, "GX010005.MP4")
    assert source.path.read_bytes() == original


MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", ".")).resolve()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("filename", "serial", "model"),
    [
        ("GX010005.MP4", "C3531325778769", "HERO13 Black"),
        ("GX020001.MP4", "C3461325328633", "HERO10 Black"),
    ],
)
def test_real_hero_serial_metadata(filename: str, serial: str, model: str) -> None:
    path = MEDIA_DIR / filename
    if not path.is_file():
        pytest.skip(f"{filename} is not present in MEDIA_DIR")
    metadata = extract_gopro_header_metadata(path)
    assert metadata.camera_serial_number == serial
    assert metadata.camera_model == model
    assert metadata.camera_firmware
