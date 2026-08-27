"""第三方 Provider 配置与 Factory 测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    ProviderConfigError,
    create_provider,
    load_provider_settings,
)


def config_text(
    *,
    base_url: str = "https://vendor.example/v1/",
    api_key: str = "secret-for-test-only",
    model: str = "vendor-model",
    stream: str = "true",
    allow_insecure_http: str = "false",
    extra: str = "",
) -> str:
    return f"""
[active]
profile = "third_party"

[profiles.third_party]
protocol = "openai_chat_completions"
base_url = "{base_url}"
endpoint = "/chat/completions"
auth_type = "bearer"
api_key = "{api_key}"
model = "{model}"
stream = {stream}
connect_timeout_seconds = 10
request_timeout_seconds = 60
allow_insecure_http = {allow_insecure_http}
{extra}
"""


class ProviderSettingsTests(unittest.TestCase):
    def load(self, content: str):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "providers.toml"
        path.write_text(content, encoding="utf-8")
        return load_provider_settings(path)

    def test_读取_base_url_模型和_bearer_配置(self) -> None:
        settings = self.load(config_text())
        profile = settings.active

        self.assertEqual(profile.name, "third_party")
        self.assertEqual(profile.model, "vendor-model")
        self.assertEqual(profile.auth_type, "bearer")
        self.assertEqual(
            profile.request_url,
            "https://vendor.example/v1/chat/completions",
        )
        self.assertNotIn("secret-for-test-only", repr(profile))
        self.assertNotIn("secret-for-test-only", repr(settings))

    def test_factory_使用配置模型创建_provider(self) -> None:
        settings = self.load(config_text(model="chosen-model"))

        model, provider = create_provider(settings)

        self.assertEqual(model.id, "chosen-model")
        self.assertEqual(model.provider, "third_party")
        self.assertEqual(model.api, "openai_chat_completions")
        self.assertIs(provider.profile, settings.active)

    def test_示例占位值不能作为真实配置运行(self) -> None:
        with self.assertRaisesRegex(ProviderConfigError, "占位值"):
            self.load(config_text(api_key="REPLACE_WITH_YOUR_API_KEY"))

    def test_远程明文_http_默认被拒绝(self) -> None:
        with self.assertRaisesRegex(ProviderConfigError, "HTTPS"):
            self.load(config_text(base_url="http://vendor.example/v1"))

    def test_localhost_允许_http(self) -> None:
        settings = self.load(config_text(base_url="http://127.0.0.1:8000/v1"))
        self.assertEqual(
            settings.active.request_url,
            "http://127.0.0.1:8000/v1/chat/completions",
        )

    def test_unknown_字段被拒绝(self) -> None:
        with self.assertRaisesRegex(ProviderConfigError, "未知字段"):
            self.load(config_text(extra="unexpected = 1"))

    def test_读取模型重试配置(self) -> None:
        settings = self.load(
            config_text(
                extra="""
[profiles.third_party.retry]
enabled = true
max_retries = 3
initial_delay_seconds = 0.1
max_delay_seconds = 5
jitter_ratio = 0
max_elapsed_seconds = 10
retryable_statuses = [429, 503]

[profiles.third_party.retry.circuit_breaker]
enabled = true
failure_threshold = 2
recovery_timeout_seconds = 4
"""
            )
        )
        policy = settings.active.retry_policy
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.max_retries, 3)
        self.assertEqual(policy.retryable_statuses, frozenset({429, 503}))
        self.assertEqual(policy.max_elapsed_seconds, 10)
        self.assertTrue(policy.circuit_breaker.enabled)
        self.assertEqual(policy.circuit_breaker.failure_threshold, 2)

    def test_stream_false_被拒绝(self) -> None:
        with self.assertRaisesRegex(ProviderConfigError, "stream=true"):
            self.load(config_text(stream="false"))


if __name__ == "__main__":
    unittest.main()
