"""AI 业务需求入口文件的契约测试。"""

from __future__ import annotations

import unittest
from pathlib import Path


class BusinessRequirementsDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def test_agents_要求业务开发前读取需求文件(self) -> None:
        instructions = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("BUSINESS_REQUIREMENTS.md", instructions)
        self.assertIn("必须先完整阅读", instructions)

    def test_需求文件包含生成可用配置和工具所需章节(self) -> None:
        requirements = (self.root / "BUSINESS_REQUIREMENTS.md").read_text(
            encoding="utf-8"
        )
        for heading in (
            "产品信息",
            "Intent 清单",
            "工具与外部系统清单",
            "写操作与审批",
            "禁止和超范围规则",
            "参数规则",
            "预期对话验收用例",
            "business.toml 生成规则",
            "工具实现生成规则",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, requirements)


if __name__ == "__main__":
    unittest.main()
