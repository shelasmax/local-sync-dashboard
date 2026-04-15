from __future__ import annotations

import json
import unittest

from app.services.rclone import parse_progress_line, summarize_rclone_error


class RcloneProgressParsingTest(unittest.TestCase):
    def test_parse_progress_line_from_json_stats(self):
        line = json.dumps(
            {
                "stats": {
                    "bytes": 1024,
                    "totalBytes": 4096,
                    "transfers": 3,
                    "totalTransfers": 12,
                    "speed": 256.5,
                    "eta": 120,
                    "listed": 99,
                    "checks": 4,
                    "totalChecks": 7,
                    "transferring": [
                        {
                            "name": "Documents/report.pdf",
                            "bytes": 512,
                            "size": 1024,
                            "percentage": 50,
                            "speed": 128,
                            "eta": 4,
                        }
                    ],
                }
            }
        )

        progress = parse_progress_line(line)

        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.bytes_transferred, 1024)
        self.assertEqual(progress.total_bytes, 4096)
        self.assertEqual(progress.files_transferred, 3)
        self.assertEqual(progress.total_files, 12)
        self.assertEqual(progress.eta_seconds, 120)
        self.assertEqual(progress.listed, 99)
        self.assertEqual(progress.active_items[0]["name"], "Documents/report.pdf")
        self.assertEqual(progress.active_items[0]["percentage"], 50)

    def test_parse_progress_line_ignores_non_json(self):
        self.assertIsNone(parse_progress_line("plain text"))

    def test_summarize_rclone_error_for_missing_source_directory(self):
        output = "\n".join(
            [
                json.dumps({"level": "error", "msg": "error listing source root directory: directory not found"}),
                json.dumps({"level": "notice", "msg": "Failed to size with 2 errors: last error was: directory not found"}),
            ]
        )

        summary = summarize_rclone_error(output, returncode=3)

        self.assertIn("Источник или подпапка не найдены", summary)

    def test_summarize_rclone_error_for_missing_remote(self):
        output = 'Failed to create file system for "missing:": didn\'t find section in config file'

        summary = summarize_rclone_error(output)

        self.assertIn("remote не найден", summary)

    def test_summarize_rclone_error_for_synology_home_path(self):
        output = json.dumps(
            {
                "level": "error",
                "msg": "error reading source root directory: directory not found",
                "object": "Local file system at /home/Ext_HDD",
            }
        )

        summary = summarize_rclone_error(output, returncode=3)

        self.assertIn("`/Volumes`", summary)

    def test_summarize_rclone_error_for_duplicated_synology_segment(self):
        output = json.dumps(
            {
                "level": "error",
                "msg": "error reading source root directory: directory not found",
                "object": "Local file system at /Volumes/home/Ext_HDD/Ext_HDD",
            }
        )

        summary = summarize_rclone_error(output, returncode=3)

        self.assertIn("дублирует", summary)


if __name__ == "__main__":
    unittest.main()
