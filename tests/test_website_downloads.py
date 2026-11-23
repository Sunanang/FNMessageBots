"""官网下载区回归：普通版与 root 版都要能直接下载，校验值必须与实际文件一致。"""
import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "website"
DOWNLOADS = SITE / "downloads"


class WebsiteDownloadTests(unittest.TestCase):
    def setUp(self):
        self.index = (SITE / "index.html").read_text(encoding="utf-8")
        self.version = json.loads((DOWNLOADS / "version.json").read_text(encoding="utf-8"))

    def test_version_file_lists_both_packages_with_matching_checksums(self):
        names = [asset["name"] for asset in self.version["assets"]]
        self.assertTrue(any(n.endswith("-root.fpk") for n in names), f"缺少 root 版：{names}")
        self.assertTrue(any(n.endswith(".fpk") and not n.endswith("-root.fpk") for n in names),
                        f"缺少普通版：{names}")
        for asset in self.version["assets"]:
            path = DOWNLOADS / asset["name"]
            self.assertTrue(path.is_file(), f"下载区缺少文件 {asset['name']}")
            self.assertEqual(path.stat().st_size, asset["size"], asset["name"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), asset["sha256"], asset["name"])
            self.assertTrue((DOWNLOADS / (asset["name"] + ".sha256")).is_file(), asset["name"] + ".sha256")

    def test_page_offers_direct_download_for_plain_and_root(self):
        for kind in ("plain", "root"):
            self.assertIn(f'data-dl="{kind}"', self.index, f"缺少 {kind} 下载按钮")
        hrefs = re.findall(r'href="(downloads/[^"]+\.fpk)"', self.index)
        self.assertTrue(hrefs, "页面里没有本站直连的 fpk 下载链接")
        for href in hrefs:
            self.assertTrue((SITE / href).is_file(), f"页面指向不存在的文件 {href}")
        self.assertNotIn("releases/download", self.index, "下载按钮不应跳到 GitHub")

    def test_page_fallback_version_matches_shipped_packages(self):
        tag = self.version["tag"]
        self.assertIn(f"<span data-ver>{tag}</span>", self.index, "静态兜底版本号与 downloads/version.json 不一致")


if __name__ == "__main__":
    unittest.main()
