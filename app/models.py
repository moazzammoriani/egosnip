from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class IMUSettings(BaseModel):
    retain_one_pre_zero_sample: bool = False


class Clip(BaseModel):
    id: str
    clip_index: int = Field(ge=1)
    task_label: str = Field(min_length=1, max_length=240)
    requested_start_s: float = Field(ge=0)
    requested_end_s: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_interval(self) -> "Clip":
        if self.requested_end_s <= self.requested_start_s:
            raise ValueError("clip end must be after clip start")
        return self


class ProjectState(BaseModel):
    source_file: str
    source_id: str
    imu_settings: IMUSettings = Field(default_factory=IMUSettings)
    clips: list[Clip] = Field(default_factory=list)
    next_clip_index: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_unique_clip_identity(self) -> "ProjectState":
        ids = [clip.id for clip in self.clips]
        indices = [clip.clip_index for clip in self.clips]
        if len(ids) != len(set(ids)):
            raise ValueError("clip IDs must be unique")
        if len(indices) != len(set(indices)):
            raise ValueError("clip indices must be unique")
        minimum_next = max(indices, default=0) + 1
        if self.next_clip_index < minimum_next:
            self.next_clip_index = minimum_next
        return self


class ExportRequest(BaseModel):
    clip_ids: list[str] | None = None
