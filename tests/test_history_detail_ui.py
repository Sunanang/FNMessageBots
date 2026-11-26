"""历史记录页：行内展开详情（复用已有 /api/push-history/<id> 与 .detail-wrap 样式）。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class HistoryDetailRowTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "src/web/templates/history.html").read_text(encoding="utf-8")

    def test_rows_are_clickable(self):
        self.assertIn("record-row", self.html)
        self.assertIn("rowEl.onclick = function(){ toggleDetail(recordId, rowEl); }", self.html)

    def test_detail_uses_existing_endpoint(self):
        self.assertIn('fetch("api/push-history/" + encodeURIComponent(recordId)', self.html)

    def test_detail_renders_channels_and_event_data(self):
        self.assertIn("renderChannelList", self.html)
        for section in ("概览", "渠道结果", "事件数据"):
            self.assertIn(section, self.html, f"详情缺少「{section}」段")

    def test_only_one_row_expanded_at_a_time(self):
        self.assertIn('document.querySelectorAll(".detail-row").forEach(function(el){ el.parentNode.removeChild(el); })',
                      self.html)

    def test_detail_html_is_escaped(self):
        """详情里的 error/response 来自远端返回，必须转义后再插入。"""
        self.assertIn("function esc(text)", self.html)
        self.assertIn("esc(c.error)", self.html)
        self.assertIn("esc(shortenText(stringifyDetailValue(c.response), 400))", self.html)


if __name__ == "__main__":
    unittest.main()
