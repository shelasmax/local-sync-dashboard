from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import ProfileType


class StorageProfilePayload(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    profile_type: ProfileType
    root_path_or_remote: str = Field(min_length=1, max_length=512)
    secret: str | None = Field(default=None, max_length=4000)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", "root_path_or_remote")
    @classmethod
    def strip_basic_fields(cls, value: str) -> str:
        return value.strip()


class StorageProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    profile_type: str
    root_path_or_remote: str
    status: str
    secret_ref: str | None
    options_json: str


class ConnectionTestPayload(BaseModel):
    profile_type: ProfileType
    root_path_or_remote: str = Field(min_length=1, max_length=512)
    options: dict[str, Any] = Field(default_factory=dict)


class SyncJobPayload(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    source_profile_id: int
    source_path: str = ""
    target_profile_id: int
    target_path: str = ""
    schedule: str = "manual"
    enabled: bool = True
    mode: str = "copy"
    filters: list[str] = Field(default_factory=list)
    bandwidth_limit: str | None = Field(default=None, max_length=32)
    verify_checksum: bool = False
    rclone_transfers: int | None = Field(default=None, ge=1, le=64)
    rclone_checkers: int | None = Field(default=None, ge=1, le=128)
    rclone_fast_list: bool = False

    @field_validator("name", "source_path", "target_path", "schedule", mode="before")
    @classmethod
    def strip_strings(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    @field_validator("mode")
    @classmethod
    def only_copy_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized != "copy":
            raise ValueError("MVP supports only copy mode.")
        return normalized

    @field_validator("filters", mode="before")
    @classmethod
    def normalize_filters(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("bandwidth_limit", mode="before")
    @classmethod
    def normalize_bandwidth_limit(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("rclone_transfers", "rclone_checkers", mode="before")
    @classmethod
    def normalize_optional_ints(cls, value):
        if value in (None, ""):
            return None
        return value


class SyncJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    source_profile_id: int
    source_path: str
    target_profile_id: int
    target_path: str
    schedule: str
    enabled: bool
    mode: str
    filters_json: str
    bandwidth_limit: str | None
    verify_checksum: bool
    rclone_transfers: int | None
    rclone_checkers: int | None
    rclone_fast_list: bool


class RunHistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    status: str
    summary: str
    log_path: str | None
    command_preview: str | None
    bytes_transferred: int
    files_transferred: int
    stdout: str
    stderr: str
