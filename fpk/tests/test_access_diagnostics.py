"""配置权限诊断回归；不执行真实授权、不联网、不发送推送。"""
import json
import os
import shlex
import stat
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

root = Path(__file__).resolve().parents[1]
source = root / "src"
if not source.is_dir():
    source = root / "cmd/fnmessagebots/src"
sys.path.insert(0, str(source))

from monitor import docker_socket_access as docker_access
from monitor import nas_patrol as patrol
from monitor.ssh_journal_poller import SshJournalPoller
from utils import access_guidance as guidance
from web import access_diagnostics as diagnostics


class AccessDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"FNMB_GATEWAY_SOCKET": "/mock/app.sock"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        for module in (diagnostics, guidance, docker_access):
            container = patch.object(module, "in_container", return_value=False)
            container.start()
            self.addCleanup(container.stop)

    def database(self, sql="CREATE TABLE log(id INTEGER)", name="events.db"):
        path = self.base / name
        with sqlite3.connect(path) as conn:
            conn.executescript(sql)
        return str(path)

    def result(self, code=0, stdout="", stderr=""):
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)

    def test_readable_database_needs_no_authorization(self):
        path = self.database()
        self.assertIsNone(diagnostics.database_access_issue("主日志库", path, "SELECT id FROM log LIMIT 1"))

    def test_parents_only_require_traversal(self):
        path = self.database()
        def access(filename, mode):
            return mode != os.R_OK or str(filename) == path
        with patch.object(diagnostics.os, "access", side_effect=access):
            self.assertIsNone(diagnostics.database_access_issue("主日志库", path, "SELECT id FROM log LIMIT 1"))

    def test_missing_file_does_not_suggest_permission_change(self):
        result = diagnostics.database_access_issue("相册库", str(self.base / "missing/photo.db"))
        self.assertEqual(result["code"], "missing")
        self.assertEqual(result["commands"], "")
        self.assertNotIn("compose", result["action"].lower())

    def test_inaccessible_database_generates_targeted_acl(self):
        path = self.database(name="file with spaces.db")
        with patch.object(diagnostics.os, "access", side_effect=lambda p, mode: str(p) != path):
            result = diagnostics.database_access_issue("任务计划库", path)
        self.assertEqual(result["code"], "permission")
        self.assertIn("FnMessageBot", result["commands"])
        self.assertIn("realpath -e", result["commands"])
        self.assertNotIn("chmod", result["commands"])
        self.assertNotIn("chown", result["commands"])

    def test_inaccessible_parent_generates_acl_for_whole_path(self):
        path = self.database()
        with patch.object(diagnostics.os, "access", side_effect=lambda p, mode: Path(p) != self.base):
            result = diagnostics.database_access_issue("备份库", path)
        self.assertEqual(result["code"], "permission")
        self.assertIn(path, result["commands"])

    def test_container_does_not_authorize_fpk_account(self):
        path = self.database()
        with patch.object(diagnostics, "in_container", return_value=True), patch.object(diagnostics.os, "access", return_value=False):
            result = diagnostics.database_access_issue("备份库", path)
        self.assertIn("UID/GID", result["action"])
        self.assertEqual(result["commands"], "")

    def test_unsupported_lite_schema_cannot_be_fixed_by_acl(self):
        path = self.database("CREATE TABLE unrelated(id INTEGER)", "litegallery.sqlite")
        result = diagnostics.database_access_issue("相册库", path, "SELECT id FROM share_link LIMIT 1")
        self.assertEqual(result["code"], "schema")
        self.assertIn("适配", result["action"])
        self.assertEqual(result["commands"], "")

    def test_malformed_database_does_not_suggest_deleting_it(self):
        path = self.base / "bad.db"
        path.write_text("not sqlite")
        result = diagnostics.database_access_issue("主日志库", str(path))
        self.assertEqual(result["code"], "database")
        self.assertEqual(result["commands"], "")
        self.assertIn("不要直接删除", result["action"])

    def test_locked_database_is_not_a_permission_problem(self):
        path = self.database()
        result = diagnostics.database_access_issue("主日志库", path, sqlite_error=sqlite3.OperationalError("database is locked"))
        self.assertEqual(result["code"], "busy")
        self.assertEqual(result["commands"], "")

    def test_unreadable_sidecar_has_same_database_acl_instructions(self):
        path = self.database()
        Path(path + "-shm").write_text("mock shm")
        with patch.object(diagnostics.os, "access", side_effect=lambda p, mode: str(p) != path + "-shm"):
            result = diagnostics.database_access_issue("主日志库", path)
        self.assertEqual(result["code"], "permission")
        self.assertIn("-shm", result["commands"])

    def test_generated_command_keeps_untrusted_path_as_one_argument(self):
        path = str(self.base / "db ' $(touch unexpected); name.db")
        command = guidance.db_acl_commands(path)
        first_line = shlex.split(command.splitlines()[0])
        self.assertEqual(first_line[4], path)
        syntax = subprocess.run(["bash", "-n"], input=command, text=True, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        # fake sudo 仅打印传入参数，不执行 heredoc 中的授权脚本。
        fake_sudo = self.base / "sudo"
        fake_sudo.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
        fake_sudo.chmod(0o755)
        result = subprocess.run(["bash", "-c", command], env={"PATH": str(self.base) + ":/usr/bin:/bin"},
                                text=True, capture_output=True, cwd=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(path, result.stdout)
        self.assertFalse((self.base / "unexpected").exists())

    def test_only_selected_sources_are_checked(self):
        path = self.database("CREATE TABLE operations(id INTEGER)")
        with patch.object(diagnostics, "ssh_access_issues", side_effect=AssertionError("unselected SSH")), \
             patch.object(diagnostics, "patrol_access_issues", side_effect=AssertionError("unselected patrol")), \
             patch.object(diagnostics, "docker_access_issue", side_effect=AssertionError("unselected Docker")):
            self.assertEqual(diagnostics.collect_access_warnings({"backup_db_path": path}, ["BACKUP_TASK_SUCCESS"]), [])

    def test_main_log_checked_for_system_events(self):
        result = diagnostics.collect_access_warnings({}, ["LoginSucc"])
        self.assertEqual(result[0]["title"], "主日志库")
        self.assertEqual(result[0]["code"], "missing")

    def test_ssh_exit_zero_permission_hint_is_not_success(self):
        result = self.result(stderr="Hint: You are currently not seeing messages from other users and the system.")
        with patch.object(diagnostics, "_run_readonly", return_value=result):
            warning = diagnostics.ssh_access_issues()[0]
        self.assertEqual(warning["code"], "permission")
        self.assertIn("usermod -aG systemd-journal FnMessageBot", warning["commands"])

    def test_runtime_ssh_poller_also_rejects_partial_journal_access(self):
        poller = SshJournalPoller(cursor_dir=str(self.base))
        with patch("monitor.ssh_journal_poller.journalctl_available", return_value=True), \
             patch.object(poller, "_run_journalctl", return_value=(0, "", "Hint: not seeing messages from other users")):
            self.assertFalse(poller._probe())

    def test_readable_journal_does_not_check_or_warn_about_ssh_service_state(self):
        with patch.object(diagnostics, "_run_readonly", return_value=self.result()) as run:
            self.assertEqual(diagnostics.ssh_access_issues(), [])
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("systemctl", " ".join(run.call_args.args[0]))

    def test_active_ssh_without_history_is_not_reported_as_permission_failure(self):
        with patch.object(diagnostics, "_run_readonly", return_value=self.result()):
            self.assertEqual(diagnostics.ssh_access_issues(), [])

    def test_journal_without_files_is_a_missing_source(self):
        with patch.object(diagnostics, "_run_readonly", return_value=self.result(stderr="No journal files were found.")):
            warning = diagnostics.ssh_access_issues()[0]
        self.assertEqual(warning["code"], "source")
        self.assertEqual(warning["commands"], "")

    def test_fpk_missing_docker_socket_does_not_request_compose_mount(self):
        text = docker_access.check_docker_socket_access(str(self.base / "absent.sock"))
        self.assertIn("服务已启动", text)
        self.assertNotIn("Compose", text)

    def test_docker_group_is_based_on_actual_socket_not_fixed_gid(self):
        path = str(self.base / "custom.sock")
        with patch.object(guidance.os, "stat", return_value=SimpleNamespace(st_gid=994)), \
             patch("grp.getgrgid", return_value=SimpleNamespace(gr_name="docker")):
            self.assertEqual(guidance.docker_permission_commands(path), "sudo usermod -aG docker FnMessageBot")
        with patch.object(guidance.os, "stat", return_value=SimpleNamespace(st_gid=0)), \
             patch("grp.getgrgid", return_value=SimpleNamespace(gr_name="root")):
            command = guidance.docker_permission_commands(path)
        self.assertNotIn("usermod", command)
        self.assertIn(path, command)
        self.assertIn("setfacl", command)

    def test_accessible_socket_with_api_failure_does_not_request_elevation(self):
        path = str(self.base / "docker.sock")
        client = Mock()
        client.ping.side_effect = RuntimeError("daemon unavailable")
        with patch.object(docker_access, "docker_sdk", SimpleNamespace(DockerClient=Mock(return_value=client))), \
             patch.object(docker_access.os, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660)), \
             patch.object(docker_access.os, "access", return_value=True):
            result = diagnostics.docker_access_issue(path)
        self.assertEqual(result["code"], "unavailable")
        self.assertEqual(result["commands"], "")
        client.close.assert_called_once()

    def smart_check(self, response, disk="sdb", hwmon="32", disks=None):
        with patch.object(patrol, "_read_system_version", return_value="Debian"), \
             patch.object(patrol, "_read_fnos_version", return_value="FnOS"), \
             patch.object(patrol, "_patrol_list_visible_whole_disks", return_value=disks or [disk]), \
             patch.object(patrol, "_hwmon_temp1_for_block", return_value=hwmon), \
             patch.object(patrol, "_resolve_cmd", side_effect=lambda name: {"smartctl": "/usr/sbin/smartctl", "setcap": "/usr/sbin/setcap", "setfacl": "/usr/bin/setfacl"}[name]), \
             patch.object(diagnostics, "_run_readonly", **(
                 {"side_effect": response} if isinstance(response, list) else {"return_value": response})), \
             patch.object(diagnostics.Path, "exists", return_value=True), \
             patch.object(diagnostics.os, "access", return_value=True):
            return diagnostics.patrol_access_issues()

    def test_smart_nonzero_health_alarm_is_valid_data(self):
        data = {"smart_status": {"passed": False}, "temperature": {"current": 32}}
        self.assertEqual(self.smart_check(self.result(8, json.dumps(data))), [])

    def test_sata_permission_instructions_include_device_and_capability(self):
        result = self.smart_check(self.result(2, stderr="Operation not permitted"))[0]
        self.assertEqual(result["code"], "permission")
        self.assertIn("cap_sys_rawio+ep", result["commands"])
        self.assertNotIn("cap_sys_admin", result["commands"])
        self.assertIn("/dev/sdb", result["commands"])
        self.assertNotIn("sudo python", result["commands"])

    def test_smart_execution_permission_denied_includes_authorization_commands(self):
        warning = self.smart_check([PermissionError("Operation not permitted")], hwmon="")[0]
        self.assertEqual(warning["code"], "permission")
        self.assertIn("sudo /usr/sbin/setcap cap_sys_rawio+ep /usr/sbin/smartctl", warning["commands"])
        self.assertIn("sudo /usr/bin/setfacl -m u:FnMessageBot:r -- /dev/sdb", warning["commands"])
        self.assertIn("温度也会显示 --", warning["message"])

    def test_historical_smart_threshold_exit_32_does_not_request_authorization(self):
        text = (
            "SMART overall-health self-assessment test result: PASSED\n"
            "190 Airflow_Temperature_Cel 0x0022 067 042 045 Old_age Always In_the_past 33 (2 177 33 26 0)\n"
            "194 Temperature_Celsius 0x0022 033 058 000 Old_age Always - 33 (0 20 0 0 0)\n"
        )
        self.assertEqual(self.smart_check(self.result(32, text), hwmon=""), [])

    def test_nvme_permission_instructions_include_admin_capability(self):
        result = self.smart_check(self.result(2, stderr="Permission denied"), disk="nvme0n1")[0]
        self.assertIn("cap_sys_admin", result["commands"])
        self.assertIn("/dev/nvme0n1", result["commands"])

    def test_sata_authorization_preserves_capability_needed_by_other_nvme_disks(self):
        result = diagnostics._smart_permission_issue("/dev/sdb", "/usr/sbin/smartctl", needs_nvme=True)
        self.assertIn("cap_sys_rawio,cap_sys_admin+ep", result["commands"])

    def test_smart_authorization_survives_collapsed_newlines_and_reports_probe_status(self):
        commands = diagnostics._smart_permission_issue(
            "/dev/sdb", "/usr/sbin/smartctl", devices=["/dev/sda", "/dev/sdb"])["commands"]
        # 仅模拟 sudo，不执行任何真实授权；覆盖聊天/终端粘贴丢失换行的情况。
        sudo_stub = '''sudo() {
case "$1" in
*/setcap|setcap) return 0 ;;
*/setfacl|setfacl) return 0 ;;
-u) echo "SMART模拟健康告警"; return 8 ;;
*) return 99 ;;
esac
}
'''
        for script in (commands, commands.replace("\n", " ")):
            with self.subTest(collapsed="\n" not in script):
                result = subprocess.run(["/bin/sh", "-c", sudo_stub + script],
                                        capture_output=True, text=True, timeout=2)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("检查 /dev/sda", result.stdout)
                self.assertIn("检查 /dev/sdb", result.stdout)
                self.assertEqual(result.stdout.count("退出码: 8"), 2)

    def test_failed_smart_authorization_stops_before_probe(self):
        commands = diagnostics._smart_permission_issue("/dev/sdb", "/usr/sbin/smartctl")["commands"]
        for failed_step in ("setcap", "setfacl"):
            for script in (commands, commands.replace("\n", " ")):
                with self.subTest(step=failed_step, collapsed="\n" not in script):
                    sudo_stub = ('sudo() { echo "调用 $1"; '
                                 f'if [ "${{1##*/}}" = {failed_step} ]; then return 6; fi; return 0; }}\n')
                    result = subprocess.run(["/bin/sh", "-c", sudo_stub + script],
                                            capture_output=True, text=True, timeout=2)
                    self.assertEqual(result.returncode, 6, result.stderr)
                    self.assertNotIn("检查 /dev/sdb", result.stdout)
                    self.assertNotIn("调用 -u", result.stdout)
                    if failed_step == "setcap":
                        self.assertNotIn("调用 /usr/bin/setfacl", result.stdout)

    def test_mixed_disks_permission_denials_are_one_actionable_warning(self):
        disks = ["nvme0n1", "sda", "sdb"]
        data = {"smartctl": {"messages": [{"string": "Smartctl open device failed: Permission denied"}]}}
        result = self.smart_check(self.result(2, json.dumps(data)), disks=disks)
        self.assertEqual(len(result), 1)
        warning = result[0]
        self.assertEqual(warning["code"], "permission")
        self.assertIn("健康状态将显示 --", warning["message"])
        self.assertIn("无需重启", warning["action"])
        self.assertEqual(warning["faq"], "faq-patrol")
        commands = warning["commands"]
        self.assertEqual(commands.count("sudo /usr/sbin/setcap"), 1)
        self.assertIn("cap_sys_rawio,cap_sys_admin+ep", commands)
        self.assertIn("sudo /usr/bin/setfacl -m u:FnMessageBot:r -- /dev/nvme0n1 /dev/sda /dev/sdb", commands)
        self.assertIn('sudo -u FnMessageBot /usr/sbin/smartctl -H -A "$disk"', commands)
        self.assertIn('echo "退出码: $?"', commands)
        self.assertNotIn("|", commands)

    def test_smart_acl_command_only_includes_denied_devices(self):
        responses = [self.result(0, json.dumps({"smart_status": {"passed": True}})),
                     self.result(2, stderr="Permission denied")]
        result = self.smart_check(responses, disks=["nvme0n1", "sdb"])[0]
        self.assertIn("sudo /usr/bin/setfacl -m u:FnMessageBot:r -- /dev/sdb &&\n", result["commands"])
        self.assertNotIn("/dev/nvme0n1", result["commands"])
        self.assertIn("cap_sys_admin", result["commands"])

    def test_arm_without_disk_hwmon_gets_one_temperature_permission_hint(self):
        disks = ["sda", "sdb", "sdc", "sdd", "sde"]
        result = self.smart_check(self.result(2, stderr="Permission denied"), disks=disks, hwmon="")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["code"], "permission")
        self.assertIn("温度也会显示 --", result[0]["message"])
        self.assertIn("cap_sys_rawio+ep", result[0]["commands"])
        self.assertNotIn("cap_sys_admin", result[0]["commands"])
        for disk in disks:
            self.assertIn("/dev/" + disk, result[0]["commands"])

    def test_unreadable_devices_are_identified_before_query_time_budget(self):
        with patch.object(patrol, "_read_system_version", return_value="Debian"), \
             patch.object(patrol, "_read_fnos_version", return_value="FnOS"), \
             patch.object(patrol, "_patrol_list_visible_whole_disks", return_value=["sda", "sdb"]), \
             patch.object(patrol, "_hwmon_temp1_for_block", return_value=""), \
             patch.object(patrol, "_resolve_cmd", side_effect=lambda name: {"smartctl": "/usr/sbin/smartctl", "setcap": "/usr/sbin/setcap", "setfacl": "/usr/bin/setfacl"}[name]), \
             patch.object(diagnostics.Path, "exists", return_value=True), \
             patch.object(diagnostics.os, "access", side_effect=lambda p, mode: p != "/dev/sdb"), \
             patch.object(diagnostics.time, "monotonic", side_effect=[0, 1]), \
             patch.object(diagnostics, "_run_readonly", side_effect=subprocess.TimeoutExpired("smartctl", 1.5)) as run:
            results = diagnostics.patrol_access_issues()
        run.assert_called_once()
        permission = [r for r in results if r["code"] == "permission"][0]
        self.assertIn("/dev/sdb", permission["commands"])
        self.assertNotIn("/dev/sda", permission["commands"])

    def test_text_smart_data_is_valid_for_save_check_as_well_as_patrol(self):
        text = (
            "SMART overall-health self-assessment test result: PASSED\n"
            "194 Temperature_Celsius 0x0022 100 058 000 Old_age Always - 32 (0 20 0 0 0)\n"
        )
        self.assertEqual(self.smart_check(self.result(0, text), hwmon=""), [])

    def test_temperature_missing_hint_uses_actual_x86_sata_device(self):
        data = json.dumps({"smart_status": {"passed": True}})
        warning = self.smart_check(self.result(0, data), disk="sda", hwmon="")[0]
        self.assertEqual(warning["title"], "硬盘温度")
        self.assertIn("sudo -u FnMessageBot /usr/sbin/smartctl -H -A /dev/sda", warning["commands"])
        self.assertIn("sudo /usr/sbin/smartctl -H -A /dev/sda", warning["commands"])
        self.assertNotIn("/dev/sdb", warning["commands"])

    def test_save_check_supports_sat_bridge_retry(self):
        responses = [self.result(2, stderr="Unknown USB bridge"),
                     self.result(0, json.dumps({"smart_status": {"passed": True}, "temperature": {"current": 32}}))]
        self.assertEqual(self.smart_check(responses, hwmon=""), [])

    def test_patrol_switch_checks_permissions_without_patrol_event(self):
        with patch.object(diagnostics, "patrol_access_issues", return_value=[]) as probe:
            diagnostics.collect_access_warnings({"nas_patrol_enabled": True}, ["WAN_IP_CHANGED"])
        probe.assert_called_once()

    def test_manual_patrol_checks_permissions_with_schedule_disabled(self):
        with patch.object(diagnostics, "patrol_access_issues", return_value=[]) as probe:
            diagnostics.collect_access_warnings({"nas_patrol_enabled": False}, ["WAN_IP_CHANGED"], include_patrol=True)
        probe.assert_called_once()

    def test_unselected_patrol_does_not_probe_disks(self):
        with patch.object(diagnostics, "patrol_access_issues", side_effect=AssertionError("unselected patrol")):
            self.assertEqual(diagnostics.collect_access_warnings({"nas_patrol_enabled": False}, ["WAN_IP_CHANGED"]), [])

    def test_banner_only_smart_success_does_not_mean_healthy(self):
        result = self.smart_check(self.result(0, "Probable ATA device behind a SAT layer"))[0]
        self.assertEqual(result["code"], "unavailable")
        self.assertIn("未返回健康数据", result["message"])

    def test_smart_timeout_does_not_request_capability(self):
        with patch.object(patrol, "_read_system_version", return_value="Debian"), \
             patch.object(patrol, "_read_fnos_version", return_value="FnOS"), \
             patch.object(patrol, "_patrol_list_visible_whole_disks", return_value=["sdb"]), \
             patch.object(patrol, "_hwmon_temp1_for_block", return_value="32"), \
             patch.object(diagnostics, "_run_readonly", side_effect=subprocess.TimeoutExpired("smartctl", 1.5)):
            result = diagnostics.patrol_access_issues()[0]
        self.assertEqual(result["code"], "timeout")
        self.assertEqual(result["commands"], "")

    def test_device_online_is_not_smart_health(self):
        with patch.object(patrol, "_run_cmd", return_value=""), patch.object(Path, "read_text", return_value="running"):
            self.assertEqual(patrol._smart_health_for_block_path("/dev/sdb"), "--")

    def test_readable_temperature_is_not_disk_health(self):
        with patch.object(patrol, "_patrol_scan_vol_space_by_physical", return_value={}), \
             patch.object(patrol, "_patrol_list_visible_whole_disks", return_value=["sdb"]), \
             patch.object(patrol, "_smart_health_for_disk", return_value="--"), \
             patch.object(patrol, "_smart_temp_for_disk", return_value="32"), \
             patch.object(patrol, "_patrol_best_mount_for_physical", return_value="/vol1"), \
             patch.object(patrol, "_patrol_disk_space_gb_for_row", return_value=("50", "100")):
            rows = patrol._collect_disk_items()
        self.assertEqual(rows[0]["status"], "--")
        self.assertTrue(rows[0]["status_reason"])


if __name__ == "__main__":
    unittest.main()
