from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import ProfileType, StorageProfile, SyncJob
from app.schemas import StorageProfilePayload
from app.services.common import encode_json
from app.services.profiles import (
    build_s3_remote_root,
    check_profile,
    ProfileConflictError,
    ProfileInUseError,
    create_profile,
    delete_profile,
    split_s3_remote_root,
    update_profile,
)


class ProfilesServiceTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        TestingSession = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self.session = TestingSession()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self):
        self.session.close()
        Base.metadata.drop_all(self.engine)
        self.tmp_dir.cleanup()

    def test_create_profile_rejects_duplicate_name(self):
        source_dir = self.root / "icloud"
        source_dir.mkdir()
        payload = StorageProfilePayload(
            name="Yandex Disk",
            profile_type=ProfileType.LOCAL_FOLDER,
            root_path_or_remote=str(source_dir),
        )

        create_profile(self.session, payload)

        with self.assertRaises(ProfileConflictError):
            create_profile(self.session, payload)

    def test_update_profile_changes_root_and_status(self):
        source_dir = self.root / "google"
        source_dir.mkdir()
        profile = create_profile(
            self.session,
            StorageProfilePayload(
                name="Google Drive",
                profile_type=ProfileType.LOCAL_FOLDER,
                root_path_or_remote=str(source_dir),
            ),
        )

        updated_dir = self.root / "google-updated"
        updated_dir.mkdir()
        updated = update_profile(
            self.session,
            profile.id,
            StorageProfilePayload(
                name="Google Drive Sync",
                profile_type=ProfileType.LOCAL_FOLDER,
                root_path_or_remote=str(updated_dir),
            ),
        )

        self.assertEqual(updated.name, "Google Drive Sync")
        self.assertEqual(updated.root_path_or_remote, str(updated_dir))
        self.assertEqual(updated.status, "ready")

    def test_delete_profile_rejects_in_use_profile(self):
        source_dir = self.root / "volume"
        source_dir.mkdir()
        profile = create_profile(
            self.session,
            StorageProfilePayload(
                name="Synology",
                profile_type=ProfileType.LOCAL_FOLDER,
                root_path_or_remote=str(source_dir),
            ),
        )
        other_profile = StorageProfile(
            name="Yandex",
            profile_type=ProfileType.YANDEX_REMOTE.value,
            root_path_or_remote="yadisk:",
            status="ready",
            options_json=encode_json({}),
        )
        self.session.add(other_profile)
        self.session.commit()
        self.session.refresh(other_profile)

        job = SyncJob(
            name="Synology -> Yandex",
            source_profile_id=profile.id,
            target_profile_id=other_profile.id,
            source_path="",
            target_path="",
            schedule="manual",
            enabled=True,
            mode="copy",
            filters_json="[]",
            verify_checksum=False,
        )
        self.session.add(job)
        self.session.commit()

        with self.assertRaises(ProfileInUseError):
            delete_profile(self.session, profile.id)

    def test_delete_profile_removes_unused_profile(self):
        source_dir = self.root / "icloud-delete"
        source_dir.mkdir()
        profile = create_profile(
            self.session,
            StorageProfilePayload(
                name="iCloud",
                profile_type=ProfileType.LOCAL_FOLDER,
                root_path_or_remote=str(source_dir),
            ),
        )

        with patch("app.services.profiles.delete_secret") as delete_secret_mock:
            delete_profile(self.session, profile.id)

        self.assertIsNone(self.session.get(StorageProfile, profile.id))
        delete_secret_mock.assert_not_called()

    def test_split_s3_remote_root(self):
        self.assertEqual(
            split_s3_remote_root("s3home:photos-archive/backups/2026"),
            ("s3home", "photos-archive", "backups/2026"),
        )

    def test_build_s3_remote_root(self):
        self.assertEqual(
            build_s3_remote_root("s3home", "photos-archive", "backups/2026"),
            "s3home:photos-archive/backups/2026",
        )

    def test_check_profile_updates_status_to_ready(self):
        source_dir = self.root / "check-ready"
        source_dir.mkdir()
        profile = create_profile(
            self.session,
            StorageProfilePayload(
                name="Check Me",
                profile_type=ProfileType.LOCAL_FOLDER,
                root_path_or_remote=str(source_dir),
            ),
        )
        profile.status = "unreachable"
        self.session.commit()

        result = check_profile(self.session, profile.id)

        self.assertTrue(result.ok)
        self.assertIn("Локальный путь доступен", result.message)
        self.assertEqual(self.session.get(StorageProfile, profile.id).status, "ready")

    def test_check_profile_updates_status_to_unreachable(self):
        missing_dir = self.root / "missing-folder"
        profile = create_profile(
            self.session,
            StorageProfilePayload(
                name="Broken Path",
                profile_type=ProfileType.LOCAL_FOLDER,
                root_path_or_remote=str(self.root),
            ),
        )
        profile.root_path_or_remote = str(missing_dir)
        self.session.commit()

        result = check_profile(self.session, profile.id)

        self.assertFalse(result.ok)
        self.assertIn("Путь не существует", result.message)
        self.assertEqual(self.session.get(StorageProfile, profile.id).status, "unreachable")


if __name__ == "__main__":
    unittest.main()
