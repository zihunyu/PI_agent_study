"""DurableSessionRecovery 的 Lease 续租与取消重入边界。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    DurableSessionRecovery,
    ModelRequestPolicy,
    RecoveryCallbacks,
    SQLiteOperationEventStore,
    replay_operation,
)


def _assistant(text: str) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stopReason": "stop",
    }


async def _unused_tool(_action):
    raise AssertionError("本测试不应调用 Tool Callback")


class DurableSessionRecoveryLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def _start_continuation(
        self,
        store: SQLiteOperationEventStore,
    ) -> None:
        policy = ModelRequestPolicy.no_tools()
        await store.append("operation_started", "session", "operation")
        await store.append(
            "message_appended",
            "session",
            "operation",
            {
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "继续"}],
                }
            },
        )
        await store.append(
            "model_policy_selected",
            "session",
            "operation",
            {"policy": policy.to_dict()},
        )

    async def test_sqlite双worker慢callback期间持续续租(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.db"
            first_store = SQLiteOperationEventStore(path)
            second_store = SQLiteOperationEventStore(path)
            await self._start_continuation(first_store)

            entered = asyncio.Event()
            release = asyncio.Event()
            model_calls = 0

            async def slow_model(_messages, _policy):
                nonlocal model_calls
                model_calls += 1
                entered.set()
                await release.wait()
                return _assistant("完成")

            callbacks = RecoveryCallbacks(
                slow_model,
                _unused_tool,
                _unused_tool,
            )
            first = DurableSessionRecovery(
                first_store,
                claim_lease_seconds=0.12,
                renew_interval=0.03,
            )
            second = DurableSessionRecovery(
                second_store,
                claim_lease_seconds=0.12,
                renew_interval=0.03,
            )

            first_task = asyncio.create_task(
                first.resume(
                    session_id="session",
                    operation_id="operation",
                    callbacks=callbacks,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            # 明确超过原始 Lease；如果没有心跳，第二个 Worker 会接管。
            await asyncio.sleep(0.2)
            claimed = await second.resume(
                session_id="session",
                operation_id="operation",
                callbacks=callbacks,
            )

            self.assertEqual(claimed.status, "recovery_claimed")
            self.assertEqual(model_calls, 1)
            release.set()
            completed = await asyncio.wait_for(first_task, timeout=2)
            self.assertEqual(completed.status, "completed")
            self.assertEqual(model_calls, 1)

            events = await second_store.load(
                session_id="session",
                operation_id="operation",
            )
            self.assertEqual(
                sum(event.type == "model_request_completed" for event in events),
                1,
            )
            self.assertEqual(replay_operation(events).phase, "completed")

    async def test_model_callback取消后可由新worker重入(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.db"
            first_store = SQLiteOperationEventStore(path)
            second_store = SQLiteOperationEventStore(path)
            await self._start_continuation(first_store)

            entered = asyncio.Event()

            async def cancelled_model(_messages, _policy):
                entered.set()
                await asyncio.Event().wait()
                raise AssertionError("不可达")

            first = DurableSessionRecovery(
                first_store,
                claim_lease_seconds=0.2,
                renew_interval=0.05,
            )
            first_task = asyncio.create_task(
                first.resume(
                    session_id="session",
                    operation_id="operation",
                    callbacks=RecoveryCallbacks(
                        cancelled_model,
                        _unused_tool,
                        _unused_tool,
                    ),
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            first_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first_task

            after_cancel = await second_store.load(
                session_id="session",
                operation_id="operation",
            )
            cancelled_state = replay_operation(after_cancel)
            self.assertEqual(
                [
                    request.phase
                    for request in cancelled_state.model_requests.values()
                ],
                ["started"],
            )

            model_calls = 0

            async def recovered_model(_messages, policy):
                nonlocal model_calls
                model_calls += 1
                self.assertEqual(policy, ModelRequestPolicy.no_tools())
                return _assistant("重入完成")

            completed = await DurableSessionRecovery(
                second_store,
                claim_lease_seconds=0.2,
                renew_interval=0.05,
            ).resume(
                session_id="session",
                operation_id="operation",
                callbacks=RecoveryCallbacks(
                    recovered_model,
                    _unused_tool,
                    _unused_tool,
                ),
            )

            self.assertEqual(completed.status, "completed")
            self.assertEqual(model_calls, 1)
            events = await second_store.load(
                session_id="session",
                operation_id="operation",
            )
            state = replay_operation(events)
            self.assertEqual(state.phase, "completed")
            phases = [request.phase for request in state.model_requests.values()]
            self.assertEqual(phases.count("failed"), 1)
            self.assertEqual(phases.count("completed"), 1)
            self.assertNotIn("started", phases)

    def test_lease配置拒绝无效续租周期(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteOperationEventStore(Path(directory) / "recovery.db")
            with self.assertRaises(ValueError):
                DurableSessionRecovery(store, claim_lease_seconds=0)
            with self.assertRaises(ValueError):
                DurableSessionRecovery(
                    store,
                    claim_lease_seconds=1,
                    renew_interval=1,
                )

    async def test_tool_callback不能替换计划中的tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteOperationEventStore(
                Path(directory) / "recovery.db"
            )
            policy = ModelRequestPolicy(
                visible_tool_names=("divide",),
                tool_choice="required",
                allowed_tool_names=("divide",),
                expected_tool_arguments={"a": 10, "b": 2},
                continuation_policy=ModelRequestPolicy.no_tools(),
            )
            await store.append("operation_started", "session", "operation")
            await store.append(
                "model_policy_selected",
                "session",
                "operation",
                {"policy": policy.to_dict()},
            )
            await store.append(
                "model_request_started",
                "session",
                "operation",
                {"requestId": "r1", "requestPolicy": policy.to_dict()},
            )
            await store.append(
                "model_request_completed",
                "session",
                "operation",
                {
                    "requestId": "r1",
                    "message": {
                        "role": "assistant",
                        "content": [{
                            "type": "toolCall",
                            "id": "divide-call",
                            "name": "divide",
                            "arguments": {"a": 10, "b": 2},
                        }],
                        "stopReason": "toolUse",
                    },
                },
            )
            await store.append(
                "tool_intent_recorded",
                "session",
                "operation",
                {
                    "toolCallId": "divide-call",
                    "toolName": "divide",
                    "arguments": {"a": 10, "b": 2},
                    "replayPolicy": "safe",
                },
            )
            await store.append(
                "tool_dispatch_started",
                "session",
                "operation",
                {"toolCallId": "divide-call"},
            )

            async def malicious_result(_action):
                return {
                    "role": "toolResult",
                    "toolCallId": "other-call",
                    "toolName": "divide",
                    "content": [],
                    "details": {},
                    "isError": False,
                }

            with self.assertRaisesRegex(RuntimeError, "Tool Call ID"):
                await DurableSessionRecovery(store).resume(
                    session_id="session",
                    operation_id="operation",
                    callbacks=RecoveryCallbacks(
                        lambda *_args: _assistant("unused"),
                        malicious_result,
                        _unused_tool,
                    ),
                )
            state = replay_operation(await store.load())
            self.assertEqual(state.tools["divide-call"].phase, "dispatch_started")
            self.assertFalse(
                any(message.get("role") == "toolResult" for message in state.messages)
            )


if __name__ == "__main__":
    unittest.main()
