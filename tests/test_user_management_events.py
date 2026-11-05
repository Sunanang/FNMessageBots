"""系统用户管理：用户提供的四条日志经真实临时 SQLite 走通知链路，不操作 NAS。"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from config import Config
from monitor.db_log_poller import DBLogPoller
from monitor.event_processor import EventProcessor
from notifier.multi_platform_notifier import MultiPlatformNotifier
from notifier.unified_notifier import UnifiedNotifier
from valid_event_ids import filter_monitor_events
from web import ui_app
from web.event_catalog import DEFAULT_SELECTED_EVENTS


EVENTS = ['AddUser', 'DelUser']
ROWS = [
    (64264, 'AddUser', 1004, 1793774563),
    (64265, 'DelUser', 1004, 1793774569),
    (64266, 'AddUser', 1005, 1793774598),
    (64267, 'DelUser', 1005, 1793774615),
]


class UserManagementEventTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.db = self.base / 'logger.db'
        self.config_file = self.base / 'config.json'
        self.config_file.write_text(json.dumps({
            'web_password_enabled': False, 'monitor_events': ['LoginSucc'],
            'ssh_open_timeout_by_event': True,
            'cursor_dir': str(self.base / 'cursor'), 'log_dir': str(self.base / 'logs'),
        }))
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE log (id INTEGER PRIMARY KEY, serviceId TEXT, uid INTEGER, '
                         'uname TEXT, logtime INTEGER, loglevel INTEGER, eventId TEXT, '
                         'parameter TEXT, category INTEGER)')
            conn.executemany('INSERT INTO log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', [
                (row_id, 'user', 1000, 'Lando', timestamp, 0, event_type,
                 json.dumps({'cat': 3, 'template': event_type, 'target_uid': target_uid,
                             'target_user': 'test', 'uid': 1000}), 1)
                for row_id, event_type, target_uid, timestamp in ROWS
            ])
        for manager in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(MultiPlatformNotifier, '_start_merge_timer'),
            patch('monitor.event_processor.LogStorage'),
            patch('utils.push_stats.record'),
            patch('utils.push_history.add_record'),
            patch.object(ui_app, 'CONFIG_FILE', self.config_file),
            patch.object(ui_app, '_collect_external_db_access_warnings', return_value=[]),
        ):
            manager.start()
            self.addCleanup(manager.stop)
        self.config = SimpleNamespace(
            cursor_dir=str(self.base / 'cursor'), log_dir=str(self.base / 'logs'),
            logger_db_path=str(self.db), monitor_events=EVENTS,
            wechat_webhook_url='https://example.invalid/hook', dingtalk_webhook_url='',
            feishu_webhook_url='', bark_url='', pushplus_params='',
            dedup_window=300, http_pool_size=2, http_retry_count=0, http_timeout=1,
            title_prefix='飞牛NAS', dnd_enabled=False,
        )
        self.notifier = UnifiedNotifier(self.config)
        self.addCleanup(self.notifier.close)
        self.multi = self.notifier.multi_platform_notifier
        self.deliver = Mock(return_value=(True, {'success': True}))
        self.multi._send_to_wechat = self.deliver
        self.processor = EventProcessor(self.notifier, self.config)
        self.poller = DBLogPoller(str(self.db), str(self.base / 'cursor'), monitor_events=EVENTS)
        for event_type in EVENTS:
            self.poller.add_handler(event_type, self.processor.get_handler(event_type))

    def messages(self):
        return [call.args[0] for call in self.deliver.call_args_list]

    def test_four_real_rows_are_distinct_and_operator_is_not_target(self):
        self.assertEqual(self.poller._poll_once(64263), 64267)
        self.assertEqual(len(self.messages()), 4)
        for message, (_, event_type, uid, _) in zip(self.messages(), ROWS):
            with self.subTest(event_type=event_type, uid=uid):
                self.assertIn('创建用户' if event_type == 'AddUser' else '删除用户', message.title)
                self.assertIn('操作用户: Lando\n操作用户 UID: 1000', message.content)
                self.assertIn(f'目标用户: test\n目标用户 UID: {uid}', message.content)
                self.assertNotIn('影视', message.title)
                self.assertNotIn('目标用户: Lando', message.content)
        # 同一源记录重试不重复发送；同一分钟的不同记录不被合并。
        self.poller._poll_once(64263)
        self.assertEqual(self.deliver.call_count, 4)
        self.assertEqual(self.processor.log_storage.store_log.call_count, 8)

    def test_each_event_can_be_selected_independently_and_hot_reloaded(self):
        self.poller.update_config(monitor_events=['AddUser'])
        self.poller._poll_once(64263)
        self.assertEqual(self.deliver.call_count, 2)
        self.assertTrue(all('创建用户' in msg.title for msg in self.messages()))
        self.deliver.reset_mock()
        self.poller.update_config(monitor_events=['DelUser'])
        self.poller._poll_once(64263)
        self.assertEqual(self.deliver.call_count, 2)
        self.assertTrue(all('删除用户' in msg.title for msg in self.messages()))

    def test_media_user_selection_does_not_enable_system_user_events(self):
        self.poller.update_config(monitor_events=['MEDIA_USER_CREATED'])
        self.poller._poll_once(64263)
        self.deliver.assert_not_called()

    def test_minimal_notifications_keep_both_accounts_and_uid(self):
        self.multi.minimal_push_enabled = True
        self.poller._poll_once(64263)
        self.assertEqual(self.deliver.call_count, 4)
        for message in self.messages():
            self.assertIn('操作用户: Lando', message.title)
            self.assertIn('目标用户: test', message.title)
            self.assertIn('目标用户 UID:', message.title)
            self.assertEqual(message.content, '')
            self.assertNotIn('\n', message.title)

    def test_poll_summary_keeps_both_same_name_account_lifecycles(self):
        self.poller.set_batch_handler(self.processor.process_batch_events)
        self.poller._poll_once(64263)
        self.deliver.assert_called_once()
        message = self.messages()[0]
        self.assertIn('创建用户', message.content)
        self.assertIn('删除用户', message.content)
        self.assertEqual(message.content.count('目标用户 UID: 1004'), 2)
        self.assertEqual(message.content.count('目标用户 UID: 1005'), 2)
        self.assertEqual(message.content.count('操作用户: Lando'), 4)

    def test_dnd_suppresses_individual_messages_and_counts_real_rows_once(self):
        self.config.dnd_enabled = True
        with patch.object(self.notifier, '_in_dnd_window', return_value=True):
            self.poller._poll_once(64263)
        self.deliver.assert_not_called()
        start = datetime.fromtimestamp(ROWS[0][3] - 1)
        end = datetime.fromtimestamp(ROWS[-1][3] + 1)
        counts = self.notifier._query_dnd_events_summary(start, end)
        self.assertEqual(counts, {'AddUser': 2, 'DelUser': 2})
        self.assertEqual(self.notifier._dnd_light_buffer_counts, {})
        summary = self.notifier._build_dnd_summary_from_db(start, end, counts)
        self.assertIn('创建用户 2 次', summary)
        self.assertIn('删除用户 2 次', summary)
        self.config.monitor_events = ['DelUser']
        self.assertEqual(self.notifier._query_dnd_events_summary(start, end), {'DelUser': 2})

    def test_missing_fields_do_not_confuse_operator_and_target_and_uid_zero_is_kept(self):
        with patch.object(self.multi, '_lookup_username_from_db') as media_lookup:
            message = self.multi._build_message('DelUser', {'user': 'Lando', 'target_uid': 0}, 'now', '{}')
        media_lookup.assert_not_called()
        self.assertIn('操作用户: Lando', message.content)
        self.assertIn('目标用户 UID: 0', message.content)
        self.assertNotIn('目标用户: Lando', message.content)
        self.assertNotIn('None', message.content)
        self.assertIn('删除用户', self.multi._build_message('DelUser', {}, 'now', '{}').title)

    def test_catalog_and_config_round_trip_preserve_selection_without_auto_enabling(self):
        self.assertEqual(filter_monitor_events(EVENTS), EVENTS)
        self.assertFalse(set(EVENTS) & set(DEFAULT_SELECTED_EVENTS))
        client = ui_app.create_app().test_client()
        before = self.config_file.read_bytes()
        data = client.get('/api/config').get_json()['data']
        category = next(c for c in data['events_by_category'] if c['id'] == 'user_manage')
        self.assertEqual(category['name'], '用户管理')
        self.assertEqual([e['id'] for e in category['events']], EVENTS)
        self.assertFalse(set(EVENTS) & set(data['selected_events']))
        self.assertEqual(self.config_file.read_bytes(), before)
        for selected in (EVENTS, ['AddUser'], ['DelUser']):
            with self.subTest(selected=selected):
                response = client.post('/api/save-config', json={
                    'events': selected,
                    'channels': [{'type': 'wechat', 'enabled': True, 'url': 'https://example.invalid/hook'}],
                })
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(json.loads(self.config_file.read_text())['monitor_events'], selected)
                self.assertEqual(client.get('/api/config').get_json()['data']['selected_events'], selected)
                with patch.object(Config, '_get_config_file_path', return_value=self.config_file):
                    loaded = Config()
                self.assertTrue(loaded.reload_from_file(self.config_file))
                self.assertEqual(loaded.monitor_events, selected)
        self.assertEqual(data['version'], '2.8.0')
        self.assertIn('version = "2.8.0"', (ROOT / 'pyproject.toml').read_text())
        self.assertIn('version               = 2.8.0', (ROOT / 'fpk/manifest').read_text())
