"""Security regressions for public errors, journal views and provider streams."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop.event_stream import AssistantMessageEventStream  # noqa: E402
from pi_agent_loop.messages import (  # noqa: E402
    assistant_message,
    error_tool_result,
    public_error_message,
    redact_sensitive_text,
)
from pi_agent_loop.providers import (  # noqa: E402
    OpenAICompatibleProvider,
    ProviderProfile,
    SSEDecoder,
)
from pi_agent_loop.providers.errors import ProviderProtocolError  # noqa: E402
from pi_agent_loop.providers.translate import OpenAIStreamTranslator  # noqa: E402
from pi_agent_loop.session.journal import JournalRedactionPolicy  # noqa: E402
from pi_agent_loop.types import Model  # noqa: E402


MODEL = Model(
    id="chosen-model",
    provider="third_party",
    api="openai_chat_completions",
)


class PublicErrorSanitizationTests(unittest.TestCase):
    def test_exception_text_is_internal_by_default(self) -> None:
        secret = "sk-live-super-secret-value"
        error = RuntimeError(f"database failed Authorization: Bearer {secret}")

        self.assertEqual(
            public_error_message(error, fallback="工具执行失败"),
            "工具执行失败",
        )
        result = error_tool_result(error)

        self.assertEqual(result.content[0]["text"], "工具执行失败")
        self.assertNotIn(secret, repr(result))
        self.assertNotIn("database failed", repr(result))

    def test_explicit_public_text_and_details_are_redacted(self) -> None:
        secret = "sk-live-super-secret-value"
        result = error_tool_result(
            f"Authorization: Bearer {secret}",
            details={
                "code": "tool_execution_error",
                "access_token": secret,
                "nested": {"reason": f"client_secret={secret}"},
            },
        )

        rendered = repr(result)
        self.assertNotIn(secret, rendered)
        self.assertIn("<redacted>", rendered)
        self.assertEqual(result.details["access_token"], "<redacted>")

    def test_assistant_error_message_redacts_free_text_credentials(self) -> None:
        secret = "sk-live-super-secret-value"
        message = assistant_message(
            model=MODEL,
            error_message=f"upstream failed; refresh_token={secret}",
        )

        self.assertNotIn(secret, message["errorMessage"])
        self.assertIn("<redacted>", message["errorMessage"])

    def test_common_cloud_scm_chat_and_pem_dummy_tokens_are_redacted(self) -> None:
        aws_dummy = "AKIA1234567890ABCDEF"
        github_dummy = "ghp_aaaaaaaaaaaaaaaaaaaa"
        slack_dummy = "xoxb-1234567890-abcdefghij"
        pem_dummy = (
            "-----BEGIN PRIVATE KEY-----\n"
            "ZHVtbXktbm90LWEtcmVhbC1rZXk=\n"
            "-----END PRIVATE KEY-----"
        )
        source = "\n".join((aws_dummy, github_dummy, slack_dummy, pem_dummy))

        redacted = redact_sensitive_text(source)

        for dummy in (aws_dummy, github_dummy, slack_dummy, pem_dummy):
            self.assertNotIn(dummy, redacted)
        self.assertGreaterEqual(redacted.count("<redacted>"), 4)


class JournalRedactionPolicyTests(unittest.TestCase):
    def test_combination_keys_and_free_text_credentials_are_redacted(self) -> None:
        secret = "sk-live-super-secret-value"
        redacted = JournalRedactionPolicy().redact(
            {
                "access_token": secret,
                "refresh-token": secret,
                "client_secret": secret,
                "nested": {
                    "message": f"Authorization: Bearer {secret}",
                    "detail": f"password={secret}",
                },
                "token_count": 12,
            }
        )

        self.assertEqual(redacted["access_token"], "<redacted>")
        self.assertEqual(redacted["refresh-token"], "<redacted>")
        self.assertEqual(redacted["client_secret"], "<redacted>")
        self.assertNotIn(secret, repr(redacted))
        self.assertEqual(redacted["token_count"], 12)


class SSELimitTests(unittest.TestCase):
    def test_line_limit_rejects_unterminated_stream_before_unbounded_growth(
        self,
    ) -> None:
        decoder = SSEDecoder(
            max_line_bytes=8,
            max_event_bytes=16,
            max_total_bytes=32,
        )

        with self.assertRaisesRegex(ProviderProtocolError, "单行"):
            decoder.feed(b"x" * 9)

    def test_trickle_line_is_scanned_incrementally_and_still_bounded(self) -> None:
        decoder = SSEDecoder(
            max_line_bytes=256,
            max_event_bytes=512,
            max_total_bytes=512,
        )

        for _ in range(256):
            self.assertEqual(decoder.feed(b"x"), [])
        with self.assertRaisesRegex(ProviderProtocolError, "单行"):
            decoder.feed(b"x")

    def test_split_crlf_does_not_count_delimiter_against_line_limit(self) -> None:
        decoder = SSEDecoder(
            max_line_bytes=7,
            max_event_bytes=8,
            max_total_bytes=32,
        )

        self.assertEqual(decoder.feed(b"data: x\r"), [])
        self.assertEqual(decoder.feed(b"\n\r\n"), ["x"])

    def test_event_total_bytes_and_event_count_are_bounded(self) -> None:
        event_limited = SSEDecoder(
            max_line_bytes=16,
            max_event_bytes=5,
            max_total_bytes=64,
        )
        with self.assertRaisesRegex(ProviderProtocolError, "单事件"):
            event_limited.feed(b"data: abc\ndata: def\n\n")

        total_limited = SSEDecoder(
            max_line_bytes=8,
            max_event_bytes=8,
            max_total_bytes=12,
        )
        total_limited.feed(b": one\n")
        with self.assertRaisesRegex(ProviderProtocolError, "累计字节"):
            total_limited.feed(b": two\n\n")

        count_limited = SSEDecoder(
            max_line_bytes=16,
            max_event_bytes=16,
            max_total_bytes=64,
            max_events=1,
        )
        with self.assertRaisesRegex(ProviderProtocolError, "事件数量"):
            count_limited.feed(b"data: one\n\ndata: two\n\n")


class ProviderStreamLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_argument_accumulator_is_bounded(self) -> None:
        stream = AssistantMessageEventStream()
        translator = OpenAIStreamTranslator(
            stream,
            MODEL,
            max_tool_argument_bytes=4,
        )

        with self.assertRaisesRegex(ProviderProtocolError, "arguments"):
            translator.feed(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {
                                            "name": "write",
                                            "arguments": "12345",
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            )

    async def test_absolute_stream_deadline_stops_trickle_response(self) -> None:
        closed = asyncio.Event()

        class TrickleBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    await asyncio.sleep(0.005)
                    yield b": keepalive\n\n"

            async def aclose(self) -> None:
                closed.set()

        profile = ProviderProfile(
            name="third_party",
            protocol="openai_chat_completions",
            base_url="https://vendor.example/v1",
            endpoint="/chat/completions",
            auth_type="bearer",
            api_key="test-only-key",
            model=MODEL.id,
            stream=True,
            connect_timeout_seconds=1,
            request_timeout_seconds=0.03,
            allow_insecure_http=False,
        )
        provider = OpenAICompatibleProvider(
            profile,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=TrickleBody(),
                )
            ),
        )
        try:
            result_stream = provider.stream(
                MODEL,
                {"systemPrompt": "", "messages": [], "tools": []},
                {},
            )
            events = await asyncio.wait_for(
                _collect(result_stream),
                timeout=1,
            )
            result = await result_stream.result()
        finally:
            await provider.aclose()

        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(
            result["providerError"]["code"],
            "provider_timeout_error",
        )
        self.assertTrue(closed.is_set())


async def _collect(stream: AssistantMessageEventStream) -> list[dict]:
    return [event async for event in stream]


if __name__ == "__main__":
    unittest.main()
