"""Autonomous complex-task planning and correction integration tests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from pi_agent_loop.harness.autonomous import AutonomousPlanRunner
from pi_agent_loop.planning import (
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanExecutionResult,
    PlanExecutionState,
    PlanStep,
    PlanStepState,
    ResultValidation,
    SynthesizedPlanResult,
)


class _Store:
    def __init__(self) -> None:
        self.plans: dict[str, MultiIntentPlan] = {}

    async def initialize(self, plan: MultiIntentPlan) -> None:
        self.plans[plan.plan_id] = plan

    async def load(self, plan_id: str):
        return SimpleNamespace(plan=self.plans[plan_id])


class _Workflow:
    supports_resource_admission = True

    def __init__(self, initial, policies, outcomes, *, planner=None) -> None:
        self.initial = initial
        self.policies = policies
        self.outcomes = outcomes
        self.planner = planner
        self.store = _Store()
        self.executed: list[str] = []

    async def plan(self, request):
        self.assert_request = request
        await self.store.initialize(self.initial)
        return self.initial

    async def execute(
        self,
        plan_id,
        *,
        cancellation=None,
        resource_reserver=None,
    ):
        if cancellation is not None:
            cancellation.throw_if_cancelled()
        if resource_reserver is not None:
            await resource_reserver(
                self.store.plans[plan_id].steps[0],
                1,
                None,
            )
        self.executed.append(plan_id)
        return self.outcomes[plan_id]


def _result(
    plan: MultiIntentPlan,
    status: str,
    *,
    value=None,
    error: str | None = None,
    approval_id: str | None = None,
) -> PlanExecutionResult:
    step = plan.steps[0]
    state = PlanExecutionState(
        plan.plan_id,
        {
            step.step_id: PlanStepState(
                status=status,  # type: ignore[arg-type]
                attempts=0 if status == "waiting_approval" else 1,
                approval_id=approval_id,
                result=value,
                error=error,
            )
        },
        version=1,
    )
    return PlanExecutionResult(
        state,
        SynthesizedPlanResult(
            plan.plan_id,
            state.phase,
            ((step.step_id, value),) if status == "succeeded" else (),
            ((step.step_id, error or "failed"),) if status == "failed" else (),
            (step.step_id,) if status == "manual_intervention" else (),
        ),
        (),
    )


def _trusted_fixture_validator(observation, _token) -> ResultValidation:
    """The fake workflow's result contract is the semantic oracle in these tests."""

    latest = observation.latest_execution
    assert latest is not None
    if latest.state.phase == "completed":
        return ResultValidation.valid()
    issues = tuple(error for _step_id, error in latest.synthesized.failures)
    return ResultValidation.invalid(*(issues or ("fixture plan did not complete",)))


class AutonomousPlanRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume继续使用同一闭环完成等待中的计划(self) -> None:
        policy = IntentPlanPolicy("read")
        plan = MultiIntentPlan(
            "读取异步结果",
            (PlanStep("read", "read"),),
            plan_id="resume-plan",
        )
        workflow = _Workflow(
            plan,
            {"read": policy},
            {"resume-plan": _result(plan, "waiting_approval", approval_id="a-1")},
        )
        runner = AutonomousPlanRunner(  # type: ignore[arg-type]
            workflow,
            result_validator=_trusted_fixture_validator,
        )

        waiting = await runner.run(plan.request)
        workflow.outcomes[plan.plan_id] = _result(
            plan,
            "succeeded",
            value={"ready": True},
        )
        completed = await runner.resume(plan.plan_id)

        self.assertEqual(waiting.status, "waiting_approval")
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.plan_id, plan.plan_id)
        self.assertEqual(workflow.executed, [plan.plan_id, plan.plan_id])

    async def test_safe_plan失败后重规划并重新验证(self) -> None:
        policy = IntentPlanPolicy("read", capabilities=("records.read",))
        initial = MultiIntentPlan(
            "读取记录",
            (PlanStep("primary", "read", capabilities=policy.capabilities),),
            plan_id="initial",
        )
        corrected = MultiIntentPlan(
            "读取记录",
            (
                PlanStep(
                    "replica",
                    "read",
                    arguments={"source": "replica"},
                    capabilities=policy.capabilities,
                ),
            ),
            plan_id="corrected",
        )
        workflow = _Workflow(
            initial,
            {"read": policy},
            {
                "initial": _result(initial, "failed", error="primary unavailable"),
                "corrected": _result(
                    corrected,
                    "succeeded",
                    value={"records": [1, 2]},
                ),
            },
        )
        replan_calls = 0

        def replan(observation, validation, _token):
            nonlocal replan_calls
            replan_calls += 1
            self.assertEqual(observation.latest_plan.plan_id, "initial")
            self.assertIn("primary unavailable", validation.issues)
            return corrected

        result = await AutonomousPlanRunner(
            workflow,  # type: ignore[arg-type]
            result_validator=_trusted_fixture_validator,
            replanner=replan,
        ).run("读取记录")

        self.assertEqual(result.status, "completed")
        self.assertEqual(workflow.executed, ["initial", "corrected"])
        self.assertEqual(replan_calls, 1)
        self.assertEqual(result.closed_loop.correction_rounds, 1)
        self.assertIn("records", result.response_text)

    async def test业务校验失败触发受控纠正(self) -> None:
        policy = IntentPlanPolicy("read")
        initial = MultiIntentPlan(
            "读取健康副本",
            (PlanStep("primary", "read"),),
            plan_id="bad-data",
        )
        corrected = MultiIntentPlan(
            "读取健康副本",
            (PlanStep("replica", "read"),),
            plan_id="good-data",
        )
        workflow = _Workflow(
            initial,
            {"read": policy},
            {
                "bad-data": _result(initial, "succeeded", value={"healthy": False}),
                "good-data": _result(corrected, "succeeded", value={"healthy": True}),
            },
        )

        def validate(observation, _token):
            latest = observation.latest_execution
            assert latest is not None
            value = latest.synthesized.ordered_results[0][1]
            return (
                ResultValidation.valid()
                if value["healthy"]
                else ResultValidation.invalid("result is unhealthy")
            )

        result = await AutonomousPlanRunner(
            workflow,  # type: ignore[arg-type]
            result_validator=validate,
            replanner=lambda *_args: corrected,
        ).run("读取健康副本")

        self.assertEqual(result.status, "completed")
        self.assertEqual(workflow.executed, ["bad-data", "good-data"])

    async def test写计划失败绝不自动重规划(self) -> None:
        policy = IntentPlanPolicy(
            "write",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("manager",),
        )
        plan = MultiIntentPlan(
            "更新资源",
            (
                PlanStep(
                    "write",
                    "write",
                    requires_approval=True,
                    write=True,
                    replay_policy="never",
                    approval_roles=("manager",),
                ),
            ),
            plan_id="write-plan",
        )
        workflow = _Workflow(
            plan,
            {"write": policy},
            {"write-plan": _result(plan, "failed", error="write failed")},
        )
        replans = 0

        def replan(*_args):
            nonlocal replans
            replans += 1
            return None

        result = await AutonomousPlanRunner(
            workflow,  # type: ignore[arg-type]
            replanner=replan,
        ).run("更新资源")

        self.assertEqual(result.status, "manual_intervention")
        self.assertEqual(workflow.executed, ["write-plan"])
        self.assertEqual(replans, 0)

    async def test等待审批是挂起而不是失败或人工未知(self) -> None:
        policy = IntentPlanPolicy(
            "write",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("manager",),
        )
        plan = MultiIntentPlan(
            "更新资源",
            (
                PlanStep(
                    "write",
                    "write",
                    requires_approval=True,
                    write=True,
                    replay_policy="never",
                    approval_roles=("manager",),
                ),
            ),
            plan_id="approval-plan",
        )
        workflow = _Workflow(
            plan,
            {"write": policy},
            {
                "approval-plan": _result(
                    plan,
                    "waiting_approval",
                    approval_id="approval-1",
                )
            },
        )

        result = await AutonomousPlanRunner(workflow).run("更新资源")  # type: ignore[arg-type]

        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(result.pending_approval_ids, ("approval-1",))
        self.assertEqual(result.closed_loop.status, "suspended")

    async def test无可信validator时结构完成仍然fail_closed为未知(self) -> None:
        policy = IntentPlanPolicy("read")
        plan = MultiIntentPlan(
            "读取结果",
            (PlanStep("read", "read"),),
            plan_id="no-validator",
        )
        workflow = _Workflow(
            plan,
            {"read": policy},
            {"no-validator": _result(plan, "succeeded", value={"ok": True})},
        )

        result = await AutonomousPlanRunner(workflow).run(plan.request)  # type: ignore[arg-type]

        self.assertEqual(result.status, "manual_intervention")
        self.assertEqual(result.closed_loop.validations[-1].status, "outcome_unknown")
        self.assertIn("no trusted semantic", result.closed_loop.reason or "")


if __name__ == "__main__":
    unittest.main()
