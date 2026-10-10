"""巡检容量回归：物理整盘、文件系统与可用空间分别采集和展示。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
source = root / "src"
if not source.is_dir():
    source = root / "cmd/fnmessagebots/src"
sys.path.insert(0, str(source))

from monitor import nas_patrol as patrol
from notifier.multi_platform_notifier import MultiPlatformNotifier


class PatrolDiskCapacityTests(unittest.TestCase):
    def report(self, disks):
        notifier = MultiPlatformNotifier.__new__(MultiPlatformNotifier)
        return notifier._build_nas_patrol_content({"disks": disks})

    def collect_system_disk(self, sectors="500118192"):
        def read(path, **kwargs):
            if str(path) == "/sys/block/nvme0n1/size":
                return sectors
            raise FileNotFoundError(str(path))

        with patch.object(patrol, "_patrol_scan_vol_space_by_physical", return_value={
            "nvme0n1": {"free_gb": "84.7", "total_gb": "138.3"},
        }), patch.object(patrol, "_patrol_list_visible_whole_disks", return_value=["nvme0n1"]), \
             patch.object(patrol, "_smart_health_for_disk", return_value="健康"), \
             patch.object(patrol, "_smart_temp_for_disk", return_value="37"), \
             patch.object(patrol, "_patrol_best_mount_for_physical", return_value="/vol1"), \
             patch.object(Path, "read_text", read), \
             patch.object(patrol, "_run_cmd", return_value=""):
            return patrol._collect_disk_items()

    def test_system_disk_reports_whole_capacity_and_smaller_data_volume(self):
        disks = self.collect_system_disk()
        self.assertEqual(disks[0]["size_gb"], "238.47")
        self.assertEqual(disks[0]["filesystem_size_gb"], "138.3")
        self.assertEqual(disks[0]["free_gb"], "84.7")
        report = self.report(disks)
        self.assertIn("硬盘容量: 238.47GB", report)
        self.assertIn("存储空间剩余: 84.7GB / 138.3GB", report)
        self.assertNotIn("84.7GB / 238.47GB", report)
        self.assertIn("温度: 37℃", report)
        self.assertIn("健康状态: 正常", report)

    def test_missing_whole_capacity_does_not_copy_partition_capacity(self):
        report = self.report(self.collect_system_disk(sectors="unavailable"))
        self.assertIn("硬盘容量: --", report)
        self.assertIn("存储空间剩余: 84.7GB / 138.3GB", report)

    def test_capacity_falls_back_to_whole_device_lsblk_bytes(self):
        with patch.object(Path, "read_text", side_effect=PermissionError), \
             patch.object(patrol, "_run_cmd", return_value="256060514304\n") as run:
            self.assertEqual(patrol._disk_size_gb_for_disk("nvme0n1"), "238.47")
        self.assertEqual(run.call_args.args[0][-1], "/dev/nvme0n1")

    def test_unmounted_disk_has_capacity_without_invented_free_space(self):
        with patch.object(patrol, "_patrol_vol_bag_for_physical", return_value=None), \
             patch.object(patrol, "_patrol_best_mount_for_physical", return_value=""), \
             patch.object(patrol, "_patrol_space_gb_via_findmnt_for_disk", return_value=("--", "--")), \
             patch.object(patrol, "_patrol_disk_free_gb_for_row", return_value="--"):
            free, total = patrol._patrol_disk_space_gb_for_row("", "sda")
        report = self.report([{"size_gb": "238.47", "free_gb": free, "filesystem_size_gb": total}])
        self.assertIn("硬盘容量: 238.47GB", report)
        self.assertIn("存储空间剩余: -- / --", report)

    def test_free_only_fallback_does_not_use_whole_disk_as_volume_denominator(self):
        with patch.object(patrol, "_patrol_vol_bag_for_physical", return_value=None), \
             patch.object(patrol, "_patrol_best_mount_for_physical", return_value=""), \
             patch.object(patrol, "_patrol_space_gb_via_findmnt_for_disk", return_value=("--", "--")), \
             patch.object(patrol, "_patrol_disk_free_gb_for_row", return_value="84.7"):
            pair = patrol._patrol_disk_space_gb_for_row("", "nvme0n1")
        self.assertEqual(pair, ("84.7", "--"))

    def test_mounted_filesystem_fallback_keeps_free_and_total_together(self):
        with patch.object(patrol, "_patrol_vol_bag_for_physical", return_value=None), \
             patch.object(patrol, "_patrol_df_space_gb_pair", return_value=("84.7", "138.3")):
            self.assertEqual(patrol._patrol_disk_space_gb_for_row("/vol1", "nvme0n1"),
                             ("84.7", "138.3"))

    def test_raid_volume_can_be_larger_than_one_physical_disk(self):
        report = self.report([{
            "device": "sda", "size_gb": "1000.00", "free_gb": "1536.0", "filesystem_size_gb": "2048.0",
        }])
        self.assertIn("硬盘容量: 1000.00GB", report)
        self.assertIn("存储空间剩余: 1.5TB / 2.0TB", report)

    def test_large_disk_capacity_retains_two_decimal_places(self):
        report = self.report([{"size_gb": "11172.0", "free_gb": "0.0", "filesystem_size_gb": "11170.0"}])
        self.assertIn("硬盘容量: 10.91TB", report)
        self.assertIn("存储空间剩余: 0.0GB / 10.9TB", report)


if __name__ == "__main__":
    unittest.main()
