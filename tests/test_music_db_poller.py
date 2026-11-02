"""音乐库轮询：临时 SQLite、模拟时钟；不访问 NAS、不发送推送。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def test_restart_preserves_observed_pending_but_skips_offline_additions(self):
        self.add_track(70, "重启前已观察")
        self.poll()
        self.poller._save_dedup()
        self.add_track(71, "停机期间新增")
        restarted = MusicDBPoller(str(self.db), str(self.base), monitor_events=ALL_EVENTS)
        restarted.add_handler(MUSIC_SUMMARY_EVENT, self.summary)
        self.now += 60

        def run_one_cycle(_seconds):
            restarted.running = False

        restarted.running = True
        with patch("monitor.music_db_poller.time.sleep", side_effect=run_one_cycle):
            restarted._run_loop()
        self.summary.assert_called_once()
        data = self.summary.call_args.args[0]
        self.assertEqual(data["by_type"], {MUSIC_TRACK_ADDED: 1})
        self.assertIn("重启前已观察", format_music_summary(data))
        self.assertNotIn("停机期间新增", format_music_summary(data))

    def test_remove_only_tracks_new_songs_for_later_removal(self):
        self.add_track(1, "已有曲目")
        self.state["initialized"] = False
        self.poller.update_config(monitor_events=[MUSIC_TRACK_REMOVED])
        self.poll()
        self.add_track(80, "后来入库")
        self.poll()
        self.soft_delete(80)
        self.poll()
        self.poll(60)
        self.summary.assert_called_once()
        self.assertEqual(self.summary.call_args.args[0]["by_type"], {MUSIC_TRACK_REMOVED: 1})

    def test_real_handler_failure_retains_summary_and_favorite_cursor(self):
        from monitor.event_processor import EventProcessor
        from notifier.unified_notifier import NotificationResult

        notifier = Mock()
        notifier.send_notification.return_value = NotificationResult(False, "mock", {})
        with patch("monitor.event_processor.LogStorage"):
            processor = EventProcessor(notifier, SimpleNamespace(log_retention_days=7, log_dir=str(self.base)))
        for event in (MUSIC_SUMMARY_EVENT, MUSIC_FAVORITED):
            self.poller.add_handler(event, processor.get_handler(event))
        self.add_track(90, "失败后重试")
        with sqlite3.connect(self.db) as writer:
            writer.execute("INSERT INTO favorite_track VALUES (1, 90, 1, 200)")
        self.poll()
        self.poll(60)
        self.assertEqual(self.state["last_favorite_id"], 0)
        self.assertTrue(self.state["music_pending"])
        notifier.send_notification.return_value = NotificationResult(True, "mock", {})
        self.poll(60)
        self.assertEqual(self.state["last_favorite_id"], 1)
        self.assertFalse(self.state["music_pending"])

    def test_concurrent_insert_does_not_advance_past_unobserved_track(self):
        with sqlite3.connect(self.db) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
        self.add_track(100, "先入库")
        original = self.poller._accumulate_library

        def accumulate(*args):
            original(*args)
            self.add_track(101, "查询过程中入库")

        with patch.object(self.poller, "_accumulate_library", side_effect=accumulate):
            self.poll()
        self.poll()
        self.poll(60)
        self.assertEqual(self.summary.call_args.args[0]["by_type"], {MUSIC_TRACK_ADDED: 2})

    def test_legacy_pending_ids_and_capped_details_still_cancel_net_zero(self):
        from monitor.music_db_poller import _PENDING_DETAIL_CAP
        for tid in range(1, _PENDING_DETAIL_CAP + 3):
            self.add_track(tid)
        self.poll()
        # 模拟旧版本已保存的列表格式，包含未保留预览的曲目。
        self.state["music_pending"]["added_ids"] = list(range(1, _PENDING_DETAIL_CAP + 3))
        self.soft_delete(_PENDING_DETAIL_CAP + 2)
        self.poll()
        self.poll(60)
        self.assertEqual(self.summary.call_args.args[0]["by_type"], {MUSIC_TRACK_ADDED: _PENDING_DETAIL_CAP + 1})

    def test_failed_summary_retries_only_after_sixty_seconds(self):
        self.add_track(120)
        self.poll()
        self.summary.return_value = False
        self.poll(60)
        self.poll(59)
        self.summary.assert_called_once()
        self.assertTrue(self.state["music_pending"])
        self.summary.return_value = True
        self.poll(1)
        self.assertEqual(self.summary.call_count, 2)
        self.assertFalse(self.state["music_pending"])

    def test_failed_checkpoint_prevents_summary_delivery(self):
        self.add_track(130)
        self.poll()
        with patch.object(self.poller, "_write_state", return_value=False):
            self.poll(60)
        self.summary.assert_not_called()
        self.assertTrue(self.state["music_pending"])
        self.poll()
        self.summary.assert_called_once()

    def test_deselected_pending_stages_are_not_replayed_after_restart(self):
        self.add_track(140)
        self.poll()
        self.poller.update_config(monitor_events=[MUSIC_FAVORITED])
        restored = self.poller._load_state()
        restored["initialized"] = False
        self.poller.update_config(monitor_events=ALL_EVENTS)
        self.now += 60
        self.poller._poll_once(restored)
        self.summary.assert_not_called()

    def test_summary_delivery_releases_read_transaction(self):
        self.add_track(150)
        self.poll()
        # 非 WAL 库：网络回调期间音乐服务仍能写入，不能持有整轮读取事务。
        self.summary.side_effect = lambda *_: self.add_track(151)
        self.poll(60)
        self.summary.assert_called_once()
        self.assertFalse(self.state["music_pending"])

    def test_dnd_keeps_original_music_stage_counts(self):
        from notifier.unified_notifier import UnifiedNotifier
        notifier = UnifiedNotifier.__new__(UnifiedNotifier)
        notifier.config = SimpleNamespace(monitor_events=LIBRARY_EVENTS)
        notifier.multi_platform_notifier = Mock()
        notifier.logger = Mock()
        import threading
        notifier._dnd_light_buffer_lock = threading.Lock()
        notifier._dnd_light_buffer_counts = {}
        with patch.object(notifier, "_in_dnd_window", return_value=True):
            result = notifier.send_notification(MUSIC_SUMMARY_EVENT, {
                "by_type": {MUSIC_TRACK_ADDED: 100, MUSIC_TRACK_REMOVED: 2},
            }, "{}", "2026-11-01 23:00:00")
        self.assertTrue(result.success)
        self.assertEqual(notifier._dnd_light_buffer_counts, {MUSIC_TRACK_ADDED: 100, MUSIC_TRACK_REMOVED: 2})
        notifier.multi_platform_notifier.send_notification.assert_not_called()

    def test_application_http_failure_then_success_retains_and_delivers_music_summary(self):
        import json
        import requests
        from main import Application
        from monitor.event_processor import EventProcessor
        from notifier.multi_platform_notifier import MultiPlatformNotifier
        from notifier.unified_notifier import UnifiedNotifier

        config = SimpleNamespace(
            monitor_events=LIBRARY_EVENTS, cursor_dir=str(self.base),
            wechat_webhook_url="https://example.invalid/hook", dingtalk_webhook_url="",
            feishu_webhook_url="", bark_url="", pushplus_params="", dedup_window=300,
            http_pool_size=2, http_retry_count=0, http_timeout=1,
            poll_batch_summary_enabled=True, minimal_push_enabled=True,
        )
        with patch.object(MultiPlatformNotifier, "_start_merge_timer"), patch(
            "monitor.event_processor.LogStorage"
        ), patch("utils.push_stats.record"), patch("utils.push_history.add_record"):
            notifier = UnifiedNotifier(config)
            self.addCleanup(notifier.close)
            processor = EventProcessor(notifier, config)
            app = Application()
            app.config, app.event_processor, app.music_db_poller = config, processor, self.poller
            app._register_db_event_handlers()
            self.poller.set_poll_batch_summary(True, app._enqueue_unified_batch_items)
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"errcode":40001,"errmsg":"invalid credential"}'
            with patch.object(notifier.multi_platform_notifier.connection_pool.session, "request", return_value=response) as request:
                self.add_track(160, "真实链路回归")
                self.poll()
                self.poll(60)
                request.assert_called_once()
                self.assertTrue(self.state["music_pending"])
                self.poll(59)
                request.assert_called_once()
                response._content = b'{"errcode":0}'
                self.poll(1)
                self.assertEqual(request.call_count, 2)
                payload = json.dumps(request.call_args.kwargs["json"], ensure_ascii=False)
                self.assertIn("音乐库本轮汇总", payload)
                self.assertIn("真实链路回归", payload)
                self.assertFalse(self.state["music_pending"])
                self.assertEqual(app._unified_batch_buf, [])

    def test_long_unicode_previews_leave_room_for_channel_title_and_footer(self):
        from monitor.music_summary import build_music_summary
        pending = self.poller._empty_pending()
        for stage in LIBRARY_EVENTS:
            pending["stage_counts"][stage] = 10
            for i in range(10):
                pending["tracks"][f"{stage}:{i}"] = {
                    "track_id": i, "title": "音乐🎵" * 40, "artist": "艺人" * 40,
                    "album": "专辑" * 40, "stages": [stage],
                }
        data = build_music_summary(pending)
        body = format_music_summary(data)
        self.assertLess(len(body.encode("utf-8")), 1850)
        self.assertEqual(data["by_type"], {stage: 10 for stage in LIBRARY_EVENTS})
        self.assertTrue(all(section["omitted"] == 6 for section in data["sections"]))
        self.assertIn("不代表整个扫描任务完成", body)


if __name__ == "__main__":
    unittest.main()
