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
        self.assertIn("TOOLS_IMPLEMENTATION_GUIDE.md", instructions)
        self.assertIn("STATE_MACHINE_IMPLEMENTATION_GUIDE.md", instructions)
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
            "业务状态机定义",
            "Session、Approval 和恢复要求",
            "预期对话验收用例",
            "business.toml 生成规则",
            "工具实现生成规则",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, requirements)

    def test_工具指南包含实现注册测试和完成标准(self) -> None:
        guide = (self.root / "TOOLS_IMPLEMENTATION_GUIDE.md").read_text(
            encoding="utf-8"
        )
        for heading in (
            "什么叫一个“完整工具”",
            "JSON Schema 要求",
            "运行时参数校验",
            "CancellationToken",
            "独立 Timeout",
            "注册方式一：ToolRegistry",
            "注册方式三：CapabilityRegistry",
            "公开导出",
            "Agent 集成测试",
            "Definition of Done",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, guide)

    def test_状态机指南包含业务实现和恢复安全契约(self) -> None:
        guide = (self.root / "STATE_MACHINE_IMPLEMENTATION_GUIDE.md").read_text(
            encoding="utf-8"
        )
        for heading in (
            "Command 不能直接改状态",
            "可信事实来源",
            "Approval 状态机",
            "写操作和幂等",
            "Replay Policy",
            "完整 Session 和恢复",
            "并发和 expected_version",
            "Reducer 必须是纯函数",
            "必须测试的情况",
            "Definition of Done",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, guide)


if __name__ == "__main__":
    unittest.main()
