"""真实模型 basic_usage 命令行与动态说明测试。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SPEC = importlib.util.spec_from_file_location(
    "basic_usage_example",
    ROOT / "examples" / "basic_usage.py",
)
assert SPEC is not None and SPEC.loader is not None
BASIC_USAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASIC_USAGE)


class BasicUsageTests(unittest.TestCase):
    def test_命令行后面的多个词会合并为用户消息(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["basic_usage.py", "查询", "订单", "1001"],
        ):
            self.assertEqual(
                BASIC_USAGE.parse_user_message(),
                "查询 订单 1001",
            )

    def test_模型调用原因根据真实历史动态生成(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "one",
                        "name": "add",
                        "arguments": {"a": 8, "b": 9},
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "17"}],
            },
        ]

        explanations = BASIC_USAGE.model_call_explanations(messages)

        self.assertEqual(len(explanations), 2)
        self.assertIn("add", explanations[0])
        self.assertIn("工具结果", explanations[1])

    def test_basic_usage_不再包含_scripted_provider(self) -> None:
        source = (ROOT / "examples" / "basic_usage.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("ScriptedProvider", source)
        self.assertNotIn(
            'await agent.prompt("请同时计算 2+3 和 4×5")',
            source,
        )


if __name__ == "__main__":
    unittest.main()
