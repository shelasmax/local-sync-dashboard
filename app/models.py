from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ProfileType(StrEnum):
    LOCAL_FOLDER = "local_folder"
    SYNOLOGY_SHARE = "synology_share"
    S3_REMOTE = "s3_remote"
    YANDEX_REMOTE = "yandex_remote"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"


class StorageProfile(Base):
    __tablename__ = "storage_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    profile_type: Mapped[str] = mapped_column(String(32), index=True)
    root_path_or_remote: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    secret_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    options_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    source_jobs: Mapped[list["SyncJob"]] = relationship(
        back_populates="source_profile",
        foreign_keys="SyncJob.source_profile_id",
    )
    target_jobs: Mapped[list["SyncJob"]] = relationship(
        back_populates="target_profile",
        foreign_keys="SyncJob.target_profile_id",
    )


class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    source_profile_id: Mapped[int] = mapped_column(ForeignKey("storage_profiles.id"))
    source_path: Mapped[str] = mapped_column(String(512), default="")
    target_profile_id: Mapped[int] = mapped_column(ForeignKey("storage_profiles.id"))
    target_path: Mapped[str] = mapped_column(String(512), default="")
    schedule: Mapped[str] = mapped_column(String(120), default="manual")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    mode: Mapped[str] = mapped_column(String(16), default="copy")
    filters_json: Mapped[str] = mapped_column(Text, default="[]")
    bandwidth_limit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verify_checksum: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    source_profile: Mapped[StorageProfile] = relationship(
        back_populates="source_jobs",
        foreign_keys=[source_profile_id],
    )
    target_profile: Mapped[StorageProfile] = relationship(
        back_populates="target_jobs",
        foreign_keys=[target_profile_id],
    )
    runs: Mapped[list["RunHistory"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class RunHistory(Base):
    __tablename__ = "run_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("sync_jobs.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    log_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    command_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    bytes_transferred: Mapped[int] = mapped_column(Integer, default=0)
    files_transferred: Mapped[int] = mapped_column(Integer, default=0)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[SyncJob] = relationship(back_populates="runs")


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    log_retention_days: Mapped[int] = mapped_column(Integer, default=14)
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, default=1)
    default_retry_count: Mapped[int] = mapped_column(Integer, default=1)
    default_run_timeout_seconds: Mapped[int] = mapped_column(Integer, default=60 * 60 * 6)
