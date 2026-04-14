from __future__ import annotations

import unittest

from app.main import job_transfer_warning
from app.models import ProfileType, StorageProfile


class MainHelpersTest(unittest.TestCase):
    def make_profile(self, profile_type: ProfileType, name: str) -> StorageProfile:
        return StorageProfile(
            name=name,
            profile_type=profile_type.value,
            root_path_or_remote=name,
            status="ready",
            options_json="{}",
        )

    def test_job_transfer_warning_for_two_remote_profiles(self):
        source = self.make_profile(ProfileType.S3_REMOTE, "s3:")
        target = self.make_profile(ProfileType.YANDEX_REMOTE, "yadisk:")

        warning = job_transfer_warning(source, target)

        self.assertIn("через локальный Mac", warning)

    def test_job_transfer_warning_for_one_remote_profile(self):
        source = self.make_profile(ProfileType.LOCAL_FOLDER, "/tmp/source")
        target = self.make_profile(ProfileType.YANDEX_REMOTE, "yadisk:")

        warning = job_transfer_warning(source, target)

        self.assertIn("выполняется локально", warning)

    def test_job_transfer_warning_for_two_local_profiles(self):
        source = self.make_profile(ProfileType.LOCAL_FOLDER, "/tmp/source")
        target = self.make_profile(ProfileType.SYNOLOGY_SHARE, "/Volumes/nas")

        self.assertEqual(job_transfer_warning(source, target), "")


if __name__ == "__main__":
    unittest.main()
