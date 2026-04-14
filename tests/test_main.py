from __future__ import annotations

import unittest

from app.main import job_pre_run_checklist, job_transfer_warning, profile_diagnostic_hints
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

    def test_job_pre_run_checklist_for_remote_route(self):
        source = self.make_profile(ProfileType.S3_REMOTE, "s3:")
        target = self.make_profile(ProfileType.YANDEX_REMOTE, "yadisk:")

        checklist = job_pre_run_checklist(source, target)

        self.assertGreaterEqual(len(checklist), 3)
        self.assertTrue(any("sleep" in item for item in checklist))

    def test_job_pre_run_checklist_for_local_route(self):
        source = self.make_profile(ProfileType.LOCAL_FOLDER, "/tmp/source")
        target = self.make_profile(ProfileType.SYNOLOGY_SHARE, "/Volumes/nas")

        self.assertEqual(job_pre_run_checklist(source, target), [])

    def test_profile_diagnostic_hints_for_missing_s3_remote(self):
        profile = self.make_profile(ProfileType.S3_REMOTE, "missingremote:bucket")
        profile.status = "unreachable"
        profile.options_json = '{"endpoint":"https://s3.example.com","_diagnostics":{"message":"remote not found"}}'

        hints = profile_diagnostic_hints(profile, configured_remotes={"yadisk"})

        self.assertTrue(any("rclone remote" in item for item in hints))

    def test_profile_diagnostic_hints_for_yandex_auth_problem(self):
        profile = self.make_profile(ProfileType.YANDEX_REMOTE, "yadisk:")
        profile.status = "unreachable"
        profile.options_json = '{"_diagnostics":{"message":"401 Unauthorized"}}'

        hints = profile_diagnostic_hints(profile, configured_remotes={"yadisk"})

        self.assertTrue(any("OAuth" in item or "переподключ" in item for item in hints))


if __name__ == "__main__":
    unittest.main()
