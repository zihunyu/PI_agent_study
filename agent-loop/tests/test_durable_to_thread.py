"""Durable thread boundary cancellation regressions."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest

from pi_agent_loop import DurableAgentHost, Model, ScriptedProvider
from pi_agent_loop.async_utils import durable_to_thread


MODEL = Model(id="durable-thread-model", provider="test", api="scripted")


async def _wait_for_thread_event(
    event: threading.Event,
    *,
    timeout: float = 2,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        if loop.time() >= deadline:
            raise TimeoutError("thread event did not become ready")
        await asyncio.sleep(0.001)


class DurableToThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_调用方连续取消两次仍等待线程完成后传播第一次取消(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_work() -> str:
            started.set()
            try:
                if not release.wait(timeout=5):
                    raise TimeoutError("test worker was not released")
                return "committed"
            finally:
                finished.set()

        task = asyncio.create_task(durable_to_thread(blocking_work))
        try:
            await _wait_for_thread_event(started)

            task.cancel("first cancellation")
            await asyncio.sleep(0)
            task.cancel("second cancellation")
            await asyncio.sleep(0)

            self.assertFalse(task.done())
            self.assertFalse(finished.is_set())
        finally:
            release.set()

        with self.assertRaises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(task, timeout=2)
        self.assertEqual(raised.exception.args, ("first cancellation",))
        self.assertTrue(finished.is_set())

    async def test_真实journal读取线程结束前host_close不会返回(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="durable-thread-host-close",
                state_dir=directory,
                model=MODEL,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="integration",
                tools=[],
                auto_recover=False,
            )
            journal = host.resources.journal
            self.assertIsNotNone(journal)
            assert journal is not None
            original_load = journal._load_events_sync

            def blocking_load(*args, **kwargs):
                started.set()
                try:
                    if not release.wait(timeout=5):
                        raise TimeoutError("test journal worker was not released")
                    return original_load(*args, **kwargs)
                finally:
                    finished.set()

            journal._load_events_sync = blocking_load
            recovery_task = asyncio.create_task(host.recover_on_startup())
            close_task: asyncio.Task[None] | None = None
            try:
                await _wait_for_thread_event(started)
                close_task = asyncio.create_task(host.close())
                await asyncio.sleep(0.02)

                self.assertFalse(recovery_task.done())
                self.assertFalse(close_task.done())
                self.assertFalse(finished.is_set())
            finally:
                release.set()

            assert close_task is not None
            await asyncio.wait_for(close_task, timeout=2)
            with self.assertRaises(asyncio.CancelledError):
                await recovery_task
            self.assertTrue(finished.is_set())
            self.assertTrue(host.lifecycle.closed)
            self.assertEqual(host.lifecycle.active_operation_count, 0)


if __name__ == "__main__":
    unittest.main()
