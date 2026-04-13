from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = BASE_DIR / "var"


@dataclass(slots=True)
class Settings:
    app_name: str = "Локальная панель синхронизации"
    data_dir: Path = Path(os.getenv("LOCAL_SYNC_DATA_DIR", DEFAULT_DATA_DIR))
    db_filename: str = "app.db"
    keychain_service_prefix: str = "com.localsync.dashboard"
    default_log_retention_days: int = 14
    default_retry_count: int = 1
    default_run_timeout_seconds: int = 60 * 60 * 6
    default_max_concurrent_runs: int = 1
    default_host: str = "127.0.0.1"
    default_port: int = 8000

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def templates_dir(self) -> Path:
        return BASE_DIR / "app" / "templates"

    @property
    def static_dir(self) -> Path:
        return BASE_DIR / "app" / "static"

    @property
    def scheduler_timezone(self):
        return datetime.now().astimezone().tzinfo

    def ensure_runtime_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
