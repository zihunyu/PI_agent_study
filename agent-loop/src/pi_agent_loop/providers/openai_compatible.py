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
from .sse import (
    DEFAULT_MAX_SSE_EVENT_BYTES,
    DEFAULT_MAX_SSE_EVENTS,
    DEFAULT_MAX_SSE_LINE_BYTES,
    DEFAULT_MAX_SSE_TOTAL_BYTES,
    iter_sse_data,
)
from .translate import (
    DEFAULT_MAX_TOOL_ARGUMENT_BYTES,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TOTAL_TOOL_ARGUMENT_BYTES,
    OpenAIStreamTranslator,
)


def _positive_limit(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return value


class OpenAICompatibleProvider:
    """把 OpenAI Chat Completions SSE 转成内部 Assistant 事件流。"""

    # Generic Harness lifecycle opt-in; custom providers can expose the same
    # marker without importing this adapter type.
    manage_with_host = True
    # Internal RetryingStreamFn surrounds the actual HTTP attempt, so the
    # unified ModelCallRuntime must not charge provider.stream as another
    # physical call on top of these attempts.
    provides_physical_attempt_admission = True

    def __init__(
        self,
        profile: ProviderProfile,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        owns_client: bool | None = None,
        limits: httpx.Limits | None = None,
        max_sse_line_bytes: int = DEFAULT_MAX_SSE_LINE_BYTES,
        max_sse_event_bytes: int = DEFAULT_MAX_SSE_EVENT_BYTES,
        max_response_bytes: int = DEFAULT_MAX_SSE_TOTAL_BYTES,
        max_sse_events: int = DEFAULT_MAX_SSE_EVENTS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        max_tool_argument_bytes: int = DEFAULT_MAX_TOOL_ARGUMENT_BYTES,
        max_total_tool_argument_bytes: int = DEFAULT_MAX_TOTAL_TOOL_ARGUMENT_BYTES,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("client and transport cannot be supplied together")
        if client is None and owns_client is False:
            raise ValueError(
                "an internally created client must be owned by the provider"
            )
        self.max_sse_line_bytes = _positive_limit(
            "max_sse_line_bytes",
            max_sse_line_bytes,
        )
        self.max_sse_event_bytes = _positive_limit(
            "max_sse_event_bytes",
            max_sse_event_bytes,
        )
        self.max_response_bytes = _positive_limit(
            "max_response_bytes",
            max_response_bytes,
        )
        self.max_sse_events = _positive_limit("max_sse_events", max_sse_events)
        self.max_tool_calls = _positive_limit("max_tool_calls", max_tool_calls)
        self.max_tool_argument_bytes = _positive_limit(
            "max_tool_argument_bytes",
            max_tool_argument_bytes,
        )
        self.max_total_tool_argument_bytes = _positive_limit(
            "max_total_tool_argument_bytes",
            max_total_tool_argument_bytes,
        )
        if self.max_sse_line_bytes > self.max_response_bytes:
            raise ValueError("max_sse_line_bytes 不能大于 max_response_bytes")
        if self.max_sse_event_bytes > self.max_response_bytes:
            raise ValueError("max_sse_event_bytes 不能大于 max_response_bytes")
        if self.max_tool_argument_bytes > self.max_total_tool_argument_bytes:
            raise ValueError(
                "max_tool_argument_bytes 不能大于 max_total_tool_argument_bytes"
            )
        self.profile = profile
        self._client = client or httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            limits=limits
            or httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30.0,
            ),
        )
        # Injected clients are caller-owned unless ownership is explicitly handed
        # to the provider. Internally created clients are always provider-owned.
        self._owns_client = client is None or bool(owns_client)
        self._accepting = True
        self._closed = False
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._close_lock = asyncio.Lock()
        # call_count 是逻辑模型 Turn；attempt_count 包含内部 Retry Attempt。
        self.call_count = 0
        self.attempt_count = 0
        self._retrying_stream = RetryingStreamFn(
            self._stream_attempt,
            profile.retry_policy,
            physical_attempt_admission=True,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        """The shared connection-pooled client used by every retry attempt."""

        return self._client

    @property
    def owns_client(self) -> bool:
        return self._owns_client

    @property
    def closed(self) -> bool:
        return self._closed

    async def aclose(self) -> None:
        """Drain in-flight requests, then close the owned pooled client."""

        async with self._close_lock:
            if self._closed:
                return
            self._accepting = False
            active = tuple(self._active_tasks)
            for task in active:
                task.cancel("provider is closing")
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            if self._owns_client:
                await self._client.aclose()
            self._closed = True

    async def __aenter__(self) -> "OpenAICompatibleProvider":
        if not self._accepting or self._closed:
            raise RuntimeError("provider is closed")
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self.aclose()

    def stream(
        self,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> AssistantMessageEventStream:
        """满足 Agent Loop 的 StreamFn 契约，并在后台执行 HTTP 请求。"""

        if self._closed:
            raise RuntimeError("provider is closed")
        effective_options = dict(options)
        effective_options.update(self.profile.generation.resolve(options))
        self.call_count += 1
        return self._retrying_stream(model, context, effective_options)

    def _stream_attempt(
        self,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> AssistantMessageEventStream:
        if not self._accepting or self._closed:
            raise RuntimeError("provider is closed")
        stream = AssistantMessageEventStream()
        self.attempt_count += 1
        task = asyncio.create_task(
            self._run(stream, model, context, options),
            name=f"pi-provider-call:{self.profile.name}:{model.id}",
        )
        self._active_tasks.add(task)

        def settled(completed: asyncio.Task[None]) -> None:
            self._active_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(settled)
        return stream

    async def _run(
        self,
        stream: AssistantMessageEventStream,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        cancellation = options.get("cancellation_token")
        if cancellation is not None and not isinstance(cancellation, CancellationToken):
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
        from ..model_attempts import current_model_attempt_admission_scope

        scope = current_model_attempt_admission_scope()
        if scope is not None and scope.max_tokens is not None:
            observed = await scope.snapshot()
            remaining = scope.max_tokens - observed.tokens
            for name in ("max_tokens", "max_completion_tokens"):
                if name in payload:
                    payload[name] = min(payload[name], remaining)
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
            # httpx read timeouts apply to each I/O operation.  A peer that
            # trickles one byte before every read timeout can otherwise keep a
            # model attempt alive forever, so enforce a wall-clock deadline too.
            async with asyncio.timeout(self.profile.request_timeout_seconds):
                async with self._client.stream(
                    "POST",
                    self.profile.request_url,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                ) as response:
                    self._raise_for_status(response.status_code, response.headers)
                    content_type = response.headers.get("content-type", "")
                    if content_type and "text/event-stream" not in content_type:
                        raise ProviderProtocolError(
                            "第三方 API 在 stream=true 时未返回 text/event-stream"
                        )

                    translator = OpenAIStreamTranslator(
                        stream,
                        model,
                        max_tool_calls=self.max_tool_calls,
                        max_tool_argument_bytes=self.max_tool_argument_bytes,
                        max_total_tool_argument_bytes=(
                            self.max_total_tool_argument_bytes
                        ),
                    )
                    translator.start()
                    saw_done = False
                    async for data in iter_sse_data(
                        response.aiter_bytes(),
                        max_line_bytes=self.max_sse_line_bytes,
                        max_event_bytes=self.max_sse_event_bytes,
                        max_total_bytes=self.max_response_bytes,
                        max_events=self.max_sse_events,
                    ):
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
                        raise ProviderProtocolError("SSE 连接在 data: [DONE] 之前结束")
        except TimeoutError as error:
            raise ProviderTimeoutError("第三方模型 API 流超过绝对截止时间") from error
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
        final["usageObserved"] = False
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
        final["usageObserved"] = False
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
