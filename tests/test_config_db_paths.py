"""空配置回退到标准数据库路径。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from config_db_paths import apply_discovered_db_paths, diagnostic_db_path


class ConfigDbPathsTests(unittest.TestCase):
    def test_empty_music_path_defaults_to_canonical_on_host(self):
        canonical = "/usr/local/apps/@appdata/trim.music/db/music.db"
        self.assertEqual(diagnostic_db_path("music_db_path", ""), canonical)
        with patch("utils.access_guidance.in_container", return_value=False), \
             patch("config_db_paths.discover_db_path", return_value=""):
            out, notes = apply_discovered_db_paths({"music_db_path": ""})
        self.assertEqual(out["music_db_path"], canonical)
        self.assertTrue(any("music_db_path=" in n for n in notes))

    def test_container_does_not_invent_host_music_path(self):
        with patch("utils.access_guidance.in_container", return_value=True), \
             patch("config_db_paths.discover_db_path", return_value=""):
            out, _ = apply_discovered_db_paths({"music_db_path": ""})
        self.assertEqual(out.get("music_db_path") or "", "")


if __name__ == "__main__":
    unittest.main()
