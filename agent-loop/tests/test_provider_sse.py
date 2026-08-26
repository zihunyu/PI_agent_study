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


if __name__ == "__main__":
    unittest.main()
