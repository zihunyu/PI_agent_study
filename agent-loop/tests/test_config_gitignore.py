"""真实配置不能被 Git 跟踪的安全边界测试。"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class ConfigGitIgnoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def check_ignored(self, relative_path: str) -> bool:
        result = subprocess.run(
            ["git", "check-ignore", "-q", relative_path],
            cwd=self.root,
            check=False,
        )
        return result.returncode == 0

    def test_真实_toml_配置必须被忽略(self) -> None:
        self.assertTrue(self.check_ignored("config/agent.toml"))
        self.assertTrue(self.check_ignored("config/providers.toml"))
        self.assertTrue(self.check_ignored("config/business.toml"))

    def test_example_配置允许提交(self) -> None:
        self.assertFalse(self.check_ignored("config/agent.toml.example"))
        self.assertFalse(self.check_ignored("config/providers.toml.example"))
        self.assertFalse(self.check_ignored("config/business.toml.example"))

    def test_真实配置当前没有被_git_跟踪(self) -> None:
        tracked = subprocess.run(
            [
                "git",
                "ls-files",
                "--",
                "config/agent.toml",
                "config/providers.toml",
                "config/business.toml",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(tracked, "")


if __name__ == "__main__":
    unittest.main()
