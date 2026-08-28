"""Model Policy、Reducer 与 Recovery 的 fail-closed 回归测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    DurableSessionRecovery,
    InMemoryOperationEventStore,
    ModelRequestPolicy,
    RecoveryCallbacks,
    ToolChoicePolicy,
    replay_operation,
)
from pi_agent_loop.model_policy import (  # noqa: E402
    ModelRequestPolicyError,
    capture_model_request_policy,
    validate_model_response_policy,
)
from pi_agent_loop.routing.capabilities import CapabilityRegistry  # noqa: E402
from pi_agent_loop.routing.guard import RequiredToolCallGuard  # noqa: E402
from pi_agent_loop.session.operation_events import OperationEvent  # noqa: E402
from pi_agent_loop.session.operation_state import (  # noqa: E402
    OperationLogInvariantError,
)


def assistant(
    *,
    stop_reason: str = "stop",
    content: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content or [],
        "stopReason": stop_reason,
    }


def tool_call(
    call_id: str,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "toolCall",
        "id": call_id,
        "name": name,
        "arguments": arguments,
    }


def request_events(
    policy: ModelRequestPolicy,
    message: dict[str, Any],
) -> list[OperationEvent]:
    return [
        OperationEvent("operation_started", "s", "o", 0),
        OperationEvent(
            "model_request_started",
            "s",
            "o",
            1,
            data={
                "requestId": "r1",
                "requestPolicy": policy.to_dict(),
            },
        ),
        OperationEvent(
            "model_request_completed",
            "s",
            "o",
            2,
            data={"requestId": "r1", "message": message},
        ),
    ]


class RecoveryPolicyBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def required_policy(
        self,
        expected: dict[str, Any] | None,
    ) -> ModelRequestPolicy:
        return ModelRequestPolicy(
            visible_tool_names=("refund",),
            tool_choice="required",
            allowed_tool_names=("refund",),
            expected_tool_arguments=expected,
        )

    def test_expected_arguments未指定与明确空对象可持久化区分(self) -> None:
        unspecified = ModelRequestPolicy(
            visible_tool_names=("refund",),
            tool_choice="auto",
        )
        explicit_empty = self.required_policy({})

        self.assertIsNone(
            ModelRequestPolicy.from_dict(
                unspecified.to_dict()
            ).expected_tool_arguments
        )
        self.assertEqual(
            ModelRequestPolicy.from_dict(
                explicit_empty.to_dict()
            ).expected_tool_arguments,
            {},
        )
        self.assertIsNone(
            capture_model_request_policy(["refund"], {}).expected_tool_arguments
        )
        self.assertEqual(
            capture_model_request_policy(
                ["refund"],
                {"expected_tool_arguments": {}},
            ).expected_tool_arguments,
            {},
        )

    def test明确空参数要求唯一调用且参数精确为空(self) -> None:
        policy = self.required_policy({})
        validate_model_response_policy(
            assistant(
                stop_reason="toolUse",
                content=[tool_call("ok", "refund", {})],
            ),
            policy,
        )

        invalid_messages = (
            assistant(
                stop_reason="toolUse",
                content=[tool_call("extra", "refund", {"force": True})],
            ),
            assistant(
                stop_reason="toolUse",
                content=[
                    tool_call("one", "refund", {}),
                    tool_call("two", "refund", {}),
                ],
            ),
        )
        for message in invalid_messages:
            with self.subTest(message=message):
                with self.assertRaises(ModelRequestPolicyError):
                    validate_model_response_policy(message, policy)

    def test_expected_arguments递归严格区分JSON类型(self) -> None:
        expected = {
            "order": {
                "paid": 1,
                "flags": [True, {"count": 2}],
            }
        }
        policy = self.required_policy(expected)
        validate_model_response_policy(
            assistant(
                stop_reason="toolUse",
                content=[tool_call("ok", "refund", expected)],
            ),
            policy,
        )

        wrong_values = (
            {
                "order": {
                    "paid": True,
                    "flags": [True, {"count": 2}],
                }
            },
            {
                "order": {
                    "paid": 1,
                    "flags": [True, {"count": 2.0}],
                }
            },
        )
        for arguments in wrong_values:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ModelRequestPolicyError):
                    validate_model_response_policy(
                        assistant(
                            stop_reason="toolUse",
                            content=[tool_call("bad", "refund", arguments)],
                        ),
                        policy,
                    )

    def test_guard与policy使用相同的空参数和严格类型语义(self) -> None:
        guard = RequiredToolCallGuard(CapabilityRegistry())
        empty_violation = guard.validate(
            assistant(
                stop_reason="toolUse",
                content=[tool_call("bad", "refund", {"force": True})],
            ),
            policy=ToolChoicePolicy("required"),
            allowed_tool_names=("refund",),
            expected_arguments={},
        )
        typed_violation = guard.validate(
            assistant(
                stop_reason="toolUse",
                content=[tool_call("bad", "refund", {"amount": True})],
            ),
            policy=ToolChoicePolicy("required"),
            allowed_tool_names=("refund",),
            expected_arguments={"amount": 1},
        )

        self.assertEqual(
            empty_violation.code if empty_violation else None,
            "required_tool_arguments_mismatch",
        )
        self.assertEqual(
            typed_violation.code if typed_violation else None,
            "required_tool_arguments_mismatch",
        )

    def test_reducer拒绝错误工具参数额外调用和length响应(self) -> None:
        policy = self.required_policy({"order_id": "1001"})
        invalid_messages = {
            "wrong_tool": assistant(
                stop_reason="toolUse",
                content=[
                    tool_call("bad", "delete_order", {"order_id": "1001"})
                ],
            ),
            "wrong_arguments": assistant(
                stop_reason="toolUse",
                content=[tool_call("bad", "refund", {"order_id": "9999"})],
            ),
            "extra_call": assistant(
                stop_reason="toolUse",
                content=[
                    tool_call("one", "refund", {"order_id": "1001"}),
                    tool_call("two", "refund", {"order_id": "1001"}),
                ],
            ),
            "length": assistant(
                stop_reason="length",
                content=[{"type": "text", "text": "truncated"}],
            ),
            "tooluse_without_call": assistant(
                stop_reason="toolUse",
                content=[{"type": "text", "text": "missing call"}],
            ),
            "stop_with_call": assistant(
                stop_reason="stop",
                content=[
                    tool_call("bad", "refund", {"order_id": "1001"})
                ],
            ),
        }

        for label, message in invalid_messages.items():
            with self.subTest(label=label):
                with self.assertRaises(OperationLogInvariantError):
                    replay_operation(request_events(policy, message))

    def test_reducer按request策略而不是后续active策略校验(self) -> None:
        original = self.required_policy({"order_id": "1001"})
        relaxed = ModelRequestPolicy(
            visible_tool_names=("refund",),
            tool_choice="auto",
        )
        events = request_events(
            original,
            assistant(
                stop_reason="toolUse",
                content=[tool_call("bad", "refund", {"order_id": "9999"})],
            ),
        )
        events.insert(
            2,
            OperationEvent(
                "model_policy_selected",
                "s",
                "o",
                2,
                data={"policy": relaxed.to_dict()},
            ),
        )
        events[-1] = OperationEvent(
            events[-1].type,
            "s",
            "o",
            3,
            data=events[-1].data,
        )

        with self.assertRaises(OperationLogInvariantError):
            replay_operation(events)

    async def test自定义恢复callback返回length不会写completed或结束operation(
        self,
    ) -> None:
        store = InMemoryOperationEventStore()
        policy = ModelRequestPolicy.no_tools()
        await store.append("operation_started", "s", "o")
        await store.append(
            "message_appended",
            "s",
            "o",
            {
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "continue"}],
                }
            },
        )
        await store.append(
            "model_policy_selected",
            "s",
            "o",
            {"policy": policy.to_dict()},
        )

        async def request_model(_messages, _policy):
            return assistant(
                stop_reason="length",
                content=[{"type": "text", "text": "truncated"}],
            )

        async def unused(_action):
            raise AssertionError("不应执行工具")

        callbacks = RecoveryCallbacks(request_model, unused, unused)
        with self.assertRaisesRegex(
            ModelRequestPolicyError,
            "长度上限",
        ):
            await DurableSessionRecovery(store).resume(
                session_id="s",
                operation_id="o",
                callbacks=callbacks,
            )

        events = await store.load(session_id="s", operation_id="o")
        event_types = [event.type for event in events]
        self.assertIn("model_request_failed", event_types)
        self.assertNotIn("model_request_completed", event_types)
        self.assertNotIn("operation_finished", event_types)
        self.assertEqual(replay_operation(events).phase, "running")

    def test失败模型响应恢复时只能人工处理且不能completed(self) -> None:
        policy = ModelRequestPolicy.no_tools()
        events = request_events(
            policy,
            assistant(
                stop_reason="error",
                content=[],
            ),
        )
        operation = replay_operation(events)
        plan = DurableSessionRecovery(
            InMemoryOperationEventStore()
        ).planner.plan(operation)

        self.assertEqual(plan.actions[0].kind, "manual_intervention")
        completed = [
            *events,
            OperationEvent(
                "operation_finished",
                "s",
                "o",
                3,
                data={"outcome": "completed"},
            ),
        ]
        with self.assertRaises(OperationLogInvariantError):
            replay_operation(completed)


if __name__ == "__main__":
    unittest.main()
