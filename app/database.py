from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


settings.ensure_runtime_dirs()


class Base(DeclarativeBase):
    pass


engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def migrate_sync_jobs_table(bind=None) -> None:
    additive_columns = [
        ("rclone_transfers", "INTEGER"),
        ("rclone_checkers", "INTEGER"),
        ("rclone_fast_list", "BOOLEAN NOT NULL DEFAULT 0"),
    ]
    target_engine = bind or engine
    with target_engine.begin() as connection:
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(sync_jobs)")).fetchall()
        }
        for column_name, column_sql in additive_columns:
            if column_name in columns:
                continue
            connection.execute(text(f"ALTER TABLE sync_jobs ADD COLUMN {column_name} {column_sql}"))
