from pathlib import Path

from app.export import project_slug
from app.identity import source_stem_from_filename
from app.media import Source
from app.models import Clip, ProjectState
from app.projects import ProjectStore, next_clip_index


SOURCE_ID = "GP-000001_GX010005_A72F91C3"


def clip(index: int, label: str = "Sewing seam") -> Clip:
    return Clip(
        id=f"{SOURCE_ID}_{index:03d}",
        clip_index=index,
        task_label=label,
        requested_start_s=float(index),
        requested_end_s=float(index + 1),
    )


def test_source_filename_stem_extraction() -> None:
    assert source_stem_from_filename("GX010005.MP4") == "GX010005"
    assert source_stem_from_filename("camera day 1.mp4") == "camera_day_1"


def test_project_slug_sanitization() -> None:
    assert project_slug("Claru Textile Pilot") == "claru_textile_pilot"
    assert project_slug("Claru / Textile Pilot") == "claru_textile_pilot"
    assert project_slug("café project") == "cafe_project"


def test_multiple_clips_and_deleted_clip_are_not_renumbered() -> None:
    project = ProjectState(
        project_name="Claru Textile Pilot",
        source_file="GX010005.MP4",
        source_id=SOURCE_ID,
        device_id="GP-000001",
        camera_serial_number="C3531325778769",
        recording_id="A72F91C3",
        clips=[clip(1), clip(2, "Cutting fabric"), clip(3, "Removing stitching")],
        next_clip_index=4,
    )
    project.clips = [item for item in project.clips if item.clip_index != 2]
    assert [item.clip_index for item in project.clips] == [1, 3]
    assert next_clip_index(project) == 4


def test_deleting_highest_clip_does_not_reuse_number() -> None:
    project = ProjectState(
        project_name="Claru Textile Pilot",
        source_file="GX010005.MP4",
        source_id=SOURCE_ID,
        device_id="GP-000001",
        camera_serial_number="C3531325778769",
        recording_id="A72F91C3",
        clips=[clip(1), clip(2)],
        next_clip_index=3,
    )
    project.clips.pop()
    assert next_clip_index(project) == 3


def test_task_label_and_next_index_persist(tmp_path: Path) -> None:
    source = Source(
        file_id="a" * 20,
        filename="GX010005.MP4",
        source_id=SOURCE_ID,
        source_file_stem="GX010005",
        recording_id="A72F91C3",
        recording_fingerprint="a" * 64,
        device_id="GP-000001",
        camera_serial_number="C3531325778769",
        camera_model="HERO13 Black",
        camera_firmware="H24.01.02.10.00",
        path=tmp_path / "GX010005.MP4",
        cache_dir=tmp_path / "cache",
    )
    project = ProjectState(
        project_name="Claru Textile Pilot",
        source_file=source.filename,
        source_id=source.source_id,
        device_id=source.device_id,
        camera_serial_number=source.camera_serial_number,
        recording_id=source.recording_id,
        clips=[clip(1, "Cutting fabric")],
        next_clip_index=2,
    )
    store = ProjectStore()
    store.save(source, project, source_duration=10)
    loaded = store.load(source)
    assert loaded.clips[0].task_label == "Cutting fabric"
    assert loaded.project_name == "Claru Textile Pilot"
    assert loaded.next_clip_index == 2
