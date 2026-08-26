"""父子 CancellationToken 的传播和解绑测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import CancellationToken  # noqa: E402


class CancellationTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_父令牌取消会传播给全部子令牌(self) -> None:
        parent = CancellationToken()
        add_child = parent.create_child()
        multiply_child = parent.create_child()

        parent.cancel("用户取消 Agent")

        self.assertTrue(parent.cancelled)
        self.assertTrue(add_child.cancelled)
        self.assertTrue(multiply_child.cancelled)
        self.assertEqual(add_child.reason, "用户取消 Agent")
        self.assertEqual(multiply_child.reason, "用户取消 Agent")

    async def test_取消一个子令牌不会取消父令牌和兄弟令牌(self) -> None:
        parent = CancellationToken()
        add_child = parent.create_child()
        multiply_child = parent.create_child()

        add_child.cancel("add 执行超时")

        self.assertTrue(add_child.cancelled)
        self.assertFalse(parent.cancelled)
        self.assertFalse(multiply_child.cancelled)

    async def test_detach_后父令牌不再控制已经结束的子令牌(self) -> None:
        parent = CancellationToken()
        finished_child = parent.create_child()
        finished_child.detach()

        parent.cancel("稍后取消 Agent")

        self.assertTrue(parent.cancelled)
        self.assertFalse(finished_child.cancelled)


if __name__ == "__main__":
    unittest.main()
