from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from .identity import normalize_camera_serial, source_stem_from_filename


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
    project_name: str = Field(max_length=240)
    source_file: str
    source_id: str
    device_id: str = Field(pattern=r"^GP-\d{6,}$")
    camera_serial_number: str = Field(min_length=1, max_length=128)
    recording_id: str = Field(pattern=r"^[A-F0-9]{8}$")
    imu_settings: IMUSettings = Field(default_factory=IMUSettings)
    clips: list[Clip] = Field(default_factory=list)
    next_clip_index: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_unique_clip_identity(self) -> "ProjectState":
        if int(self.device_id.removeprefix("GP-")) < 1:
            raise ValueError("device ID number must be positive")
        if normalize_camera_serial(self.camera_serial_number) != self.camera_serial_number:
            raise ValueError("camera serial number must be normalized")
        expected_source_id = (
            f"{self.device_id}_{source_stem_from_filename(self.source_file)}_{self.recording_id}"
        )
        if self.source_id != expected_source_id:
            raise ValueError("source ID must match device, source filename, and recording ID")
        ids = [clip.id for clip in self.clips]
        indices = [clip.clip_index for clip in self.clips]
        if len(ids) != len(set(ids)):
            raise ValueError("clip IDs must be unique")
        if len(indices) != len(set(indices)):
            raise ValueError("clip indices must be unique")
        for clip in self.clips:
            if clip.id != f"{self.source_id}_{clip.clip_index:03d}":
                raise ValueError("clip ID must be source ID plus its stable clip index")
        minimum_next = max(indices, default=0) + 1
        if self.next_clip_index < minimum_next:
            self.next_clip_index = minimum_next
        return self


class ExportRequest(BaseModel):
    clip_ids: list[str] | None = None


class CreateProjectRequest(BaseModel):
    project_name: str = Field(min_length=1, max_length=240)
