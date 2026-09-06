from pathlib import Path

from app.export import sanitize_task_label
from app.media import Source, source_id_from_filename
from app.models import Clip, ProjectState
from app.projects import ProjectStore, next_clip_index


def clip(index: int, label: str = "Sewing seam") -> Clip:
    return Clip(
        id=f"GX010005_{index:03d}",
        clip_index=index,
        task_label=label,
        requested_start_s=float(index),
        requested_end_s=float(index + 1),
    )


def test_source_id_extraction() -> None:
    assert source_id_from_filename("GX010005.MP4") == "GX010005"
    assert source_id_from_filename("camera day 1.mp4") == "camera_day_1"


def test_filename_sanitization() -> None:
    assert sanitize_task_label("Removing rough stitching") == "removing_rough_stitching"
    assert sanitize_task_label("  Cut / sew: final?  ") == "cut_sew_final"
    assert sanitize_task_label("***") == "untitled_task"


def test_multiple_clips_and_deleted_clip_are_not_renumbered() -> None:
    project = ProjectState(
        source_file="GX010005.MP4",
        source_id="GX010005",
        clips=[clip(1), clip(2, "Cutting fabric"), clip(3, "Removing stitching")],
        next_clip_index=4,
    )
    project.clips = [item for item in project.clips if item.clip_index != 2]
    assert [item.clip_index for item in project.clips] == [1, 3]
    assert next_clip_index(project) == 4


def test_deleting_highest_clip_does_not_reuse_number() -> None:
    project = ProjectState(
        source_file="GX010005.MP4",
        source_id="GX010005",
        clips=[clip(1), clip(2)],
        next_clip_index=3,
    )
    project.clips.pop()
    assert next_clip_index(project) == 3


def test_task_label_and_next_index_persist(tmp_path: Path) -> None:
    source = Source("a" * 20, "GX010005.MP4", "GX010005", tmp_path / "GX010005.MP4", tmp_path / "cache")
    project = ProjectState(
        source_file=source.filename,
        source_id=source.source_id,
        clips=[clip(1, "Cutting fabric")],
        next_clip_index=2,
    )
    store = ProjectStore()
    store.save(source, project, source_duration=10)
    loaded = store.load(source)
    assert loaded.clips[0].task_label == "Cutting fabric"
    assert loaded.next_clip_index == 2

