from pathlib import Path

import pytest
from pydantic import ValidationError

from app.export import ClipExporter, clip_basename, project_slug
from app.media import MediaLibrary, Source
from app.models import Clip, ProjectState
from app.projects import ProjectStore
from app.telemetry import TelemetryCache


def make_source(tmp_path: Path, source_id: str) -> Source:
    recording_id = "A72F91C3" if source_id.endswith("5") else "91BD204E"
    full_source_id = f"GP-000001_{source_id}_{recording_id}"
    return Source(
        file_id=("a" if source_id.endswith("5") else "b") * 20,
        filename=f"{source_id}.MP4",
        source_id=full_source_id,
        source_file_stem=source_id,
        recording_id=recording_id,
        recording_fingerprint=("a" if source_id.endswith("5") else "b") * 64,
        device_id="GP-000001",
        camera_serial_number="C3531325778769",
        camera_model="HERO13 Black",
        camera_firmware="H24.01.02.10.00",
        path=tmp_path / f"{source_id}.MP4",
        cache_dir=tmp_path / ".snipper_cache" / full_source_id,
    )


def make_clip(source_id: str, label: str = "deburring motorbike keys") -> Clip:
    return Clip(
        id=f"{source_id}_001",
        clip_index=1,
        task_label=label,
        requested_start_s=1.0,
        requested_end_s=3.0,
    )


def make_project(source: Source, name: str = "Claru Textile Pilot", label: str = "deburring motorbike keys") -> ProjectState:
    return ProjectState(
        project_name=name,
        source_file=source.filename,
        source_id=source.source_id,
        device_id=source.device_id,
        camera_serial_number=source.camera_serial_number,
        recording_id=source.recording_id,
        clips=[make_clip(source.source_id, label)],
        next_clip_index=2,
    )


def test_project_name_is_required_by_the_model() -> None:
    with pytest.raises(ValidationError):
        ProjectState(source_file="GX010005.MP4", source_id="GX010005")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Claru Textile Pilot", "claru_textile_pilot"),
        ("Claru / Textile Pilot", "claru_textile_pilot"),
        ("../Claru", "claru"),
        ("CLARU: Pilot #1", "claru_pilot_1"),
        ("  Claru   Pilot", "claru_pilot"),
        ("café project", "cafe_project"),
    ],
)
def test_project_slug_is_normalized_and_path_safe(name: str, expected: str) -> None:
    assert project_slug(name) == expected


@pytest.mark.parametrize("name", ["", "   ", "..", "///", "💥"])
def test_project_names_with_empty_slugs_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="Project Name"):
        project_slug(name)


def test_project_slug_has_a_bounded_safe_character_set() -> None:
    slug = project_slug("A" * 200)
    assert len(slug) == 80
    assert slug == "a" * 80


def test_project_name_and_task_label_save_reload_and_edit(tmp_path: Path) -> None:
    source = make_source(tmp_path, "GX010005")
    store = ProjectStore()
    project = make_project(source)
    store.save(source, project, source_duration=10.0)
    loaded = store.load(source)
    assert loaded.project_name == "Claru Textile Pilot"
    assert loaded.clips[0].task_label == "deburring motorbike keys"

    loaded.project_name = "Claru Production"
    store.save(source, loaded, source_duration=10.0)
    assert store.load(source).project_name == "Claru Production"
    assert store.load(source).clips[0].id == f"{source.source_id}_001"


def test_export_hierarchy_is_shared_by_sources_and_has_no_task_label(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path)
    exporter = ClipExporter(library, TelemetryCache(library))
    first_source = make_source(tmp_path, "GX010005")
    second_source = make_source(tmp_path, "GX010006")
    first_project = make_project(first_source)
    second_project = make_project(second_source)

    first_dir, first_base = exporter._output_dir(first_source, first_project, first_project.clips[0])
    second_dir, _ = exporter._output_dir(second_source, second_project, second_project.clips[0])

    assert first_dir == tmp_path / "exports" / "claru_textile_pilot" / first_source.source_id / f"{first_source.source_id}_001"
    assert second_dir == tmp_path / "exports" / "claru_textile_pilot" / second_source.source_id / f"{second_source.source_id}_001"
    assert first_dir.parents[1] == second_dir.parents[1]
    assert first_base == f"{first_source.source_id}_001"
    assert clip_basename(first_source, first_project.clips[0]) == first_project.clips[0].id
    generated_names = {
        f"{first_base}.mp4",
        f"{first_base}_imu.csv",
        f"{first_base}_accelerometer.csv",
        f"{first_base}_gyroscope.csv",
        f"{first_base}.json",
    }
    assert all("deburring" not in name for name in generated_names)
    assert "deburring" not in str(first_dir)


def test_project_or_task_rename_has_only_the_intended_path_effect(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path)
    exporter = ClipExporter(library, TelemetryCache(library))
    source = make_source(tmp_path, "GX010005")
    project = make_project(source, name="Claru Pilot", label="deburring motorbike keys")
    old_path, _ = exporter._output_dir(source, project, project.clips[0])

    project.clips[0].task_label = "deburring motorcycle keys"
    label_edit_path, _ = exporter._output_dir(source, project, project.clips[0])
    assert label_edit_path == old_path

    project.project_name = "Claru Production"
    renamed_project_path, _ = exporter._output_dir(source, project, project.clips[0])
    assert renamed_project_path == tmp_path / "exports" / "claru_production" / source.source_id / f"{source.source_id}_001"
    assert old_path.is_dir()


def test_failure_manifest_uses_clip_id_filename_and_keeps_task_metadata(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path)
    exporter = ClipExporter(library, TelemetryCache(library))
    source = make_source(tmp_path, "GX010005")
    project = make_project(source)

    result = exporter.write_failure_manifest(source, project, project.clips[0], "expected test failure")
    output_dir = tmp_path / result["output_dir"]
    manifest_path = output_dir / f"{source.source_id}_001.json"
    assert manifest_path.is_file()
    assert "deburring" not in manifest_path.name
    manifest = result["manifest"]
    assert manifest["manifest_schema_version"] == "1.0"
    assert manifest["project_name"] == "Claru Textile Pilot"
    assert manifest["project_slug"] == "claru_textile_pilot"
    assert manifest["task_label"] == "deburring motorbike keys"
    assert manifest["qc"]["status"] == "FAIL"
