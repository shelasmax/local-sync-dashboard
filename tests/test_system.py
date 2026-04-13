from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.system import discover_local_candidates


class SystemDiscoveryTest(unittest.TestCase):
    def test_discovers_expected_local_candidates(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            icloud = root / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
            google = root / "Library" / "CloudStorage" / "GoogleDrive-test@example.com"
            volumes = root / "Volumes" / "Synology"

            icloud.mkdir(parents=True)
            google.mkdir(parents=True)
            volumes.mkdir(parents=True)

            candidates = discover_local_candidates(home=root, volumes_dir=root / "Volumes")

            self.assertIn(str(icloud), candidates["icloud"])
            self.assertIn(str(google), candidates["google_drive"])
            self.assertIn(str(volumes), candidates["mounted_volumes"])


if __name__ == "__main__":
    unittest.main()
