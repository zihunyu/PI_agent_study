"""最小但严格的 Server-Sent Events 数据解析器。"""

from __future__ import annotations

import codecs
from collections.abc import AsyncIterable, AsyncIterator

from .errors import ProviderProtocolError


class SSEDecoder:
    """接收任意边界的 bytes，并输出完整 SSE `data` 事件。"""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._text = ""
        self._data_lines: list[str] = []

    def feed(self, chunk: bytes) -> list[str]:
        try:
            self._text += self._decoder.decode(chunk)
        except UnicodeDecodeError as error:
            raise ProviderProtocolError("SSE 响应不是合法 UTF-8") from error
        return self._consume_complete_lines(final=False)

    def finalize(self) -> list[str]:
        try:
            self._text += self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as error:
            raise ProviderProtocolError("SSE 响应不是合法 UTF-8") from error
        events = self._consume_complete_lines(final=True)
        if self._data_lines:
            events.append("\n".join(self._data_lines))
            self._data_lines.clear()
        return events

    def _consume_complete_lines(self, *, final: bool) -> list[str]:
        events: list[str] = []
        while self._text:
            newline_at = -1
            newline_size = 0
            for index, character in enumerate(self._text):
                if character == "\n":
                    newline_at = index
                    newline_size = 1
                    break
                if character == "\r":
                    # 若 CR 位于非最终 chunk 末尾，等待下一 chunk 判断 CRLF。
                    if index == len(self._text) - 1 and not final:
                        return events
                    newline_at = index
                    newline_size = (
                        2
                        if index + 1 < len(self._text)
                        and self._text[index + 1] == "\n"
                        else 1
                    )
                    break
            if newline_at < 0:
                if final:
                    line, self._text = self._text, ""
                    self._process_line(line, events)
                break
            line = self._text[:newline_at]
            self._text = self._text[newline_at + newline_size :]
            self._process_line(line, events)
        return events

    def _process_line(self, line: str, events: list[str]) -> None:
        if line == "":
            if self._data_lines:
                events.append("\n".join(self._data_lines))
                self._data_lines.clear()
            return
        if line.startswith(":"):
            return
        field, separator, value = line.partition(":")
        if field != "data":
            return
        if separator and value.startswith(" "):
            value = value[1:]
        self._data_lines.append(value)


async def iter_sse_data(chunks: AsyncIterable[bytes]) -> AsyncIterator[str]:
    """把 HTTP bytes 流转换成 SSE data 字符串流。"""

    decoder = SSEDecoder()
    async for chunk in chunks:
        for data in decoder.feed(chunk):
            yield data
    for data in decoder.finalize():
        yield data
