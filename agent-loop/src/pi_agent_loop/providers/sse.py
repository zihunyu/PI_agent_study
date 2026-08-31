"""最小但严格的 Server-Sent Events 数据解析器。"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator

from .errors import ProviderProtocolError


DEFAULT_MAX_SSE_LINE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_SSE_EVENT_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_SSE_TOTAL_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_SSE_EVENTS = 100_000
_NEWLINE_RE = re.compile(br"[\r\n]")


def _positive_limit(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return value


class SSEDecoder:
    """接收任意边界的 bytes，并输出完整 SSE `data` 事件。"""

    def __init__(
        self,
        *,
        max_line_bytes: int = DEFAULT_MAX_SSE_LINE_BYTES,
        max_event_bytes: int = DEFAULT_MAX_SSE_EVENT_BYTES,
        max_total_bytes: int = DEFAULT_MAX_SSE_TOTAL_BYTES,
        max_events: int = DEFAULT_MAX_SSE_EVENTS,
    ) -> None:
        self.max_line_bytes = _positive_limit("max_line_bytes", max_line_bytes)
        self.max_event_bytes = _positive_limit("max_event_bytes", max_event_bytes)
        self.max_total_bytes = _positive_limit("max_total_bytes", max_total_bytes)
        self.max_events = _positive_limit("max_events", max_events)
        if self.max_event_bytes > self.max_total_bytes:
            raise ValueError("max_event_bytes 不能大于 max_total_bytes")
        if self.max_line_bytes > self.max_total_bytes:
            raise ValueError("max_line_bytes 不能大于 max_total_bytes")
        # Keep raw bytes until a complete SSE line is available. Newline bytes
        # cannot occur inside a UTF-8 multibyte sequence, so each line can be
        # decoded exactly once. ``_line_start`` and ``_search_from`` are cursors
        # into the bytearray.  Processed bytes are compacted once per ``feed``
        # instead of deleting from the head for every line; the latter turns a
        # single large chunk containing many short events into quadratic work.
        self._buffer = bytearray()
        self._line_start = 0
        self._search_from = 0
        self._data_lines: list[str] = []
        self._event_bytes = 0
        self._total_bytes = 0
        self._events_emitted = 0

    def feed(self, chunk: bytes) -> list[str]:
        self._total_bytes += len(chunk)
        if self._total_bytes > self.max_total_bytes:
            raise ProviderProtocolError("SSE 响应超过累计字节上限")
        self._buffer.extend(chunk)
        events = self._consume_complete_lines(final=False)
        self._assert_pending_line_limit()
        return events

    def finalize(self) -> list[str]:
        events = self._consume_complete_lines(final=True)
        if self._data_lines:
            self._emit_pending_event(events)
        return events

    def _consume_complete_lines(self, *, final: bool) -> list[str]:
        events: list[str] = []
        while self._line_start < len(self._buffer):
            newline_match = _NEWLINE_RE.search(self._buffer, self._search_from)
            newline_at = -1 if newline_match is None else newline_match.start()
            if newline_at < 0:
                if final:
                    line = bytes(self._buffer[self._line_start :])
                    self._line_start = len(self._buffer)
                    self._search_from = self._line_start
                    self._assert_line_limit(line)
                    self._process_line(self._decode_line(line), events)
                else:
                    self._search_from = len(self._buffer)
                break
            if (
                self._buffer[newline_at] == 13
                and newline_at == len(self._buffer) - 1
                and not final
            ):
                # A CR at a chunk boundary may be the first half of CRLF.
                self._search_from = newline_at
                self._assert_pending_line_limit()
                break
            newline_size = (
                2
                if self._buffer[newline_at] == 13
                and newline_at + 1 < len(self._buffer)
                and self._buffer[newline_at + 1] == 10
                else 1
            )
            line = bytes(self._buffer[self._line_start : newline_at])
            self._assert_line_limit(line)
            self._line_start = newline_at + newline_size
            self._search_from = self._line_start
            self._process_line(self._decode_line(line), events)
        self._compact_processed_bytes()
        return events

    def _compact_processed_bytes(self) -> None:
        """Discard a processed prefix with at most one bytearray move per feed."""

        if self._line_start == 0:
            return
        processed = self._line_start
        del self._buffer[:processed]
        self._line_start = 0
        self._search_from = max(0, self._search_from - processed)

    @staticmethod
    def _decode_line(line: bytes) -> str:
        try:
            return line.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ProviderProtocolError("SSE 响应不是合法 UTF-8") from error

    def _assert_line_limit(self, line: bytes) -> None:
        if len(line) > self.max_line_bytes:
            raise ProviderProtocolError("SSE 单行超过字节上限")

    def _assert_pending_line_limit(self) -> None:
        # A CR at a chunk boundary is a pending line delimiter, not payload.
        pending_bytes = len(self._buffer) - (
            1 if self._buffer.endswith(b"\r") else 0
        )
        if pending_bytes > self.max_line_bytes:
            raise ProviderProtocolError("SSE 单行超过字节上限")

    def _emit_pending_event(self, events: list[str]) -> None:
        self._events_emitted += 1
        if self._events_emitted > self.max_events:
            raise ProviderProtocolError("SSE 事件数量超过上限")
        events.append("\n".join(self._data_lines))
        self._data_lines.clear()
        self._event_bytes = 0

    def _process_line(self, line: str, events: list[str]) -> None:
        if line == "":
            if self._data_lines:
                self._emit_pending_event(events)
            return
        if line.startswith(":"):
            return
        field, separator, value = line.partition(":")
        if field != "data":
            return
        if separator and value.startswith(" "):
            value = value[1:]
        value_bytes = len(value.encode("utf-8"))
        candidate_bytes = self._event_bytes + value_bytes
        if self._data_lines:
            candidate_bytes += 1  # newline inserted when data fields are joined
        if candidate_bytes > self.max_event_bytes:
            raise ProviderProtocolError("SSE 单事件超过字节上限")
        self._data_lines.append(value)
        self._event_bytes = candidate_bytes


async def iter_sse_data(
    chunks: AsyncIterable[bytes],
    *,
    max_line_bytes: int = DEFAULT_MAX_SSE_LINE_BYTES,
    max_event_bytes: int = DEFAULT_MAX_SSE_EVENT_BYTES,
    max_total_bytes: int = DEFAULT_MAX_SSE_TOTAL_BYTES,
    max_events: int = DEFAULT_MAX_SSE_EVENTS,
) -> AsyncIterator[str]:
    """把 HTTP bytes 流转换成 SSE data 字符串流。"""

    decoder = SSEDecoder(
        max_line_bytes=max_line_bytes,
        max_event_bytes=max_event_bytes,
        max_total_bytes=max_total_bytes,
        max_events=max_events,
    )
    async for chunk in chunks:
        for data in decoder.feed(chunk):
            yield data
    for data in decoder.finalize():
        yield data
