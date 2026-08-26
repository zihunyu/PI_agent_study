"""Agent TOML 配置加载测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import AgentLimits, load_agent_limits  # noqa: E402


class AgentConfigTests(unittest.TestCase):
    def test_项目配置读取为_10_5_20(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "agent.toml"

        limits = load_agent_limits(config_path)

        self.assertEqual(limits.max_tool_calls, 10)
        self.assertEqual(limits.max_parallel_tools, 5)
        self.assertEqual(limits.max_turns, 20)

    def test_缺少字段时明确报错(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.toml"
            path.write_text(
                "[limits]\nmax_turns=20\nmax_tool_calls=10\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "max_parallel_tools"):
                load_agent_limits(path)

    def test_零负数小数和布尔值都被拒绝(self) -> None:
        invalid_values = [0, -1, 1.5, True]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    AgentLimits(
                        max_tool_calls=value,  # type: ignore[arg-type]
                        max_parallel_tools=5,
                        max_turns=20,
                    )

    def test_未知字段被拒绝以防拼写错误(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.toml"
            path.write_text(
                "[limits]\n"
                "max_turns=20\n"
                "max_tool_calls=10\n"
                "max_parallel_tools=5\n"
                "max_tool_call=999\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "未知字段"):
                load_agent_limits(path)


if __name__ == "__main__":
    unittest.main()
