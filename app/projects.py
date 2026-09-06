from __future__ import annotations

import json
from pathlib import Path

from .media import Source
from .models import ProjectState


def next_clip_index(project: ProjectState) -> int:
    return project.next_clip_index


class ProjectStore:
    def path_for(self, source: Source) -> Path:
        return source.cache_dir / "project.json"

    def empty(self, source: Source) -> ProjectState:
        return ProjectState(source_file=source.filename, source_id=source.source_id)

    def load(self, source: Source) -> ProjectState:
        path = self.path_for(source)
        if not path.exists():
            return self.empty(source)
        try:
            project = ProjectState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError(f"Project state is invalid for {source.filename}")
        if project.source_file != source.filename or project.source_id != source.source_id:
            raise ValueError("Project source identity does not match selected media")
        return project

    def save(self, source: Source, project: ProjectState, source_duration: float) -> ProjectState:
        if project.source_file != source.filename or project.source_id != source.source_id:
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
