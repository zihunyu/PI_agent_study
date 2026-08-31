"""Multi Intent Planner、DAG、审批、失败和恢复测试。"""

from __future__ import annotations

import asyncio
import unittest

from pi_agent_loop import (
    CancellationToken,
    OperationCancelledError,
    RequestDecision,
    TaskDecision,
    TaskDependencyHint,
    VerifiedIdentity,
)
from pi_agent_loop.planning import (
    ApprovalBarrier,
    DependencyGraph,
    HybridRequestPlanner,
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanApprovalDecision,
    PlanApprovalReceipt,
    PlanEvent,
    PlanExecutionState,
    PlanExecutor,
    PlanParameterContract,
    PlanStep,
    PlanStepState,
    PlanValidationError,
    PlanValidator,
    TaskStateMachine,
)


NOW = 1_000.0
EMPTY_WRITE_CONTRACT = PlanParameterContract(allow_empty=True)
APPROVER = VerifiedIdentity(
    principal_id="approver-1",
    roles=frozenset({"plan.approver"}),
    issuer="test-identity",
    verification_id="identity-verification-1",
)


def approval_receipt(step, state, *, approver=APPROVER, receipt_id="receipt-1"):
    return PlanApprovalReceipt.issue(
        approval_id=f"approval-{receipt_id}",
        receipt_id=receipt_id,
        plan_id=state.plan_id,
        step=step,
        state_version=state.version,
        approver=approver,
        verification_id=f"proof-{receipt_id}",
        issued_at=NOW - 10,
        expires_at=NOW + 10,
    )


class OneTimeReceiptConsumer:
    def __init__(self) -> None:
        self.consumed: set[str] = set()

    def __call__(self, receipt, _step, _state, _cancellation):
        if receipt.receipt_id in self.consumed:
            raise RuntimeError("receipt already consumed")
        self.consumed.add(receipt.receipt_id)
        return receipt.consumed(NOW)


class P2MultiIntentPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_never步骤handler异常按结果未知进入人工而非普通失败(self) -> None:
        policy = IntentPlanPolicy(
            "resource.write",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("plan.approver",),
            parameter_contract=EMPTY_WRITE_CONTRACT,
        )
        plan = MultiIntentPlan(
            "更新资源",
            (
                PlanStep(
                    "write",
                    policy.intent,
                    requires_approval=True,
                    write=True,
                    replay_policy="never",
                    approval_roles=("plan.approver",),
                    parameter_contract=EMPTY_WRITE_CONTRACT,
                ),
            ),
            plan_id="write-outcome-unknown",
        )

        async def authorize(step, state, _token):
            return PlanApprovalDecision.grant(
                step,
                approval_receipt(step, state),
            )

        async def execute(_step, _token):
            raise TimeoutError("外部结果无法确认")

        result = await PlanExecutor(
            plan,
            {policy.intent: policy},
            execute,
            approval_barrier=ApprovalBarrier(
                authorize,
                receipt_consumer=OneTimeReceiptConsumer(),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        ).execute()

        self.assertEqual(result.state.phase, "manual_intervention")
        self.assertEqual(
            result.state.steps["write"].status,
            "manual_intervention",
        )
        self.assertEqual(result.events[-1].type, "step_outcome_unknown")

    def safe_policy(self, intent):
        return IntentPlanPolicy(intent)

    async def test_task_decision映射参数依赖且不再调用自由规划器(self) -> None:
        source_arguments = {
            "record_id": "R-1",
            "filters": {"active": True},
        }
        read = RequestDecision(
            status="in_scope_tool_ready",
            reason="读取主记录",
            message="读取",
            intent="record.read",
            extracted_fields=source_arguments,
            required_capabilities=("records.read",),
            task_id="read_record",
        )
        details = RequestDecision(
            status="in_scope_tool_ready",
            reason="读取依赖明细",
            message="读取明细",
            intent="detail.read",
            extracted_fields={"limit": 20},
            required_capabilities=("details.read",),
            task_id="read_details",
            depends_on=("read_record",),
        )
        task_decision = TaskDecision(
            components=(read, details),
            dependencies=(
                TaskDependencyHint("read_details", ("read_record",)),
            ),
        )
        planner_calls = 0

        async def planner_fn(_request, _catalog):
            nonlocal planner_calls
            planner_calls += 1
            raise AssertionError("TaskDecision 路径不应再次调用自由规划器")

        planner = HybridRequestPlanner(
            {
                "record.read": IntentPlanPolicy(
                    "record.read",
                    capabilities=("records.read",),
                ),
                "detail.read": IntentPlanPolicy(
                    "detail.read",
                    capabilities=("details.read",),
                ),
            },
            planner_fn,
        )

        plan = await planner.plan(
            "先读取主记录，再读取明细",
            task_decision=task_decision,
        )

        self.assertEqual(planner_calls, 0)
        self.assertEqual(
            DependencyGraph(plan).topological_order,
            ("read_record", "read_details"),
        )
        self.assertEqual(
            plan.step("read_record").arguments,
            {"record_id": "R-1", "filters": {"active": True}},
        )
        self.assertEqual(
            plan.step("read_details").depends_on,
            ("read_record",),
        )
        self.assertEqual(plan.step("read_details").arguments, {"limit": 20})
        source_arguments["filters"]["active"] = False
        self.assertEqual(
            plan.step("read_record").arguments["filters"],
            {"active": True},
        )

    def test_task_decision不能覆盖可信写审批重放和能力策略(self) -> None:
        policy = IntentPlanPolicy(
            "order.cancel",
            requires_approval=True,
            write=True,
            replay_policy="never",
            capabilities=("orders.cancel.trusted",),
            approval_roles=("order.manager",),
        )
        # Router 故意把写操作伪装为无需审批的安全读取，并声明错误能力。
        untrusted = RequestDecision(
            status="in_scope_tool_ready",
            reason="模型声称这是安全读取",
            message="安全",
            intent="order.cancel",
            extracted_fields={"order_id": "1001"},
            required_capabilities=("orders.read.untrusted",),
            requires_approval=False,
            side_effect=False,
            risk="low",
            task_id="cancel",
        )
        companion = RequestDecision(
            status="in_scope_no_tool",
            reason="生成说明",
            message="说明",
            intent="response.explain",
            task_id="explain",
            depends_on=("cancel",),
        )
        task_decision = TaskDecision(
            components=(untrusted, companion),
            dependencies=(TaskDependencyHint("explain", ("cancel",)),),
        )
        planner = HybridRequestPlanner(
            {
                "order.cancel": policy,
                "response.explain": IntentPlanPolicy("response.explain"),
            },
            lambda *_args: None,
        )

        plan = planner.plan_from_task_decision("取消并解释", task_decision)

        step = plan.step("cancel")
        self.assertTrue(step.write)
        self.assertTrue(step.requires_approval)
        self.assertEqual(step.replay_policy, "never")
        self.assertEqual(step.capabilities, ("orders.cancel.trusted",))
        self.assertEqual(step.approval_roles, ("order.manager",))
        self.assertEqual(step.arguments, {"order_id": "1001"})

    def test_task_decision缺参数标记时拒绝(self) -> None:
        incomplete = RequestDecision(
            status="in_scope_need_clarification",
            reason="缺少订单号",
            message="请提供订单号",
            intent="order.read",
            missing_fields=("order_id",),
            required_capabilities=("orders.read",),
            task_id="read",
        )
        companion = RequestDecision(
            status="in_scope_no_tool",
            reason="解释",
            message="解释",
            intent="response.explain",
            task_id="explain",
        )
        task_decision = TaskDecision(
            components=(incomplete, companion),
            clarification_tasks=("read",),
        )
        planner = HybridRequestPlanner(
            {
                "order.read": IntentPlanPolicy(
                    "order.read",
                    capabilities=("orders.read",),
                ),
                "response.explain": IntentPlanPolicy("response.explain"),
            },
            lambda *_args: None,
        )

        with self.assertRaisesRegex(PlanValidationError, "缺失参数或能力"):
            planner.plan_from_task_decision("查询并解释", task_decision)

    def test_task_decision包含未知intent时拒绝(self) -> None:
        unknown = RequestDecision(
            status="in_scope_tool_ready",
            reason="未知业务",
            message="未知",
            intent="unknown.execute",
            task_id="unknown",
        )
        known = RequestDecision(
            status="in_scope_no_tool",
            reason="已知说明",
            message="说明",
            intent="response.explain",
            task_id="explain",
        )
        task_decision = TaskDecision(components=(unknown, known))
        planner = HybridRequestPlanner(
            {"response.explain": IntentPlanPolicy("response.explain")},
            lambda *_args: None,
        )

        with self.assertRaisesRegex(PlanValidationError, "未配置 Intent"):
            planner.plan_from_task_decision("执行未知任务", task_decision)

    async def test_hybrid_planner生成多intent并拒绝策略降级和环(self) -> None:
        policies = {
            "order.read": IntentPlanPolicy("order.read", capabilities=("orders.read",)),
            "order.cancel": IntentPlanPolicy(
                "order.cancel",
                requires_approval=True,
                write=True,
                replay_policy="never",
                capabilities=("orders.cancel",),
                approval_roles=("plan.approver",),
            ),
            "message.send": IntentPlanPolicy("message.send", capabilities=("messages.send",)),
        }

        async def planner(_request, catalog):
            self.assertEqual(len(catalog), 3)
            return {
                "planId": "plan-1",
                "steps": [
                    {"stepId": "read", "intent": "order.read", "arguments": {"id": "1"}},
                    {
                        "stepId": "cancel",
                        "intent": "order.cancel",
                        "arguments": {"id": "2"},
                        "dependsOn": ["read"],
                    },
                    {
                        "stepId": "send",
                        "intent": "message.send",
                        "dependsOn": ["cancel"],
                    },
                ],
            }

        plan = await HybridRequestPlanner(policies, planner).plan("查询、取消并发送")
        self.assertEqual(
            DependencyGraph(plan).topological_order, ("read", "cancel", "send")
        )
        self.assertTrue(plan.step("cancel").requires_approval)
        self.assertEqual(plan.step("cancel").replay_policy, "never")

        async def downgrade(_request, _catalog):
            return {
                "steps": [
                    {
                        "stepId": "cancel",
                        "intent": "order.cancel",
                        "requiresApproval": False,
                    }
                ]
            }

        with self.assertRaisesRegex(PlanValidationError, "覆盖可信策略"):
            await HybridRequestPlanner(policies, downgrade).plan("取消")

        cyclic = MultiIntentPlan(
            "cycle",
            (
                PlanStep("a", "order.read", depends_on=("b",), capabilities=("orders.read",)),
                PlanStep("b", "order.read", depends_on=("a",), capabilities=("orders.read",)),
            ),
        )
        with self.assertRaisesRegex(PlanValidationError, "存在环"):
            PlanValidator(policies).validate(cyclic)

    async def test_replanner接收失败事实并仍受可信策略约束(self) -> None:
        seen: dict[str, object] = {}

        def replan(request, previous, result, issues, catalog):
            seen.update(
                request=request,
                previous=previous,
                result=result,
                issues=issues,
                catalog=catalog,
            )
            return {
                "planId": "corrected-plan",
                "steps": [
                    {
                        "stepId": "retry-read",
                        "intent": "read",
                        "arguments": {"source": "replica"},
                    }
                ],
            }

        planner = HybridRequestPlanner(
            {"read": IntentPlanPolicy("read", capabilities=("records.read",))},
            lambda _request, _catalog: {
                "planId": "initial-plan",
                "steps": [{"stepId": "read", "intent": "read"}],
            },
            replanner_fn=replan,
        )
        original = await planner.plan("读取记录")
        corrected = await planner.replan(
            "读取记录",
            original,
            {"status": "failed"},
            ("primary unavailable",),
        )

        self.assertIsNotNone(corrected)
        assert corrected is not None
        self.assertEqual(corrected.plan_id, "corrected-plan")
        self.assertEqual(corrected.steps[0].capabilities, ("records.read",))
        self.assertEqual(seen["issues"], ("primary unavailable",))

        planner.replanner_fn = lambda *_args: {
            "planId": "unsafe-correction",
            "steps": [
                {
                    "stepId": "read",
                    "intent": "read",
                    "write": True,
                }
            ],
        }
        with self.assertRaisesRegex(PlanValidationError, "可信策略字段"):
            await planner.replan(
                "读取记录",
                original,
                {"status": "failed"},
                ("still unavailable",),
            )

    async def test_dag并发执行且依赖步骤等待(self) -> None:
        policies = {name: self.safe_policy(name) for name in ("a", "b", "c")}
        plan = MultiIntentPlan(
            "parallel then join",
            (
                PlanStep("a", "a"),
                PlanStep("b", "b"),
                PlanStep("c", "c", depends_on=("a", "b")),
            ),
            plan_id="parallel-plan",
        )
        active = 0
        max_active = 0
        timeline = []

        async def execute(step, _token):
            nonlocal active, max_active
            timeline.append(f"start:{step.step_id}")
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.015 if step.step_id != "c" else 0)
            active -= 1
            timeline.append(f"end:{step.step_id}")
            return {"step": step.step_id}

        result = await PlanExecutor(
            plan, policies, execute, max_parallel_steps=2
        ).execute()

        self.assertEqual(result.state.phase, "completed")
        self.assertEqual(max_active, 2)
        self.assertGreater(timeline.index("start:c"), timeline.index("end:a"))
        self.assertGreater(timeline.index("start:c"), timeline.index("end:b"))
        self.assertEqual(
            [step_id for step_id, _ in result.synthesized.ordered_results],
            ["a", "b", "c"],
        )

    async def test_approval_barrier绑定action_hash后推进依赖(self) -> None:
        policies = {
            "read": IntentPlanPolicy("read"),
            "cancel": IntentPlanPolicy(
                "cancel",
                requires_approval=True,
                write=True,
                replay_policy="never",
                approval_roles=("plan.approver",),
                parameter_contract=EMPTY_WRITE_CONTRACT,
            ),
            "notify": IntentPlanPolicy("notify"),
        }
        plan = MultiIntentPlan(
            "read cancel notify",
            (
                PlanStep("read", "read"),
                PlanStep(
                    "cancel",
                    "cancel",
                    depends_on=("read",),
                    requires_approval=True,
                    write=True,
                    replay_policy="never",
                    approval_roles=("plan.approver",),
                    parameter_contract=EMPTY_WRITE_CONTRACT,
                ),
                PlanStep("notify", "notify", depends_on=("cancel",)),
            ),
        )
        approvals = []
        executed = []

        async def authorize(step, state, _token):
            approvals.append(step.step_id)
            return PlanApprovalDecision.grant(step, approval_receipt(step, state))

        async def execute(step, _token):
            executed.append(step.step_id)
            return {"ok": step.step_id}

        result = await PlanExecutor(
            plan,
            policies,
            execute,
            approval_barrier=ApprovalBarrier(
                authorize,
                receipt_consumer=OneTimeReceiptConsumer(),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        ).execute()

        self.assertEqual(result.state.phase, "completed")
        self.assertEqual(approvals, ["cancel"])
        self.assertEqual(executed, ["read", "cancel", "notify"])
        self.assertEqual(
            result.state.steps["cancel"].approval_id,
            "approval-receipt-1",
        )
        event_types = [event.type for event in result.events]
        self.assertLess(
            event_types.index("step_approval_granted"),
            event_types.index("step_started", event_types.index("step_approval_granted")),
        )

    async def test_action_hash同时绑定业务参数和可信策略(self) -> None:
        original = PlanStep(
            "cancel",
            "cancel",
            arguments={"orderId": "1001"},
            requires_approval=True,
            write=True,
            replay_policy="never",
            capabilities=("orders.cancel",),
            approval_roles=("plan.approver",),
        )
        changed_capability = PlanStep(
            "cancel",
            "cancel",
            arguments={"orderId": "1001"},
            requires_approval=True,
            write=True,
            replay_policy="never",
            capabilities=("orders.admin",),
            approval_roles=("plan.approver",),
        )
        changed_argument = PlanStep(
            "cancel",
            "cancel",
            arguments={"orderId": "1002"},
            requires_approval=True,
            write=True,
            replay_policy="never",
            capabilities=("orders.cancel",),
            approval_roles=("plan.approver",),
        )
        self.assertNotEqual(original.action_hash, changed_capability.action_hash)
        self.assertNotEqual(original.action_hash, changed_argument.action_hash)
        persisted = MultiIntentPlan(
            "cancel", (original,), plan_id="persisted-plan"
        ).to_dict()
        persisted["steps"][0]["actionHash"] = "tampered"
        with self.assertRaisesRegex(PlanValidationError, "Action Hash"):
            MultiIntentPlan.from_dict(persisted)

    async def test_失败仅跳过依赖分支并保留独立结果(self) -> None:
        policies = {name: self.safe_policy(name) for name in ("a", "b", "c")}
        plan = MultiIntentPlan(
            "partial failure",
            (
                PlanStep("a", "a"),
                PlanStep("b", "b"),
                PlanStep("c", "c", depends_on=("a",)),
            ),
        )

        async def execute(step, _token):
            if step.step_id == "a":
                raise RuntimeError("A failed")
            return {"ok": step.step_id}

        result = await PlanExecutor(plan, policies, execute).execute()
        self.assertEqual(result.state.phase, "failed")
        self.assertEqual(result.state.steps["a"].status, "failed")
        self.assertEqual(result.state.steps["b"].status, "succeeded")
        self.assertEqual(result.state.steps["c"].status, "skipped")
        self.assertEqual(result.synthesized.ordered_results, (("b", {"ok": "b"}),))

    async def test_recovery只重放safe且never进入人工介入(self) -> None:
        safe_policies = {"read": IntentPlanPolicy("read")}
        safe_plan = MultiIntentPlan(
            "recover read", (PlanStep("read", "read"),), plan_id="safe-plan"
        )
        safe_machine = TaskStateMachine(safe_plan)
        interrupted = safe_machine.apply(
            safe_machine.initial_state(), PlanEvent(1, "step_started", "read", {})
        )
        calls = 0

        async def execute(_step, _token):
            nonlocal calls
            calls += 1
            return {"recovered": True}

        safe_result = await PlanExecutor(
            safe_plan, safe_policies, execute
        ).execute(initial_state=interrupted)
        self.assertEqual(calls, 1)
        self.assertEqual(safe_result.state.steps["read"].attempts, 2)
        self.assertEqual(safe_result.events[0].type, "step_recovered")
        restored = safe_machine.state_from_dict(safe_result.state.to_dict())
        self.assertEqual(restored, safe_result.state)

        never_policy = IntentPlanPolicy(
            "cancel",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("plan.approver",),
            parameter_contract=EMPTY_WRITE_CONTRACT,
        )
        never_plan = MultiIntentPlan(
            "recover write",
            (
                PlanStep(
                    "cancel",
                    "cancel",
                    requires_approval=True,
                    write=True,
                    replay_policy="never",
                    approval_roles=("plan.approver",),
                    parameter_contract=EMPTY_WRITE_CONTRACT,
                ),
            ),
            plan_id="never-plan",
        )
        never_machine = TaskStateMachine(never_plan)
        state = never_machine.initial_state()
        step = never_plan.step("cancel")
        state = never_machine.apply(
            state,
            PlanEvent(1, "step_waiting_approval", "cancel", {"actionHash": step.action_hash}),
        )
        receipt = approval_receipt(step, state, receipt_id="recovery").consumed(NOW)
        state = never_machine.apply(
            state,
            PlanEvent(
                2,
                "step_approval_granted",
                "cancel",
                {
                    "approvalId": receipt.approval_id,
                    "actionHash": step.action_hash,
                    "receipt": receipt.to_dict(),
                },
            ),
        )
        state = never_machine.apply(
            state,
            PlanEvent(
                3,
                "step_started",
                "cancel",
                {"approvalReceiptId": receipt.receipt_id, "startedAt": NOW},
            ),
        )
        never_calls = 0

        async def should_not_execute(_step, _token):
            nonlocal never_calls
            never_calls += 1

        never_result = await PlanExecutor(
            never_plan, {"cancel": never_policy}, should_not_execute
        ).execute(initial_state=state)
        self.assertEqual(never_calls, 0)
        self.assertEqual(never_result.state.phase, "manual_intervention")
        self.assertEqual(
            never_result.state.steps["cancel"].status, "manual_intervention"
        )

    async def test_waiting_approval快照恢复后重新进入barrier(self) -> None:
        policy = IntentPlanPolicy(
            "cancel",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("plan.approver",),
            parameter_contract=EMPTY_WRITE_CONTRACT,
        )
        plan = MultiIntentPlan(
            "resume approval",
            (
                PlanStep(
                    "cancel",
                    "cancel",
                    requires_approval=True,
                    write=True,
                    replay_policy="never",
                    approval_roles=("plan.approver",),
                    parameter_contract=EMPTY_WRITE_CONTRACT,
                ),
            ),
        )
        machine = TaskStateMachine(plan)
        step = plan.step("cancel")
        waiting = machine.apply(
            machine.initial_state(),
            PlanEvent(
                1,
                "step_waiting_approval",
                "cancel",
                {"actionHash": step.action_hash},
            ),
        )
        approvals = 0

        async def authorize(current, state, _token):
            nonlocal approvals
            approvals += 1
            return PlanApprovalDecision.grant(
                current,
                approval_receipt(current, state, receipt_id="resumed"),
            )

        async def execute(_step, _token):
            return {"cancelled": True}

        result = await PlanExecutor(
            plan,
            {"cancel": policy},
            execute,
            approval_barrier=ApprovalBarrier(
                authorize,
                receipt_consumer=OneTimeReceiptConsumer(),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        ).execute(initial_state=waiting)
        self.assertEqual(approvals, 1)
        self.assertEqual(result.state.phase, "completed")

    async def test_恢复拒绝绕过依赖的伪造snapshot(self) -> None:
        plan = MultiIntentPlan(
            "dependency",
            (
                PlanStep("read", "read"),
                PlanStep("notify", "notify", depends_on=("read",)),
            ),
            plan_id="forged-snapshot",
        )
        forged = PlanExecutionState(
            plan.plan_id,
            {
                "read": PlanStepState(status="pending"),
                "notify": PlanStepState(
                    status="succeeded", attempts=1, result={"sent": True}
                ),
            },
            version=1,
        )
        with self.assertRaisesRegex(PlanValidationError, "绕过了未完成依赖"):
            TaskStateMachine(plan).state_from_dict(forged.to_dict())

    async def test_cancellation主动中断忽略token的异步step(self) -> None:
        plan = MultiIntentPlan("cancel", (PlanStep("read", "read"),))
        token = CancellationToken()
        started = asyncio.Event()
        never = asyncio.Event()

        async def ignores_token(_step, _token):
            started.set()
            await never.wait()

        executor = PlanExecutor(
            plan, {"read": IntentPlanPolicy("read")}, ignores_token
        )
        task = asyncio.create_task(executor.execute(cancellation=token))
        await asyncio.wait_for(started.wait(), timeout=2)
        token.cancel("stop plan")
        with self.assertRaisesRegex(OperationCancelledError, "stop plan"):
            await asyncio.wait_for(task, timeout=2)
        self.assertEqual(executor.state.steps["read"].status, "running")

    async def test_handler成功后成功事件落盘失败不能伪造成step失败(self) -> None:
        plan = MultiIntentPlan("persist boundary", (PlanStep("read", "read"),))
        effects: list[str] = []
        persisted: list[str] = []

        async def execute(_step, _token):
            effects.append("external_effect_completed")
            return {"ok": True}

        async def persist(event):
            if event.type == "step_succeeded":
                raise OSError("temporary journal failure")
            persisted.append(event.type)

        executor = PlanExecutor(
            plan,
            {"read": IntentPlanPolicy("read")},
            execute,
            event_sink=persist,
        )

        with self.assertRaisesRegex(OSError, "temporary journal failure"):
            await executor.execute()

        self.assertEqual(effects, ["external_effect_completed"])
        self.assertEqual(persisted, ["step_started"])
        self.assertEqual(executor.state.steps["read"].status, "running")


if __name__ == "__main__":
    unittest.main()
