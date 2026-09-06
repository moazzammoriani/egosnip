from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from .media import Source
from .models import ProjectState


PROJECT_SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")


def project_slug(project_name: str, max_length: int = 80) -> str:
    """Return the safe, stable on-disk namespace for a human project name."""
    normalized = unicodedata.normalize("NFKD", project_name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", normalized.lower())
    slug = re.sub(r"_+", "_", slug).strip("_")[:max_length].rstrip("_")
    if not slug:
        raise ValueError("Project Name must contain at least one letter or number")
    return slug


class ExportProjectCatalog:
    """Filesystem project namespaces discovered directly beneath exports/."""

    def __init__(self, exports_root: Path):
        self.exports_root = exports_root.resolve()

    def _path_for_slug(self, slug: str) -> Path:
        if not PROJECT_SLUG_PATTERN.fullmatch(slug):
            raise ValueError("Project slug contains unsafe characters")
        path = (self.exports_root / slug).resolve()
        try:
            path.relative_to(self.exports_root)
        except ValueError as exc:
            raise ValueError("Project path escapes the exports directory") from exc
        return path

    @staticmethod
    def _metadata_path(project_dir: Path) -> Path:
        return project_dir / ".project.json"

    def _display_name(self, project_dir: Path, slug: str) -> str:
        metadata_path = self._metadata_path(project_dir)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            name = metadata["project_name"]
            if isinstance(name, str) and project_slug(name) == slug:
                return name
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass
        return slug.replace("_", " ").title()

    def list(self) -> list[dict[str, str]]:
        if not self.exports_root.is_dir():
            return []
        projects = []
        for path in self.exports_root.iterdir():
            if path.is_symlink() or not path.is_dir() or not PROJECT_SLUG_PATTERN.fullmatch(path.name):
                continue
            projects.append({
                "project_name": self._display_name(path, path.name),
                "project_slug": path.name,
            })
        return sorted(projects, key=lambda item: item["project_slug"])

    def ensure(self, project_name: str, *, require_new: bool = False) -> dict[str, str]:
        slug = project_slug(project_name)
        project_dir = self._path_for_slug(slug)
        if project_dir.exists():
            if project_dir.is_symlink() or not project_dir.is_dir():
                raise ValueError("Project namespace is not a safe directory")
            if require_new:
                raise FileExistsError(f"Project '{slug}' already exists; select it from the list")
        else:
            project_dir.mkdir(parents=True)
        metadata_path = self._metadata_path(project_dir)
        if not metadata_path.exists():
            metadata = {"project_name": project_name.strip(), "project_slug": slug}
            temp_path = project_dir / ".project.json.tmp"
            temp_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            temp_path.replace(metadata_path)
        return {"project_name": self._display_name(project_dir, slug), "project_slug": slug}


def next_clip_index(project: ProjectState) -> int:
    return project.next_clip_index


class ProjectStore:
    def path_for(self, source: Source) -> Path:
        return source.cache_dir / "project.json"

    def empty(self, source: Source) -> ProjectState:
        return ProjectState(
            project_name="",
            source_file=source.filename,
            source_id=source.source_id,
            device_id=source.device_id,
            camera_serial_number=source.camera_serial_number,
            recording_id=source.recording_id,
        )

    def load(self, source: Source) -> ProjectState:
        path = self.path_for(source)
        if not path.exists():
            return self.empty(source)
        try:
            project = ProjectState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError(f"Project state is invalid for {source.filename}")
        if (
            project.source_file != source.filename
            or project.source_id != source.source_id
            or project.device_id != source.device_id
            or project.camera_serial_number != source.camera_serial_number
            or project.recording_id != source.recording_id
        ):
            raise ValueError("Project source identity does not match selected media")
        return project

    def save(self, source: Source, project: ProjectState, source_duration: float) -> ProjectState:
        if (
            project.source_file != source.filename
            or project.source_id != source.source_id
            or project.device_id != source.device_id
            or project.camera_serial_number != source.camera_serial_number
            or project.recording_id != source.recording_id
        ):
            raise ValueError("Project source identity cannot be changed")
        for clip in project.clips:
            if clip.requested_end_s > source_duration + 1e-6:
                raise ValueError(
                    f"Clip {clip.id} ends after the source duration ({source_duration:.3f}s)"
                )
        source.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(source)
        temp = path.with_suffix(".tmp")
        temp.write_text(project.model_dump_json(indent=2), encoding="utf-8")
        temp.replace(path)
        return project
