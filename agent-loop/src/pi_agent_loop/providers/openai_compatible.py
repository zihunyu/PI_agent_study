"""OpenAI-compatible `/chat/completions` 异步 HTTP Provider。"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..cancellation import CancellationToken, OperationCancelledError
from ..event_stream import AssistantMessageEventStream
from ..messages import assistant_message
from ..retry.model import RetryingStreamFn
from ..types import Model
from .errors import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderHTTPError,
    ProviderModelNotFoundError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from .serialize import serialize_chat_request
from .settings import ProviderProfile
from .sse import iter_sse_data
from .translate import OpenAIStreamTranslator


class OpenAICompatibleProvider:
    """把 OpenAI Chat Completions SSE 转成内部 Assistant 事件流。"""

    def __init__(
        self,
        profile: ProviderProfile,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.profile = profile
        self._transport = transport
        # call_count 是逻辑模型 Turn；attempt_count 包含内部 Retry Attempt。
        self.call_count = 0
        self.attempt_count = 0
        self._retrying_stream = RetryingStreamFn(
            self._stream_attempt,
            profile.retry_policy,
        )

    def stream(
        self,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> AssistantMessageEventStream:
        """满足 Agent Loop 的 StreamFn 契约，并在后台执行 HTTP 请求。"""

        self.call_count += 1
        return self._retrying_stream(model, context, options)

    def _stream_attempt(
        self,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> AssistantMessageEventStream:
        stream = AssistantMessageEventStream()
        self.attempt_count += 1
        asyncio.create_task(self._run(stream, model, context, options))
        return stream

    async def _run(
        self,
        stream: AssistantMessageEventStream,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        cancellation = options.get("cancellation_token")
        if cancellation is not None and not isinstance(
            cancellation, CancellationToken
        ):
            cancellation = None

        api_key_override = options.get("api_key")
        api_key = (
            api_key_override
            if isinstance(api_key_override, str) and api_key_override
            else self.profile.api_key
        )

        work = asyncio.create_task(
            self._request(stream, model, context, options, api_key)
        )
        cancellation_waiter: asyncio.Task[None] | None = None
        try:
            if cancellation is None:
                await work
            else:
                if cancellation.cancelled:
                    raise OperationCancelledError(cancellation.reason)
                cancellation_waiter = asyncio.create_task(cancellation.wait())
                done, _ = await asyncio.wait(
                    {work, cancellation_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if work in done:
                    await work
                else:
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    raise OperationCancelledError(cancellation.reason)
        except OperationCancelledError as error:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            self._push_error(stream, model, "aborted", str(error))
        except ProviderError as error:
            self._push_provider_error(stream, model, error)
        except asyncio.CancelledError:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            self._push_error(stream, model, "aborted", "模型请求已取消")
        except Exception:
            # 不把第三方库异常详情直接输出，避免 Header 或请求对象泄密。
            self._push_error(stream, model, "error", "第三方模型请求发生内部错误")
        finally:
            if cancellation_waiter is not None:
                cancellation_waiter.cancel()
                await asyncio.gather(cancellation_waiter, return_exceptions=True)

    async def _request(
        self,
        stream: AssistantMessageEventStream,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
        api_key: str,
    ) -> None:
        payload = serialize_chat_request(self.profile, context, options)
        timeout = httpx.Timeout(
            self.profile.request_timeout_seconds,
            connect=self.profile.connect_timeout_seconds,
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "pi-agent-loop-python/0.1",
        }

        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    self.profile.request_url,
                    headers=headers,
                    json=payload,
                ) as response:
                    self._raise_for_status(response.status_code, response.headers)
                    content_type = response.headers.get("content-type", "")
                    if content_type and "text/event-stream" not in content_type:
                        raise ProviderProtocolError(
                            "第三方 API 在 stream=true 时未返回 text/event-stream"
                        )

                    translator = OpenAIStreamTranslator(stream, model)
                    translator.start()
                    saw_done = False
                    async for data in iter_sse_data(response.aiter_bytes()):
                        if data.strip() == "[DONE]":
                            saw_done = True
                            translator.finish()
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError as error:
                            raise ProviderProtocolError(
                                "SSE data 不是合法 JSON"
                            ) from error
                        translator.feed(event)

                    if not saw_done:
                        raise ProviderProtocolError(
                            "SSE 连接在 data: [DONE] 之前结束"
                        )
        except httpx.TimeoutException as error:
            raise ProviderTimeoutError("第三方模型 API 请求超时") from error
        except httpx.HTTPError as error:
            raise ProviderHTTPError("无法连接第三方模型 API") from error

    @staticmethod
    def _raise_for_status(status_code: int, headers: httpx.Headers) -> None:
        if 200 <= status_code < 300:
            return
        retry_after_ms = _retry_after_ms(headers)
        if status_code in {401, 403}:
            raise ProviderAuthenticationError(
                "第三方 API 鉴权失败，请检查本地 providers.toml 中的 API Key。",
                status_code=status_code,
                retryable=False,
            )
        if status_code == 404:
            raise ProviderModelNotFoundError(
                "第三方 API Endpoint 或配置的模型不存在",
                status_code=status_code,
                retryable=False,
            )
        if status_code == 429:
            raise ProviderRateLimitError(
                "第三方 API 请求过多，请稍后重试",
                status_code=status_code,
                retry_after_ms=retry_after_ms,
                retryable=True,
            )
        raise ProviderHTTPError(
            f"第三方 API 返回 HTTP {status_code}",
            status_code=status_code,
            retry_after_ms=retry_after_ms,
            retryable=status_code in {408, 409} or status_code >= 500,
        )

    @staticmethod
    def _push_provider_error(
        stream: AssistantMessageEventStream,
        model: Model,
        error: ProviderError,
    ) -> None:
        final = assistant_message(
            model=model,
            content=[],
            stop_reason="error",
            error_message=str(error),
        )
        final["providerError"] = {
            "code": error.code,
            "statusCode": error.status_code,
            "retryAfterMs": error.retry_after_ms,
            "retryable": error.retryable,
        }
        stream.push({"type": "error", "reason": "error", "error": final})

    @staticmethod
    def _push_error(
        stream: AssistantMessageEventStream,
        model: Model,
        reason: str,
        message: str,
    ) -> None:
        final = assistant_message(
            model=model,
            content=[],
            stop_reason="aborted" if reason == "aborted" else "error",
            error_message=message,
        )
        stream.push({"type": "error", "reason": reason, "error": final})


def _retry_after_ms(headers: httpx.Headers) -> int | None:
    """读取常见 Retry-After Header；无效或过去时间返回 None/0。"""

    raw_ms = headers.get("retry-after-ms")
    if raw_ms:
        try:
            value = float(raw_ms)
            if math.isfinite(value) and value >= 0:
                return round(value)
        except ValueError:
            pass

    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw)
        if math.isfinite(seconds) and seconds >= 0:
            return round(seconds * 1000)
    except ValueError:
        pass
    try:
        date = parsedate_to_datetime(raw)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        delta = (date - datetime.now(timezone.utc)).total_seconds()
        return max(0, round(delta * 1000))
    except (TypeError, ValueError, OverflowError):
        return None
