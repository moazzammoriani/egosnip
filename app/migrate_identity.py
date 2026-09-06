from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .media import MediaLibrary, Source
from .models import ProjectState


class LegacyMigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExportClipPlan:
    old_dir: Path
    new_clip_id: str
    manifest_path: Path
    manifest: dict[str, Any]
    renamed_files: dict[str, str]


@dataclass(frozen=True)
class ExportSourcePlan:
    old_dir: Path
    new_dir: Path
    clips: list[ExportClipPlan]


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def _legacy_cache_dir(library: MediaLibrary, source: Source) -> Path:
    legacy_file_id = hashlib.sha256(source.filename.encode("utf-8")).hexdigest()
    return library.cache_root / f"{source.source_file_stem}_{legacy_file_id[:8]}"


def _updated_project(source: Source, payload: dict[str, Any]) -> ProjectState:
    if payload.get("source_file") != source.filename:
        raise LegacyMigrationError("Legacy project source filename does not match")
    clips = []
    for raw_clip in payload.get("clips", []):
        clip = dict(raw_clip)
        clip["id"] = f"{source.source_id}_{int(clip['clip_index']):03d}"
        clips.append(clip)
    return ProjectState.model_validate({
        **payload,
        "source_id": source.source_id,
        "device_id": source.device_id,
        "camera_serial_number": source.camera_serial_number,
        "recording_id": source.recording_id,
        "clips": clips,
    })


def _plan_export_source(
    source: Source,
    old_source_dir: Path,
) -> ExportSourcePlan:
    if old_source_dir.is_symlink():
        raise LegacyMigrationError(f"Refusing symlinked legacy export: {old_source_dir}")
    new_source_dir = old_source_dir.parent / source.source_id
    if new_source_dir.exists():
        raise LegacyMigrationError(f"New export source directory already exists: {new_source_dir}")
    clips: list[ExportClipPlan] = []
    seen_indices: set[int] = set()
    entries = sorted(old_source_dir.iterdir())
    if any(path.is_symlink() or not path.is_dir() for path in entries):
        raise LegacyMigrationError(f"Unexpected artifact in legacy source directory: {old_source_dir}")
    for old_clip_dir in entries:
        manifests = list(old_clip_dir.glob("*.json"))
        if len(manifests) != 1:
            raise LegacyMigrationError(f"Expected one clip manifest in {old_clip_dir}")
        manifest_path = manifests[0]
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            clip_index = int(manifest["clip_index"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LegacyMigrationError(f"Invalid clip manifest: {manifest_path}") from exc
        if manifest.get("source_file") != source.filename:
            raise LegacyMigrationError(f"Manifest source does not match {source.filename}: {manifest_path}")
        if clip_index in seen_indices:
            raise LegacyMigrationError(f"Duplicate clip index {clip_index} in {old_source_dir}")
        seen_indices.add(clip_index)
        new_clip_id = f"{source.source_id}_{clip_index:03d}"
        old_base = manifest_path.stem
        renamed_files: dict[str, str] = {}
        for old_file in old_clip_dir.iterdir():
            if old_file.is_symlink() or not old_file.is_file() or not old_file.name.startswith(old_base):
                raise LegacyMigrationError(f"Unexpected legacy export artifact: {old_file}")
            renamed_files[old_file.name] = f"{new_clip_id}{old_file.name[len(old_base):]}"
        clips.append(ExportClipPlan(
            old_dir=old_clip_dir,
            new_clip_id=new_clip_id,
            manifest_path=manifest_path,
            manifest=manifest,
            renamed_files=renamed_files,
        ))
    return ExportSourcePlan(old_dir=old_source_dir, new_dir=new_source_dir, clips=clips)


def _update_manifest(
    source: Source,
    plan: ExportClipPlan,
) -> dict[str, Any]:
    manifest = dict(plan.manifest)
    manifest.update({
        "device_id": source.device_id,
        "camera_serial_number": source.camera_serial_number,
        "camera_model": source.camera_model,
        "camera_firmware": source.camera_firmware,
        "source_file_stem": source.source_file_stem,
        "recording_id": source.recording_id,
        "source_id": source.source_id,
        "clip_id": plan.new_clip_id,
    })
    manifest["imu_files"] = [
        plan.renamed_files.get(name, name) for name in manifest.get("imu_files", [])
    ]
    return manifest


def migrate_legacy_source(library: MediaLibrary, source: Source) -> dict[str, Any] | None:
    legacy_cache = _legacy_cache_dir(library, source)
    legacy_project_path = legacy_cache / "project.json"
    if not legacy_project_path.is_file():
        return None
    if legacy_cache.is_symlink() or legacy_project_path.is_symlink():
        raise LegacyMigrationError(f"Refusing symlinked legacy cache: {legacy_cache}")
    if source.cache_dir.exists():
        raise LegacyMigrationError(f"New identity cache already exists: {source.cache_dir}")
    try:
        legacy_payload = json.loads(legacy_project_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacyMigrationError(f"Legacy project is unreadable: {legacy_project_path}") from exc
    project = _updated_project(source, legacy_payload)
    old_source_id = str(legacy_payload.get("source_id", ""))
    if not old_source_id:
        raise LegacyMigrationError("Legacy project has no source ID")

    export_plans = []
    if library.exports_root.is_dir():
        for project_dir in library.exports_root.iterdir():
            old_export_dir = project_dir / old_source_id
            if project_dir.is_dir() and not project_dir.is_symlink() and old_export_dir.is_dir():
                export_plans.append(_plan_export_source(source, old_export_dir))

    legacy_cache.replace(source.cache_dir)
    _atomic_write_json(
        source.cache_dir / "project.json",
        project.model_dump(mode="json"),
    )

    migrated_clips = 0
    for source_plan in export_plans:
        for clip_plan in source_plan.clips:
            for old_name, new_name in clip_plan.renamed_files.items():
                (clip_plan.old_dir / old_name).replace(clip_plan.old_dir / new_name)
            new_manifest_path = clip_plan.old_dir / f"{clip_plan.new_clip_id}.json"
            _atomic_write_json(new_manifest_path, _update_manifest(source, clip_plan))
            new_clip_dir = source_plan.old_dir / clip_plan.new_clip_id
            clip_plan.old_dir.replace(new_clip_dir)
            migrated_clips += 1
        source_plan.old_dir.replace(source_plan.new_dir)

    return {
        "source_file": source.filename,
        "old_source_id": old_source_id,
        "source_id": source.source_id,
        "cache_dir": os.fspath(source.cache_dir.relative_to(library.media_dir)),
        "export_sources": len(export_plans),
        "clips": migrated_clips,
    }


def migrate_media_dir(media_dir: Path) -> list[dict[str, Any]]:
    library = MediaLibrary(media_dir)
    results = []
    for source in library.sources():
        result = migrate_legacy_source(library, source)
        if result is not None:
            results.append(result)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy EgoSnip identities in MEDIA_DIR")
    parser.add_argument(
        "--media-dir",
        type=Path,
        default=Path(os.environ.get("MEDIA_DIR", Path.cwd() / "media")),
    )
    args = parser.parse_args()
    results = migrate_media_dir(args.media_dir)
    print(json.dumps({"migrated": results}, indent=2))


if __name__ == "__main__":
    main()
