from __future__ import annotations

import unittest

from app.services.common import (
    decode_json,
    encode_json,
    is_temporary_network_error,
    join_local_path,
    join_remote_path,
    normalize_remote_root,
)


class CommonHelpersTest(unittest.TestCase):
    def test_remote_root_adds_colon(self):
        self.assertEqual(normalize_remote_root("yadisk"), "yadisk:")

    def test_remote_path_join(self):
        self.assertEqual(join_remote_path("yadisk", "photos/2026"), "yadisk:photos/2026")

    def test_local_path_join(self):
        self.assertTrue(join_local_path("/tmp/base", "child").endswith("/tmp/base/child"))

    def test_json_helpers_roundtrip(self):
        payload = {"a": 1, "b": ["x"]}
        self.assertEqual(decode_json(encode_json(payload), {}), payload)

    def test_temporary_network_error_detection(self):
        self.assertTrue(is_temporary_network_error("TLS handshake timeout"))


if __name__ == "__main__":
    unittest.main()
