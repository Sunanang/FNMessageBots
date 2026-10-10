"""巡检采集回归：使用模拟网络和 sysfs，不发送通知、不读取 NAS 私有数据。"""
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

# 同一组用例可验证 Docker 与 FPK 的源码副本。
project_root = Path(__file__).resolve().parents[1]
default_source = project_root / "src"
if not default_source.is_dir():
    default_source = project_root / "cmd/fnmessagebots/src"
source_root = os.environ.get("FNMB_TEST_SRC", str(default_source))
sys.path.insert(0, source_root)
from monitor import nas_patrol as patrol
from monitor import wan_ip_monitor as wan
from utils import fnos_platform as platform


class FakeSocket:
    def __init__(self, response):
        self.response = response
        self.closed = False

    def settimeout(self, value):
        pass

    def connect(self, address):
        pass

    def sendall(self, data):
        pass

    def makefile(self, mode):
        return io.BytesIO(self.response)

    def close(self):
        self.closed = True


class PatrolRegressionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_lan_prefers_default_route_over_virtual_and_down_interfaces(self):
        address = lambda ip: SimpleNamespace(family=socket.AF_INET, address=ip)
        psutil = SimpleNamespace(
            net_if_addrs=lambda: {
                "docker0": [address("172.17.0.1")],
                "eth0": [address("192.168.1.20")],
                "eth1": [address("10.0.0.20")],
                "eth2": [address("192.168.1.30")],
            },
            net_if_stats=lambda: {name: SimpleNamespace(isup=name != "eth2") for name in ["docker0", "eth0", "eth1", "eth2"]},
        )
        with patch.object(patrol, "psutil", psutil), patch.object(patrol, "_default_ipv4_interface", return_value="eth1"):
            self.assertEqual(patrol._pick_lan_ip(), "10.0.0.20")

    def test_bridge_lan_override_without_psutil(self):
        with patch.dict(os.environ, {"NAS_LAN_IP": "192.168.1.100"}), patch.object(patrol, "psutil", None):
            self.assertEqual(patrol._pick_lan_ip(), "192.168.1.100")

    def test_lan_excludes_link_local_and_down_addresses(self):
        psutil = SimpleNamespace(
            net_if_addrs=lambda: {"eth0": [SimpleNamespace(family=socket.AF_INET, address="169.254.1.2")]},
            net_if_stats=lambda: {},
        )
        with patch.object(patrol, "psutil", psutil), patch.object(patrol, "_default_ipv4_interface", return_value="eth0"):
            self.assertEqual(patrol._pick_lan_ip(), "--")

    def test_default_route_uses_lowest_metric_up_interface(self):
        routes = "Iface Destination Gateway Flags RefCnt Use Metric Mask\neth0 00000000 01010101 0003 0 0 200 00000000\neth1 00000000 01010101 0003 0 0 10 00000000\neth2 00000000 01010101 0000 0 0 0 00000000\n"
        with patch.object(Path, "read_text", return_value=routes):
            self.assertEqual(patrol._default_ipv4_interface(), "eth1")

    def http_call(self, responses):
        sockets = [FakeSocket(response) for response in responses]
        with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 80))]), patch.object(socket, "socket", side_effect=sockets):
            try:
                return patrol._http_get_ip("http://diagnostic.invalid/ip", family=socket.AF_INET)
            finally:
                self.assertTrue(all(sock.closed for sock in sockets))

    def test_http_decodes_chunked_ip(self):
        response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n7\r\n8.8.8.8\r\n0\r\n\r\n"
        self.assertEqual(self.http_call([response]), "8.8.8.8")

    def test_http_rejects_error_status_even_with_ip_body(self):
        with self.assertRaises(ValueError):
            self.http_call([b"HTTP/1.1 403 Forbidden\r\nContent-Length: 7\r\n\r\n8.8.8.8"])

    def test_http_follows_redirect(self):
        self.assertEqual(self.http_call([
            b"HTTP/1.1 302 Found\r\nLocation: /new-ip\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\n8.8.8.8",
        ]), "8.8.8.8")

    def test_http_redirect_limit(self):
        redirect = b"HTTP/1.1 302 Found\r\nLocation: /new-ip\r\nContent-Length: 0\r\n\r\n"
        with self.assertRaises(ValueError):
            self.http_call([redirect] * 3)

    def test_wan_rejects_private_ip_and_retains_ipv4_priority(self):
        with patch.object(patrol, "_http_get_ip", side_effect=["192.168.1.10", "8.8.8.8"]):
            self.assertEqual(patrol._pick_wan_ip(), "8.8.8.8")

    def test_wan_ipv6_fallback_rejects_wrong_family(self):
        def result(url, *, family, **kwargs):
            if family == socket.AF_INET:
                raise OSError("IPv4 unavailable")
            return "8.8.8.8" if "api6" in url else "2001:4860:4860::8888"
        with patch.object(patrol, "_http_get_ip", side_effect=result), patch.object(patrol.time, "sleep"):
            self.assertEqual(patrol._pick_wan_ip(), "2001:4860:4860::8888")

    def test_wan_monitor_reuses_patrol_result_without_second_request(self):
        with patch.object(patrol, "_pick_wan_ip") as query:
            self.assertEqual(wan._fetch_wan_ip("8.8.8.8"), "8.8.8.8")
            self.assertEqual(wan._fetch_wan_ip("--"), "--")
            self.assertEqual(wan._fetch_wan_ip("192.168.1.10"), "--")
            query.assert_not_called()

    def test_patrol_report_uses_monitor_selected_ip(self):
        app = SimpleNamespace(config=SimpleNamespace(nas_patrol_enabled=True), notifier=SimpleNamespace(send_notification=Mock(return_value=SimpleNamespace(success=True))))
        payload = {"wan_ip": "2001:4860:4860::8888"}
        with patch.object(patrol, "_collect_patrol_payload", return_value=payload), patch.object(wan, "check_and_notify_wan_ip_change", return_value="8.8.8.8"):
            self.assertTrue(patrol._send_patrol_notification(app, None))
        self.assertEqual(app.notifier.send_notification.call_args.kwargs["event_data"]["wan_ip"], "8.8.8.8")

    def test_wan_ipv6_does_not_replace_existing_ipv4_baseline(self):
        app = SimpleNamespace(config=SimpleNamespace(monitor_events=["WAN_IP_CHANGED"], cursor_dir="unused"), notifier=Mock())
        state = {"last_wan_ip": "8.8.8.8", "baseline_done": True}
        with patch.object(wan, "_fetch_wan_ip", return_value="2001:4860:4860::8888"), patch.object(wan, "_load_state", return_value=state), patch.object(wan, "_save_state"):
            self.assertEqual(wan.check_and_notify_wan_ip_change(app), "8.8.8.8")
        app.notifier.send_notification.assert_not_called()

    def test_container_system_version_uses_host_release(self):
        def read(path, **kwargs):
            if str(path) == "/host/etc/os-release":
                return 'NAME="Debian GNU/Linux"\nPRETTY_NAME="Debian GNU/Linux 12 (host)"\n'
            raise FileNotFoundError(str(path))
        with patch.object(patrol, "_in_container", return_value=True), patch.object(Path, "read_text", read):
            self.assertEqual(patrol._read_system_version(), "Debian GNU/Linux 12 (host)")

    def test_container_does_not_report_image_os_without_host_mount(self):
        reads = []
        def read(path, **kwargs):
            reads.append(str(path))
            if str(path) == "/etc/os-release":
                return 'PRETTY_NAME="Debian image"'
            raise FileNotFoundError(str(path))
        with patch.object(patrol, "_in_container", return_value=True), patch.object(Path, "read_text", read):
            self.assertEqual(patrol._read_system_version(), "--")
        self.assertNotIn("/etc/os-release", reads)

    def test_native_system_version_reads_local_release(self):
        def read(path, **kwargs):
            if str(path) == "/etc/os-release":
                return 'NAME="Debian"\nVERSION_ID="12"'
            raise FileNotFoundError(str(path))
        with patch.object(patrol, "_in_container", return_value=False), patch.object(Path, "read_text", read):
            self.assertEqual(patrol._read_system_version(), "Debian 12")

    def test_api_numeric_and_string_zero_are_success(self):
        for code in [0, "0"]:
            with self.subTest(code=code):
                connection = Mock()
                connection.getresponse.return_value = SimpleNamespace(status=200, read=lambda: json.dumps({"code": code, "data": {"systemVersion": "1.1.3100"}}).encode())
                with patch.dict(os.environ, {"TRIM_API_TOKEN": "test-placeholder"}), patch.object(platform.os.path, "exists", return_value=True), patch.object(platform, "_UnixHTTPConnection", return_value=connection):
                    self.assertEqual(platform.fetch_platform_config(), {"systemVersion": "1.1.3100"})
                connection.close.assert_called_once()

    def test_api_missing_or_error_code_is_rejected(self):
        for code in [None, False, 1, "error"]:
            with self.subTest(code=code):
                connection = Mock()
                connection.getresponse.return_value = SimpleNamespace(status=200, read=lambda: json.dumps({"code": code, "data": {"systemVersion": "wrong"}}).encode())
                with patch.dict(os.environ, {"TRIM_API_TOKEN": "test-placeholder"}), patch.object(platform.os.path, "exists", return_value=True), patch.object(platform, "_UnixHTTPConnection", return_value=connection):
                    self.assertIsNone(platform.fetch_platform_config())

    def test_current_fnos_package_version_precedes_historical_log(self):
        with patch.object(platform, "resolve_fnos_version_via_api", return_value=""), patch.object(Path, "read_text", side_effect=FileNotFoundError), patch.object(patrol, "_fnos_trim_version_from_host_dpkg_status", return_value="1.1.3100"):
            self.assertEqual(patrol._read_fnos_version("FnOS 0.8.0"), "FnOS 1.1.3100")

    def test_historical_version_alone_is_not_reported_as_current(self):
        with patch.object(platform, "resolve_fnos_version_via_api", return_value=""), patch.object(Path, "read_text", side_effect=FileNotFoundError), patch.object(patrol, "_fnos_trim_version_from_host_dpkg_status", return_value=""), patch.object(patrol, "_in_container", return_value=True):
            self.assertEqual(patrol._read_fnos_version("FnOS 0.8.0"), "--")

    def test_fnos_env_override_and_native_dpkg_status(self):
        with patch.dict(os.environ, {"TRIM_SYS_VERSION": "1.1.3100"}):
            self.assertEqual(patrol._read_fnos_version(), "FnOS 1.1.3100")
        def version(path):
            return "1.1.3100" if str(path) == "/var/lib/dpkg/status" else ""
        with patch.object(patrol, "_in_container", return_value=False), patch.object(patrol, "_fnos_trim_version_stream_dpkg_status", side_effect=version):
            self.assertEqual(patrol._fnos_trim_version_from_host_dpkg_status(), "1.1.3100")

    def test_nvme_controller_mapping(self):
        for name, controller in [("nvme0n1", "nvme0"), ("nvme12n2p3", "nvme12"), ("nvme2", "nvme2"), ("sda", "")]:
            self.assertEqual(patrol._nvme_controller_name(name), controller)

    def test_hwmon_temperatures_are_bound_to_each_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, value in [("nvme0", 41999), ("nvme1", 61000)]:
                device = root / "sys/devices" / name
                hw = device / "hwmon/hwmon0"
                hw.mkdir(parents=True)
                (hw / "temp1_input").write_text(str(value))
                (hw / "temp2_input").write_text("99000")
                for link in [root / "sys/class/nvme" / name, root / "sys/class/block" / (name + "n1") / "device"]:
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(device, target_is_directory=True)
            def mapped(path):
                return root / str(path).lstrip("/")
            with patch.object(patrol, "Path", side_effect=mapped):
                self.assertEqual(patrol._hwmon_temp1_for_block("nvme0n1"), "41")
                self.assertEqual(patrol._hwmon_temp1_for_block("nvme1n1"), "61")
                self.assertEqual(patrol._hwmon_temp1_for_block("sda"), "")

    def test_global_hwmon_is_matched_by_device_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, name, value in [(0, "sda", 35000), (1, "sdb", 46000)]:
                device = root / "sys/devices" / name
                device.mkdir(parents=True)
                block = root / "sys/class/block" / name
                block.mkdir(parents=True)
                (block / "device").symlink_to(device, target_is_directory=True)
                hw = root / "sys/class/hwmon" / f"hwmon{index}"
                hw.mkdir(parents=True)
                (hw / "device").symlink_to(device, target_is_directory=True)
                (hw / "temp1_input").write_text(str(value))
            with patch.object(patrol, "Path", side_effect=lambda path: root / str(path).lstrip("/")):
                self.assertEqual(patrol._hwmon_temp1_for_block("sda"), "35")
                self.assertEqual(patrol._hwmon_temp1_for_block("sdb"), "46")

    def test_psutil_ambiguous_temperatures_are_not_reused_for_other_disks(self):
        psutil = SimpleNamespace(sensors_temperatures=lambda: {"nvme": [SimpleNamespace(label="Composite", current=41), SimpleNamespace(label="Composite", current=61)]})
        with patch.object(patrol, "psutil", psutil):
            for name in ["nvme0n1", "nvme1n1", "sda", "sdb"]:
                self.assertEqual(patrol._psutil_disk_temp_fallback(name), "--")

    def test_psutil_exact_device_and_controller_labels(self):
        psutil = SimpleNamespace(sensors_temperatures=lambda: {
            "nvme": [SimpleNamespace(label="nvme0 Composite", current=41), SimpleNamespace(label="nvme1 Composite", current=61)],
            "drivetemp": [SimpleNamespace(label="sda", current=35), SimpleNamespace(label="sdaa", current=99)],
        })
        with patch.object(patrol, "psutil", psutil):
            self.assertEqual(patrol._psutil_disk_temp_fallback("nvme0n1"), "41.0")
            self.assertEqual(patrol._psutil_disk_temp_fallback("nvme1n1"), "61.0")
            self.assertEqual(patrol._psutil_disk_temp_fallback("sda"), "35.0")
            self.assertEqual(patrol._psutil_disk_temp_fallback("sdb"), "--")

    def test_smart_temperature_fallback_still_works_for_target_disk(self):
        smart = json.dumps({"temperature": {"current": 38}})
        with patch.object(patrol, "_hwmon_temp1_for_block", return_value=""), patch.object(patrol, "_run_cmd", return_value=smart):
            self.assertEqual(patrol._smart_temp_for_block_path("/dev/sdb"), "38.0")

    def test_arm_usb_disk_text_uses_raw_194_not_normalized_value(self):
        text = (
            "190 Airflow_Temperature_Cel 0x0022 068 042 045 Old_age Always In_the_past 32 (2 177 33 26 0)\n"
            "194 Temperature_Celsius 0x0022 032 058 000 Old_age Always - 32 (0 20 0 0 0)\n"
        )
        with patch.object(patrol, "_hwmon_temp1_for_block", return_value=""), \
             patch.object(patrol, "_run_cmd", side_effect=["JSON option not supported", text]):
            self.assertEqual(patrol._smart_temp_for_block_path("/dev/sdb"), "32.0")
        self.assertEqual(patrol._parse_celsius_from_smartctl_text(text.splitlines()[0]), "32.0")

    def test_ata_temperature_prefers_194_even_when_190_comes_first(self):
        text = (
            "190 Airflow_Temperature_Cel 0x0022 068 042 045 Old_age Always In_the_past 35 (Min/Max 20/40)\n"
            "194 Temperature_Celsius 0x0022 100 058 000 Old_age Always In_the_past 32 (0 20 0 0 0)\n"
        )
        self.assertEqual(patrol._parse_smart_temperature_output(text), "32.0")

    def test_ata_packed_json_temperature_uses_low_byte(self):
        data = {"ata_smart_attributes": {"table": [
            {"id": 194, "value": 100, "raw": {"value": 32 | (20 << 8)}},
        ]}}
        self.assertEqual(patrol._parse_smart_temperature_output(json.dumps(data)), "32.0")

    def test_readable_hwmon_does_not_require_smartctl_for_temperature(self):
        with patch.object(patrol, "_hwmon_temp1_for_block", return_value="39"), \
             patch.object(patrol, "_run_cmd", side_effect=AssertionError("SMART should not run")):
            self.assertEqual(patrol._smart_temp_for_block_path("/dev/nvme0n1"), "39")

    def test_usb_bridge_error_retries_sat_and_reads_temperature(self):
        data = json.dumps({"temperature": {"current": 32}})
        with patch.object(patrol, "_hwmon_temp1_for_block", return_value=""), \
             patch.object(patrol, "_run_cmd", side_effect=["Unknown USB bridge", data]) as run:
            self.assertEqual(patrol._smart_temp_for_block_path("/dev/sdb"), "32.0")
        self.assertEqual(run.call_args_list[1].args[0],
                         ["smartctl", "-A", "-j", "-d", "sat", "/dev/sdb"])

    def test_sat_retry_is_not_used_for_permission_errors(self):
        self.assertFalse(patrol._smartctl_sat_retry_needed(
            "/dev/sdb", "Unknown USB bridge: Permission denied"))
        with patch.object(patrol, "_run_cmd", return_value="Permission denied") as run:
            self.assertEqual(patrol._run_smartctl("/dev/sdb", ["-H"], 2.5), "Permission denied")
        run.assert_called_once()

    def test_sat_retry_requires_usb_for_scsi_errors(self):
        for path, expected in [
            ("/sys/devices/platform/usb1/1-1/host0/target0/0:0:0:0", True),
            ("/sys/devices/pci0000/ata1/host0/target0/0:0:0:0", False),
        ]:
            with patch.object(Path, "resolve", return_value=Path(path)):
                self.assertEqual(patrol._smartctl_sat_retry_needed(
                    "/dev/sdb", "unsupported field in scsi command"), expected)

    def test_smart_health_uses_actual_status_not_attribute_class(self):
        self.assertEqual(patrol._parse_smart_health_output("SMART Health Status: OK"), "健康")
        self.assertEqual(patrol._parse_smart_health_output("SMART Health Status: FAILED"), "异常")
        self.assertEqual(patrol._parse_smart_health_output(
            json.dumps({"smart_status": {"passed": False}})), "异常")
        self.assertEqual(patrol._parse_smart_health_output("Pre-fail Always - 0"), "--")

    def test_disk_enumeration_matches_reported_arm_and_x86_devices(self):
        for output, expected in [
            ("sda disk\nsdb disk\nsdc disk\nsdd disk\nsde disk\nmmcblk0 disk\nmmcblk0boot0 disk\nzram0 disk\n",
             ["sda", "sdb", "sdc", "sdd", "sde"]),
            ("sda disk\nnvme0n1 disk\n", ["sda", "nvme0n1"]),
        ]:
            with patch.object(patrol, "_run_cmd", return_value=output), \
                 patch.object(Path, "iterdir", return_value=iter([])):
                self.assertEqual(patrol._patrol_list_visible_whole_disks(), expected)

    def test_psutil_does_not_confuse_device_prefixes_or_sensor_two(self):
        psutil = SimpleNamespace(sensors_temperatures=lambda: {
            "nvme": [SimpleNamespace(label="nvme10 Composite", current=99), SimpleNamespace(label="nvme1 Sensor 2", current=80)],
        })
        with patch.object(patrol, "psutil", psutil):
            self.assertEqual(patrol._psutil_disk_temp_fallback("nvme1n1"), "--")

    def test_report_and_monitor_share_ipv4_baseline_without_second_probe(self):
        app = SimpleNamespace(
            config=SimpleNamespace(nas_patrol_enabled=True, monitor_events=["WAN_IP_CHANGED"], cursor_dir="unused"),
            notifier=SimpleNamespace(send_notification=Mock(return_value=SimpleNamespace(success=True))),
        )
        state = {"last_wan_ip": "8.8.8.8", "baseline_done": True}
        with patch.object(patrol, "_collect_patrol_payload", return_value={"wan_ip": "2001:4860:4860::8888"}), patch.object(wan, "_load_state", return_value=state), patch.object(wan, "_save_state"), patch.object(patrol, "_pick_wan_ip") as query:
            self.assertTrue(patrol._send_patrol_notification(app, None))
            query.assert_not_called()
        self.assertEqual(app.notifier.send_notification.call_args.kwargs["event_data"]["wan_ip"], "8.8.8.8")


if __name__ == "__main__":
    unittest.main()
