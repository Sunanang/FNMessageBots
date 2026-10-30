"""音乐库轮询：临时 SQLite、模拟时钟；不访问 NAS、不发送推送。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from monitor.music_db_poller import (
    MUSIC_FAVORITED,
    MUSIC_PLAYLIST_TRACK_ADDED,
    MUSIC_TRACK_ADDED,
    MUSIC_TRACK_REMOVED,
    MusicDBPoller,
)
from monitor.music_summary import MUSIC_SUMMARY_EVENT, format_music_summary


LIBRARY_EVENTS = [MUSIC_TRACK_ADDED, MUSIC_TRACK_REMOVED]
ALL_EVENTS = LIBRARY_EVENTS + [MUSIC_FAVORITED, MUSIC_PLAYLIST_TRACK_ADDED]


class MusicDBPollerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.db = self.base / "music.db"
        with sqlite3.connect(self.db) as writer:
            writer.executescript(
                """
                CREATE TABLE track(
                    id INTEGER PRIMARY KEY,
                    title TEXT,
                    guid TEXT,
                    album_id INTEGER,
                    created_at INTEGER,
                    is_audio_file_deleted INTEGER DEFAULT 0,
                    is_admin_deleted INTEGER DEFAULT 0
                );
                CREATE TABLE album(id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE artist(id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE track_artist(track_id INTEGER, artist_id INTEGER);
                CREATE TABLE favorite_track(
                    id INTEGER PRIMARY KEY,
                    track_id INTEGER,
                    user_id INTEGER,
                    created_at INTEGER
                );
                CREATE TABLE playlist(id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE playlist_track(
                    id INTEGER PRIMARY KEY,
                    playlist_id INTEGER,
                    track_id INTEGER,
                    user_id INTEGER,
                    added_at INTEGER
                );
                CREATE TABLE user(id INTEGER PRIMARY KEY, guid TEXT, nas_uid INTEGER, name TEXT);
                INSERT INTO album(id, name) VALUES (1, 'Demo Album');
                INSERT INTO artist(id, name) VALUES (1, 'Demo Artist');
                INSERT INTO playlist(id, name) VALUES (1, '我的歌单');
                INSERT INTO user(id, guid, nas_uid, name) VALUES (1, 'u-guid', 1000, 'alice');
                """
            )
        self.now = 1000.0
        manager = patch("monitor.music_db_poller.time.time", side_effect=lambda: self.now)
        manager.start()
        self.addCleanup(manager.stop)
        self.poller = MusicDBPoller(str(self.db), str(self.base), monitor_events=ALL_EVENTS)
        self.summary = Mock(return_value=True)
        self.fav = Mock(return_value=True)
        self.playlist = Mock(return_value=True)
        self.poller.add_handler(MUSIC_SUMMARY_EVENT, self.summary)
        self.poller.add_handler(MUSIC_FAVORITED, self.fav)
        self.poller.add_handler(MUSIC_PLAYLIST_TRACK_ADDED, self.playlist)
        self.state = self.poller._load_state()
        self.poller._poll_once(self.state)

    def add_track(self, track_id: int, title: str = "歌", deleted: int = 0, ts: int = 100):
        with sqlite3.connect(self.db) as writer:
            writer.execute(
                "INSERT INTO track(id, title, guid, album_id, created_at, is_audio_file_deleted, is_admin_deleted) "
                "VALUES (?,?,?,?,?,?,0)",
                (track_id, title, f"g-{track_id}", 1, ts, deleted),
            )
            writer.execute("INSERT INTO track_artist(track_id, artist_id) VALUES (?, 1)", (track_id,))

    def soft_delete(self, track_id: int):
        with sqlite3.connect(self.db) as writer:
            writer.execute("UPDATE track SET is_audio_file_deleted=1 WHERE id=?", (track_id,))

    def poll(self, advance: float = 0):
        self.now += advance
        self.poller._poll_once(self.state)

    def test_baseline_skips_history(self):
        self.add_track(1, "旧歌")
        # 重新对齐
        self.state["initialized"] = False
        self.poll()
        self.summary.assert_not_called()
        self.fav.assert_not_called()
        self.assertEqual(self.state["last_track_id"], 1)

    def test_library_add_and_remove_summary_after_quiet_window(self):
        self.add_track(10, "新歌A")
        self.add_track(11, "新歌B")
        self.poll()
        self.assertEqual(len(self.state["music_pending"]["tracks"]), 2)
        self.poll(59)
        self.summary.assert_not_called()
        self.poll(1)
        self.summary.assert_called_once()
        data = self.summary.call_args.args[0]
        self.assertEqual(data["by_type"].get(MUSIC_TRACK_ADDED), 2)
        self.assertIn("音乐库本轮汇总", format_music_summary(data))

        self.summary.reset_mock()
        self.soft_delete(10)
        self.poll()
        self.poll(60)
        self.summary.assert_called_once()
        removed = self.summary.call_args.args[0]
        self.assertEqual(removed["by_type"].get(MUSIC_TRACK_REMOVED), 1)

    def test_favorite_and_playlist_emit_immediately(self):
        self.add_track(20, "收藏曲")
        with sqlite3.connect(self.db) as writer:
            writer.execute(
                "INSERT INTO favorite_track(id, track_id, user_id, created_at) VALUES (1, 20, 1, 200)"
            )
            writer.execute(
                "INSERT INTO playlist_track(id, playlist_id, track_id, user_id, added_at) "
                "VALUES (1, 1, 20, 1, 201)"
            )
        self.poll()
        self.fav.assert_called_once()
        fav_data = self.fav.call_args.args[0]
        self.assertEqual(fav_data["title"], "收藏曲")
        self.assertEqual(fav_data["artist"], "Demo Artist")
        self.assertEqual(fav_data["user_label"], "alice")

        self.playlist.assert_called_once()
        pl_data = self.playlist.call_args.args[0]
        self.assertEqual(pl_data["playlist_name"], "我的歌单")
        self.assertEqual(pl_data["title"], "收藏曲")

    def test_unselected_library_events_do_not_accumulate(self):
        self.poller.update_config(monitor_events=[MUSIC_FAVORITED])
        self.add_track(30, "忽略入库")
        self.poll()
        self.assertFalse(self.state.get("music_pending", {}).get("tracks"))
        self.summary.assert_not_called()

    def test_missing_artist_tables_do_not_abort_poll(self):
        with sqlite3.connect(self.db) as writer:
            writer.execute("DROP TABLE track_artist")
            writer.execute("DROP TABLE artist")
            writer.execute(
                "INSERT INTO track(id, title, guid, album_id, created_at, is_audio_file_deleted, is_admin_deleted) "
                "VALUES (40, '无艺人表', 'g-40', 1, 100, 0, 0)"
            )
        self.poller._schema_cache.clear()
        self.poll()
        self.assertIn("track:40", self.state["music_pending"]["tracks"])

    def test_add_then_remove_same_track_nets_to_zero(self):
        self.add_track(50, "闪现")
        self.poll()
        self.soft_delete(50)
        self.poll()
        pending = self.state.get("music_pending") or {}
        self.assertFalse(pending.get("tracks"))
        self.assertEqual(int((pending.get("stage_counts") or {}).get(MUSIC_TRACK_ADDED) or 0), 0)
        self.assertEqual(int((pending.get("stage_counts") or {}).get(MUSIC_TRACK_REMOVED) or 0), 0)

    def test_favorite_emit_failure_does_not_advance_cursor(self):
        self.add_track(60, "收藏失败")
        with sqlite3.connect(self.db) as writer:
            writer.execute(
                "INSERT INTO favorite_track(id, track_id, user_id, created_at) VALUES (9, 60, 1, 200)"
            )
        self.fav.return_value = False
        before = int(self.state["last_favorite_id"])
        self.poll()
        self.assertEqual(self.state["last_favorite_id"], before)
        self.fav.return_value = True
        self.poll()
        self.assertEqual(self.state["last_favorite_id"], 9)
        self.assertEqual(self.fav.call_count, 2)

    def test_pending_detail_cap_keeps_accurate_stage_counts(self):
        import monitor.music_db_poller as music_mod
        from monitor.music_summary import build_music_summary

        for i in range(1, music_mod._PENDING_DETAIL_CAP + 25):
            self.add_track(1000 + i, f"曲{i}")
        self.poll()
        pending = self.state["music_pending"]
        self.assertEqual(len(pending["tracks"]), music_mod._PENDING_DETAIL_CAP)
        self.assertEqual(
            pending["stage_counts"][MUSIC_TRACK_ADDED],
            music_mod._PENDING_DETAIL_CAP + 24,
        )
        summary = build_music_summary(pending)
        self.assertEqual(
            summary["by_type"][MUSIC_TRACK_ADDED],
            music_mod._PENDING_DETAIL_CAP + 24,
        )


if __name__ == "__main__":
    unittest.main()
