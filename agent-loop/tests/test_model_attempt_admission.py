"""Physical Provider-attempt admission regression tests."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from pi_agent_loop import (
    AutonomousPlanRunner,
    ClosedLoopBudget,
    DurablePlanWorkflow,
    HybridRequestPlanner,
    IntentPlanPolicy,
    JournalPrincipal,
    Model,
    ModelAttemptAdmissionScope,
    ModelCallRuntime,
    ModelRetryPolicy,
    PlanBudgetExceeded,
    PlanResourceUsage,
    PlanUsageReservation,
    ScriptedProvider,
    SessionJournalAutonomousRunStore,
    SessionJournalPlanStore,
    SQLiteSessionEventJournal,
    StaticJournalKeyProvider,
    TokenPricing,
    activate_model_attempt_admission,
    assistant_message,
)
from pi_agent_loop.retry.model import RetryingStreamFn


class _InternalRetryProvider:
    provides_physical_attempt_admission = True

    def __init__(self, scripted, policy):
        self.scripted = scripted
        self.retry = RetryingStreamFn(
            scripted.stream,
            policy,
            physical_attempt_admission=True,
        )

    def stream(self, model, context, options):
        return self.retry(model, context, options)


class _UsageMeter:
    def __init__(self, *, reserved: PlanResourceUsage, actual: PlanResourceUsage):
        self.reserved = reserved
        self.actual = actual

    def reserve(self, *, run_id, reservation_id, stage, remaining):
        del run_id, remaining
        return PlanUsageReservation(reservation_id, stage, self.reserved)

    async def dispatch(self, _reservation, callback):
        return await callback()

    def settle(self, _reservation):
        return self.actual

    def cancel(self, _reservation):
        return None


class ModelAttemptAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="budget-model", provider="test", api="scripted")

    def response(
        self,
        *,
        retryable_error: bool = False,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> dict:
        message = assistant_message(
            model=self.model,
            stop_reason="error" if retryable_error else "stop",
            error_message="temporary" if retryable_error else None,
            content=(
                []
                if retryable_error
                else [{"type": "text", "text": "ok"}]
            ),
        )
        message["usage"]["input"] = input_tokens
        message["usage"]["output"] = output_tokens
        message["usage"]["totalTokens"] = input_tokens + output_tokens
        if retryable_error:
            message["providerError"] = {
                "code": "provider_server_error",
                "statusCode": 503,
                "retryAfterMs": 0,
                "retryable": True,
            }
        return message

    def runtime(self, provider: ScriptedProvider, *, pricing=None) -> ModelCallRuntime:
        return ModelCallRuntime(
            provider.stream,
            retry_policy=ModelRetryPolicy(
                enabled=True,
                max_retries=2,
                initial_delay_seconds=0,
                max_delay_seconds=1,
                jitter_ratio=0,
            ),
            pricing=pricing,
        )

    async def test_retry_is_rejected_before_second_raw_provider_call(self) -> None:
        provider = ScriptedProvider(
            [
                self.response(retryable_error=True, input_tokens=3),
                self.response(input_tokens=4),
            ]
        )
        runtime = self.runtime(provider)
        scope = ModelAttemptAdmissionScope(
            run_id="run-1",
            stage="planner",
            reservation_id="reservation-1",
            max_model_calls=1,
        )

        async with activate_model_attempt_admission(scope):
            result = await runtime.invoke(
                self.model,
                {"messages": [], "tools": []},
            )

        snapshot = await scope.snapshot()
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(
            result["providerError"]["code"],
            "model_attempt_budget_exceeded",
        )
        self.assertFalse(result["providerError"]["retryable"])
        self.assertEqual(snapshot.model_calls, 1)
        self.assertEqual(snapshot.tokens, 3)
        self.assertEqual(
            snapshot.attempt_ids,
            ("run-1:planner:reservation-1:attempt:1",),
        )
        await runtime.aclose()

    async def test_failed_and_successful_attempt_usage_is_aggregated(self) -> None:
        provider = ScriptedProvider(
            [
                self.response(retryable_error=True, input_tokens=2),
                self.response(input_tokens=3),
            ]
        )
        runtime = self.runtime(
            provider,
            pricing=TokenPricing(input_per_million=1_000_000),
        )
        scope = ModelAttemptAdmissionScope(
            run_id="run-2",
            stage="validator",
            reservation_id="reservation-2",
            max_model_calls=2,
            max_tokens=5,
            max_cost=5,
        )

        async with activate_model_attempt_admission(scope):
            result = await runtime.invoke(
                self.model,
                {"messages": [], "tools": []},
            )

        snapshot = await scope.snapshot()
        self.assertEqual(result["stopReason"], "stop")
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(snapshot.model_calls, 2)
        self.assertEqual(snapshot.tokens, 5)
        self.assertEqual(snapshot.cost, 5)
        self.assertEqual(snapshot.unknown_attempts, 0)
        self.assertEqual(snapshot.in_flight_attempts, 0)
        await runtime.aclose()

    async def test_consumed_token_reservation_blocks_retry_before_provider(self) -> None:
        provider = ScriptedProvider(
            [
                self.response(retryable_error=True, input_tokens=3),
                self.response(input_tokens=1),
            ]
        )
        runtime = self.runtime(provider)
        scope = ModelAttemptAdmissionScope(
            run_id="run-3",
            stage="synthesizer",
            reservation_id="reservation-3",
            max_model_calls=2,
            max_tokens=3,
        )

        async with activate_model_attempt_admission(scope):
            result = await runtime.invoke(
                self.model,
                {"messages": [], "tools": []},
            )

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(
            result["providerError"]["code"],
            "model_attempt_budget_exceeded",
        )
        self.assertEqual((await scope.snapshot()).tokens, 3)
        await runtime.aclose()

    async def test_concurrent_calls_share_one_atomic_attempt_budget(self) -> None:
        provider = ScriptedProvider(
            [self.response(input_tokens=1), self.response(input_tokens=1)]
        )
        runtime = self.runtime(provider)
        scope = ModelAttemptAdmissionScope(
            run_id="run-4",
            stage="router",
            reservation_id="reservation-4",
            max_model_calls=1,
        )

        async with activate_model_attempt_admission(scope):
            results = await asyncio.gather(
                runtime.invoke(self.model, {"messages": [], "tools": []}),
                runtime.invoke(self.model, {"messages": [], "tools": []}),
            )

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(
            sorted(result["stopReason"] for result in results),
            ["error", "stop"],
        )
        self.assertEqual((await scope.snapshot()).model_calls, 1)
        await runtime.aclose()

    async def test_no_scope_preserves_generic_stream_compatibility(self) -> None:
        provider = ScriptedProvider([self.response(input_tokens=1)])
        runtime = self.runtime(provider)

        result = await runtime.invoke(
            self.model,
            {"messages": [], "tools": []},
        )

        self.assertEqual(result["stopReason"], "stop")
        self.assertEqual(provider.call_count, 1)
        await runtime.aclose()

    async def test_provider_owned_internal_retry_counts_http_attempt_not_wrapper(
        self,
    ) -> None:
        scripted = ScriptedProvider(
            [
                self.response(retryable_error=True, input_tokens=2),
                self.response(input_tokens=2),
            ]
        )
        retry_policy = ModelRetryPolicy(
            enabled=True,
            max_retries=2,
            initial_delay_seconds=0,
            max_delay_seconds=1,
            jitter_ratio=0,
        )
        provider = _InternalRetryProvider(scripted, retry_policy)
        runtime = ModelCallRuntime(
            provider.stream,
            retry_policy=retry_policy,
        )
        scope = ModelAttemptAdmissionScope(
            run_id="run-internal",
            stage="router",
            reservation_id="reservation-internal",
            max_model_calls=1,
        )

        async with activate_model_attempt_admission(scope):
            result = await runtime.invoke(
                self.model,
                {"messages": [], "tools": []},
            )

        self.assertTrue(runtime.upstream_provides_physical_attempt_admission)
        self.assertEqual(scripted.call_count, 1)
        self.assertEqual(
            result["providerError"]["code"],
            "model_attempt_budget_exceeded",
        )
        self.assertEqual((await scope.snapshot()).model_calls, 1)
        await runtime.aclose()

    async def test_autonomous_planner_retry_uses_raw_attempt_scope(self) -> None:
        provider = ScriptedProvider(
            [
                self.response(retryable_error=True, input_tokens=3),
                self.response(input_tokens=4),
            ]
        )
        runtime = self.runtime(provider)

        async def planner_fn(_request, _catalog):
            result = await runtime.invoke(
                self.model,
                {"messages": [], "tools": []},
            )
            if result["stopReason"] == "error":
                raise RuntimeError(result["providerError"]["code"])
            return {
                "planId": "attempt-plan",
                "steps": [{"stepId": "read", "intent": "read"}],
            }

        with tempfile.TemporaryDirectory() as directory:
            runner, run_store = self._runner(
                Path(directory) / "state.sqlite3",
                planner_fn,
                meter=_UsageMeter(
                    reserved=PlanResourceUsage(model_calls=1, tokens=10),
                    actual=PlanResourceUsage(model_calls=1, tokens=3),
                ),
                budget=ClosedLoopBudget(max_model_calls=1, max_tokens=10),
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "model_attempt_budget_exceeded",
            ):
                await runner.prepare("read", run_id="attempt-run")

            durable = await run_store.load("attempt-run")
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(durable.resource_usage.model_calls, 1)
            self.assertEqual(durable.resource_usage.tokens, 3)
        await runtime.aclose()

    async def test_meter_cannot_underreport_failed_physical_attempt(self) -> None:
        provider = ScriptedProvider(
            [
                self.response(retryable_error=True, input_tokens=3),
                self.response(input_tokens=4),
            ]
        )
        runtime = self.runtime(provider)

        async def planner_fn(_request, _catalog):
            result = await runtime.invoke(
                self.model,
                {"messages": [], "tools": []},
            )
            self.assertEqual(result["stopReason"], "stop")
            return {
                "planId": "underreported-plan",
                "steps": [{"stepId": "read", "intent": "read"}],
            }

        with tempfile.TemporaryDirectory() as directory:
            runner, run_store = self._runner(
                Path(directory) / "state.sqlite3",
                planner_fn,
                meter=_UsageMeter(
                    reserved=PlanResourceUsage(model_calls=2, tokens=10),
                    actual=PlanResourceUsage(model_calls=1, tokens=4),
                ),
                budget=ClosedLoopBudget(max_model_calls=2, max_tokens=10),
            )

            with self.assertRaisesRegex(
                PlanBudgetExceeded,
                "物理 Provider Attempt",
            ):
                await runner.prepare("read", run_id="underreported-run")

            durable = await run_store.load("underreported-run")
            self.assertEqual(provider.call_count, 2)
            # Settlement was rejected; the conservative retry-tree upper bound
            # stays charged and recoverable instead of recording a smaller lie.
            self.assertEqual(durable.resource_usage.model_calls, 2)
            self.assertEqual(durable.resource_usage.tokens, 10)
            self.assertEqual(len(durable.active_reservations), 1)
        await runtime.aclose()

    def _runner(self, path, planner_fn, *, meter, budget):
        journal = SQLiteSessionEventJournal(
            path,
            key_provider=StaticJournalKeyProvider(
                {"test": b"m" * 32},
                active_key_id="test",
            ),
        )
        principal = JournalPrincipal.system("tenant")
        policy = IntentPlanPolicy("read")
        workflow = DurablePlanWorkflow(
            store=SessionJournalPlanStore(
                journal,
                principal,
                session_id="attempt-session",
            ),
            planner=HybridRequestPlanner({"read": policy}, planner_fn),
            policies={"read": policy},
            step_executor=None,
            approval_barrier=None,
        )
        run_store = SessionJournalAutonomousRunStore(
            journal,
            principal,
            session_id="attempt-session",
        )
        return (
            AutonomousPlanRunner(
                workflow,
                budget=budget,
                run_store=run_store,
                usage_meter=meter,
            ),
            run_store,
        )


if __name__ == "__main__":
    unittest.main()
