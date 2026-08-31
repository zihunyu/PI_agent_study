"""SSE 任意网络分片解析测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop.providers import SSEDecoder  # noqa: E402


class SSEDecoderTests(unittest.TestCase):
    def test_json_可以跨_utf8_和网络_chunk(self) -> None:
        raw = 'data: {"text":"你好"}\r\n\r\ndata: [DONE]\n\n'.encode()
        decoder = SSEDecoder()
        events: list[str] = []

        # 刻意逐字节喂入，使中文 UTF-8 和 CRLF 都跨 chunk。
        for byte in raw:
            events.extend(decoder.feed(bytes([byte])))
        events.extend(decoder.finalize())

        self.assertEqual(events, ['{"text":"你好"}', "[DONE]"])

    def test_一个_chunk_包含多事件和多_data_行(self) -> None:
        decoder = SSEDecoder()
        events = decoder.feed(
            b": comment\n"
            b"event: message\n"
            b"data: first\n"
            b"data: second\n\n"
            b"data: third\n\n"
        )
        events.extend(decoder.finalize())
        self.assertEqual(events, ["first\nsecond", "third"])

    def test_末尾没有空行时_finalize_仍返回事件(self) -> None:
        decoder = SSEDecoder()
        self.assertEqual(decoder.feed(b"data: final"), [])
        self.assertEqual(decoder.finalize(), ["final"])

    def test_单个大chunk内大量短事件线性处理且保持顺序(self) -> None:
        decoder = SSEDecoder(
            max_line_bytes=64,
            max_event_bytes=64,
            max_total_bytes=2 * 1024 * 1024,
            max_events=10_000,
        )
        raw = b"".join(
            f"data: event-{index}\n\n".encode() for index in range(10_000)
        )

        events = decoder.feed(raw)
        events.extend(decoder.finalize())

        self.assertEqual(len(events), 10_000)
        self.assertEqual(events[0], "event-0")
        self.assertEqual(events[-1], "event-9999")
        self.assertEqual(len(decoder._buffer), 0)

    def test_大chunk末尾cr跨chunk时只压缩已处理前缀(self) -> None:
        decoder = SSEDecoder()

        self.assertEqual(decoder.feed(b"data: first\n\ndata: second\r"), ["first"])
        self.assertEqual(decoder.feed(b"\n\r\n"), ["second"])
        self.assertEqual(decoder.finalize(), [])


if __name__ == "__main__":
    unittest.main()
