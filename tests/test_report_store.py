from __future__ import annotations

import unittest

from core.report_store import _copy_markdown_toolbar, render_markdown


class ReportCopyToolbarTest(unittest.TestCase):
    def test_toolbar_does_not_embed_full_report_text(self) -> None:
        toolbar = _copy_markdown_toolbar()

        self.assertIn("复制 Markdown", toolbar)
        self.assertIn("复制钉钉文本", toolbar)
        self.assertIn("buildCopyText", toolbar)
        self.assertNotIn("<textarea", toolbar)
        self.assertNotIn("data-copy-source", toolbar)

    def test_toolbar_overhead_is_not_tied_to_report_size(self) -> None:
        short_html = _copy_markdown_toolbar() + render_markdown("短报告")
        large_html = _copy_markdown_toolbar() + render_markdown("x" * 10000)

        self.assertLess(len(large_html) - len(short_html), 10100)


if __name__ == "__main__":
    unittest.main()
