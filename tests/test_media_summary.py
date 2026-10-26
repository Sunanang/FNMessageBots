"""影视静默批次：真实临时 SQLite、模拟时钟，不访问 NAS 或发送推送。"""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import requests
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from main import Application
from monitor.event_processor import EventProcessor
from monitor.media_db_poller import MediaDBPoller, TRIM_RESOURCE_ADDED, TRIM_SCRAPE_SUCCESS
from monitor.media_summary import MEDIA_SUMMARY_EVENT, format_media_summary
from notifier.multi_platform_notifier import MultiPlatformNotifier
from notifier.unified_notifier import UnifiedNotifier, NotificationResult

STAGES = [TRIM_RESOURCE_ADDED, TRIM_SCRAPE_SUCCESS]


class MediaSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.db = self.base / "trimmedia.db"
        with sqlite3.connect(self.db) as writer:
            writer.executescript("""
                CREATE TABLE item(guid TEXT PRIMARY KEY,type TEXT,title TEXT,path TEXT,parent_guid TEXT,
                    season_number INTEGER,episode_number INTEGER,tmdb_id TEXT,fetch_status INTEGER,
                    create_time INTEGER,update_time INTEGER,release_date TEXT,first_air_date TEXT,air_date TEXT,
                    runtime INTEGER,overview TEXT);
                CREATE TABLE item_media(guid TEXT,item_guid TEXT,path TEXT,size INTEGER,create_time INTEGER,update_time INTEGER);
                CREATE TABLE media_delete(update_time INTEGER);
            """)
        self.now = 1000.0
        manager = patch("monitor.media_db_poller.time.time", side_effect=lambda: self.now)
        manager.start()
        self.addCleanup(manager.stop)
        self.poller = MediaDBPoller(str(self.db), str(self.base), monitor_events=STAGES)
        self.delivery = Mock(return_value=True)
        self.poller.add_handler(MEDIA_SUMMARY_EVENT, self.delivery)
        self.state = self.poller._load_state()
        self.poller._poll_once(self.state)

    def item(self, guid, kind="Movie", title="电影", parent=None, season=None, episode=None, fs=0, timestamp=100):
        with sqlite3.connect(self.db) as writer:
            writer.execute("INSERT INTO item(guid,type,title,path,parent_guid,season_number,episode_number,fetch_status,create_time,update_time) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?)", (guid, kind, title, f"/library/{guid}.mkv", parent, season, episode, fs, timestamp, timestamp))

    def media(self, guid, timestamp=100):
        with sqlite3.connect(self.db) as writer:
            writer.execute("INSERT INTO item_media VALUES (?,?,?,1,?,?)", ("file-" + guid, guid, f"/library/{guid}.mkv", timestamp, timestamp))

    def poll(self, advance=0):
        self.now += advance
        self.poller._poll_once(self.state)

    def summary(self):
        return self.delivery.call_args.args[0]

    def test_item_and_file_are_deduplicated_and_scrape_extends_quiet_window(self):
        self.item("film")
        self.media("film")
        self.poll()
        self.assertEqual(len(self.state["media_pending"]["resources"]), 1)
        self.poll(59)
        self.delivery.assert_not_called()
        with sqlite3.connect(self.db) as writer:
            writer.execute("UPDATE item SET fetch_status=1,update_time=101 WHERE guid='film'")
        self.poll()
        self.poll(59)
        self.delivery.assert_not_called()
        self.poll(1)
        self.delivery.assert_called_once()
        data = self.summary()
        self.assertEqual(data["by_type"], {TRIM_RESOURCE_ADDED: 1, TRIM_SCRAPE_SUCCESS: 1})
        self.assertEqual(data["resource_count"], 1)
        self.assertEqual(self.state["media_pending"], {})
        self.poll(120)
        self.delivery.assert_called_once()

    def test_already_scraped_new_resource_is_counted_without_replaying_history(self):
        self.item("ready", fs=1)
        self.poll()
        self.poll(60)
        self.assertEqual(self.summary()["by_type"], dict.fromkeys(STAGES, 1))
        self.delivery.reset_mock()
        self.state["initialized"] = False
        self.poll(60)
        self.delivery.assert_not_called()

    def test_series_and_episodes_are_grouped_without_counting_the_file_twice(self):
        self.item("series", "TV", "测试剧", fs=1)
        self.item("season", "Season", "第一季", parent="series", season=1)
        for episode in range(1, 11):
            self.item(f"ep-{episode}", "Episode", f"单集{episode}", parent="season", season=1, episode=episode, fs=1)
            self.media(f"ep-{episode}")
        self.poll()
        self.poll(60)
        data = self.summary()
        self.assertEqual(data["by_type"][TRIM_RESOURCE_ADDED], 12)
        self.assertEqual(data["by_type"][TRIM_SCRAPE_SUCCESS], 11)
        text = format_media_summary(data)
        self.assertIn("《测试剧》 第1季 第1、2、3", text)
        self.assertIn("不代表整个任务完成", text)

    def test_same_title_in_different_series_or_seasons_is_not_merged(self):
        for series in ["a", "b"]:
            self.item(series, "TV", "相同剧名")
            for season in [0, 1]:
                self.item(f"{series}-{season}", "Episode", "同名单集", parent=series, season=season, episode=1)
        self.poll()
        self.poll(60)
        section = self.summary()["sections"][0]
        self.assertEqual(section["count"], 6)
        self.assertEqual(len(section["preview"]), 4)
        self.assertEqual(section["omitted"], 2)

    def test_restart_recovers_pending_but_does_not_collect_offline_history(self):
        self.item("before")
        self.poll()
        batch_id = self.state["media_pending"]["batch_id"]
        self.item("offline", timestamp=101)
        restarted = MediaDBPoller(str(self.db), str(self.base), monitor_events=STAGES)
        restarted.add_handler(MEDIA_SUMMARY_EVENT, self.delivery)
        restored = restarted._load_state()
        restored["initialized"] = False
        self.now += 60
        restarted._poll_once(restored)
        self.assertEqual(self.summary()["batch_id"], batch_id)
        self.assertEqual(self.summary()["resource_count"], 1)

    def test_delivery_failure_keeps_batch_for_retry_and_does_not_flood(self):
        self.item("retry")
        self.poll()
        batch_id = self.state["media_pending"]["batch_id"]
        self.delivery.return_value = False
        self.poll(60)
        self.assertEqual(self.state["media_pending"]["batch_id"], batch_id)
        self.poll(1)
        self.assertEqual(self.delivery.call_count, 1)
        self.delivery.return_value = True
        self.poll(59)
        self.assertEqual(self.delivery.call_count, 2)
        self.assertEqual(self.state["media_pending"], {})

    def test_unchecked_stage_is_not_delivered_from_an_existing_batch(self):
        self.item("both", fs=1)
        self.poll()
        self.poller.update_config(monitor_events=[TRIM_SCRAPE_SUCCESS])
        self.poll(60)
        self.assertEqual(self.summary()["by_type"], {TRIM_SCRAPE_SUCCESS: 1})

    def test_disabling_all_media_events_does_not_replay_the_old_buffer_when_reenabled(self):
        self.item("cancelled", fs=1)
        self.poll()
        self.poller.update_config(monitor_events=[])
        restarted = MediaDBPoller(str(self.db), str(self.base), monitor_events=STAGES)
        restarted.add_handler(MEDIA_SUMMARY_EVENT, self.delivery)
        state = restarted._load_state()
        state["initialized"] = False
        self.now += 120
        restarted._poll_once(state)
        self.delivery.assert_not_called()
        self.assertEqual(state["media_pending"], {})

    def test_existing_global_summary_mode_still_sends_only_one_media_summary(self):
        enqueue = Mock()
        self.poller.set_poll_batch_summary(True, enqueue)
        self.item("global", fs=1)
        self.poll()
        self.poll(60)
        enqueue.assert_not_called()
        self.delivery.assert_called_once()

    def test_file_batch_over_500_rows_with_identical_timestamp_is_not_truncated(self):
        with sqlite3.connect(self.db) as writer:
            writer.executemany("INSERT INTO item(guid,type,title,create_time,update_time) VALUES (?,'Movie',?,1,1)",
                               [(str(i), f"电影{i}") for i in range(501)])
        self.state["initialized"] = False
        self.poll()
        with sqlite3.connect(self.db) as writer:
            writer.executemany("INSERT INTO item_media VALUES (?,?,?,1,100,100)",
                               [(f"file-{i}", str(i), f"/{i}.mkv") for i in range(501)])
        self.poll()
        self.poll(60)
        self.assertEqual(self.summary()["by_type"], {TRIM_RESOURCE_ADDED: 501})
        self.assertEqual(self.summary()["sections"][0]["omitted"], 497)

    def test_persist_failure_prevents_clearing_or_delivering_the_buffer(self):
        self.item("persist")
        self.poll()
        with patch.object(self.poller, "_save_state", return_value=False):
            self.poll(60)
        self.delivery.assert_not_called()
        self.assertTrue(self.state["media_pending"])
        self.poll()
        self.delivery.assert_called_once()

    def test_switching_database_drops_the_old_source_pending_and_starts_a_new_baseline(self):
        self.item("old-source")
        self.poll()
        other = self.base / "other.db"
        with sqlite3.connect(self.db) as original, sqlite3.connect(other) as target:
            original.backup(target)
        self.poller.update_config(db_path=str(other))
        self.poll(60)
        self.delivery.assert_not_called()
        self.assertEqual(self.state["media_pending"], {})

    def test_missing_handler_keeps_the_buffer_until_delivery_is_available(self):
        self.item("handler")
        self.poll()
        self.poller.clear_handlers()
        self.poll(60)
        self.assertTrue(self.state["media_pending"])
        self.poller.add_handler(MEDIA_SUMMARY_EVENT, self.delivery)
        self.poll(60)
        self.delivery.assert_called_once()

    def test_long_titles_keep_full_counts_and_bounded_notification_body(self):
        for index in range(15):
            self.item(f"long-{index}", title="很长的影视作品名称" * 20, fs=1)
        self.poll()
        self.poll(60)
        self.assertEqual(self.summary()["by_type"], dict.fromkeys(STAGES, 15))
        text = format_media_summary(self.summary())
        self.assertIn("另有 11 组未展开", text)
        self.assertLess(len(text.encode("utf-8")), 1850)

    def test_internal_handler_registration_and_result_propagation(self):
        app = Application()
        app.config = SimpleNamespace(monitor_events=STAGES)
        app.media_db_poller = self.poller
        app.event_processor = SimpleNamespace(get_handler=lambda _: self.delivery)
        app._register_db_event_handlers()
        self.assertIn(MEDIA_SUMMARY_EVENT, self.poller.event_handlers)
        processor = object.__new__(EventProcessor)
        processor.notifier = Mock()
        processor.notifier.send_notification.return_value = NotificationResult(False, "multiple")
        processor._store_notification_log = Mock()
        result = processor._handle_trim_library_db(MEDIA_SUMMARY_EVENT, {}, SimpleNamespace(raw_data="{}", timestamp="now"))
        self.assertFalse(result.success)

    def test_do_not_disturb_counts_the_underlying_stages(self):
        self.item("quiet", fs=1)
        self.poll()
        self.poll(60)
        notifier = object.__new__(UnifiedNotifier)
        notifier.config = SimpleNamespace(monitor_events=STAGES)
        notifier._dnd_light_buffer_lock = threading.Lock()
        notifier._dnd_light_buffer_counts = {}
        notifier.logger = Mock()
        notifier._in_dnd_window = Mock(return_value=True)
        notifier.multi_platform_notifier = Mock()
        result = notifier.send_notification(MEDIA_SUMMARY_EVENT, self.summary(), "{}", "now")
        self.assertTrue(result.success)
        self.assertEqual(notifier._dnd_light_buffer_counts, dict.fromkeys(STAGES, 1))
        notifier.multi_platform_notifier.send_notification.assert_not_called()

    def test_actual_message_has_a_summary_title_even_with_minimal_mode(self):
        self.item("render", fs=1)
        self.poll()
        self.poll(60)
        with patch.object(MultiPlatformNotifier, "_start_merge_timer"):
            formatter = MultiPlatformNotifier(title_prefix="测试NAS", minimal_push_enabled=True)
        self.addCleanup(formatter.close)
        message = formatter._build_message(MEDIA_SUMMARY_EVENT, self.summary(), "now", "{}")
        self.assertIn("测试NAS-影视处理汇总", message.title)
        self.assertIn("入库：1", message.content)
        self.assertIn("刮削完成：1", message.content)

    def test_application_to_http_pipeline_delivers_one_summary_after_sixty_seconds(self):
        config = SimpleNamespace(monitor_events=STAGES, cursor_dir=str(self.base),
                                 wechat_webhook_url="https://example.invalid/hook", dingtalk_webhook_url="",
                                 feishu_webhook_url="", bark_url="", pushplus_params="", dedup_window=300,
                                 http_pool_size=2, http_retry_count=0, http_timeout=1,
                                 poll_batch_summary_enabled=True, minimal_push_enabled=True)
        with patch.object(MultiPlatformNotifier, "_start_merge_timer"), \
             patch("monitor.event_processor.LogStorage"), patch("utils.push_stats.record"), \
             patch("utils.push_history.add_record") as history:
            notifier = UnifiedNotifier(config)
            self.addCleanup(notifier.close)
            processor = EventProcessor(notifier, config)
            app = Application()
            app.config = config
            app.event_processor = processor
            app.media_db_poller = self.poller
            app._register_db_event_handlers()
            app.media_db_poller.set_poll_batch_summary(True, app._enqueue_unified_batch_items)
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"errcode":0}'
            with patch.object(notifier.multi_platform_notifier.connection_pool.session, "request", return_value=response) as request:
                self.item("integrated", fs=1)
                self.media("integrated")
                self.poll()
                self.poll(59)
                request.assert_not_called()
                self.poll(1)
                request.assert_called_once()
                payload = request.call_args.kwargs["json"]
                self.assertIn("影视处理汇总", json.dumps(payload, ensure_ascii=False))
                self.assertIn("入库：1", json.dumps(payload, ensure_ascii=False))
                self.assertIn("刮削完成：1", json.dumps(payload, ensure_ascii=False))
            self.assertEqual(app._unified_batch_buf, [])
            self.assertEqual(history.call_args.kwargs["event_type"], MEDIA_SUMMARY_EVENT)
            self.assertEqual(self.state["media_pending"], {})
