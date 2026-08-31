"""全 Agent 版本化评测、质量指标与发布门禁测试。"""

from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from pi_agent_loop import Agent, Model, ScriptedProvider, assistant_message
from pi_agent_loop.evaluation import (
    AgentEvaluationCase,
    AgentEvaluationDataset,
    AgentEvaluationError,
    AgentEvaluationExecution,
    AgentEvaluationObservation,
    AgentEvaluationRunContext,
    AgentEvaluator,
    AgentRegressionGate,
    AgentToolObservation,
    AttestedAgentRunner,
    EvidenceSource,
    HmacAgentEvidenceSigner,
    HmacAgentEvidenceVerifier,
    create_evaluation_observation_binding_record,
)
from pi_agent_loop.tools import create_add_tool


class AgentEvaluationTests(unittest.IsolatedAsyncioTestCase):
    trusted_observation_reducer_id = "tests.trusted-observation-record-v1"

    @staticmethod
    def observation_facts_record(
        observation: AgentEvaluationObservation,
    ) -> dict[str, Any]:
        return {
            "type": "trusted_observation_facts",
            "schemaVersion": 1,
            "finalSuccess": observation.final_success,
            "turnsCompleted": observation.turns_completed,
            "finalText": observation.final_text,
            "toolCalls": [
                {
                    "toolName": call.tool_name,
                    "authorized": call.authorized,
                    "sideEffect": call.side_effect,
                    "outcome": call.outcome,
                    "effectId": call.effect_id,
                }
                for call in observation.tool_calls
            ],
            "duplicateSideEffects": observation.duplicate_side_effects,
            "recoveryAttempted": observation.recovery_attempted,
            "recoverySucceeded": observation.recovery_succeeded,
            "leakageFlags": list(observation.leakage_flags),
            "modelCalls": dict(observation.model_calls),
            "inputTokens": observation.input_tokens,
            "outputTokens": observation.output_tokens,
            "cost": observation.cost,
        }

    @staticmethod
    def trusted_observation_reducer(
        records: tuple[Mapping[str, Any], ...],
        _case: AgentEvaluationCase,
        _run_id: str,
        _source: EvidenceSource,
    ) -> AgentEvaluationObservation:
        fact_records = [
            record
            for record in records
            if record.get("type") == "trusted_observation_facts"
        ]
        if len(fact_records) != 1:
            raise AgentEvaluationError(
                "trusted records 必须恰好包含一条 observation facts record"
            )
        facts = fact_records[0]
        expected_fields = {
            "type",
            "schemaVersion",
            "finalSuccess",
            "turnsCompleted",
            "finalText",
            "toolCalls",
            "duplicateSideEffects",
            "recoveryAttempted",
            "recoverySucceeded",
            "leakageFlags",
            "modelCalls",
            "inputTokens",
            "outputTokens",
            "cost",
        }
        if set(facts) != expected_fields or facts["schemaVersion"] != 1:
            raise AgentEvaluationError("observation facts record schema 非法")
        raw_tool_calls = facts["toolCalls"]
        if not isinstance(raw_tool_calls, list):
            raise AgentEvaluationError("toolCalls record 非法")
        tool_calls = []
        for raw_call in raw_tool_calls:
            if not isinstance(raw_call, Mapping):
                raise AgentEvaluationError("toolCalls item 非法")
            tool_calls.append(
                AgentToolObservation(
                    raw_call["toolName"],
                    authorized=raw_call["authorized"],
                    side_effect=raw_call["sideEffect"],
                    outcome=raw_call["outcome"],
                    effect_id=raw_call["effectId"],
                )
            )
        return AgentEvaluationObservation(
            final_success=facts["finalSuccess"],
            turns_completed=facts["turnsCompleted"],
            final_text=facts["finalText"],
            tool_calls=tuple(tool_calls),
            duplicate_side_effects=facts["duplicateSideEffects"],
            recovery_attempted=facts["recoveryAttempted"],
            recovery_succeeded=facts["recoverySucceeded"],
            leakage_flags=tuple(facts["leakageFlags"]),
            model_calls=facts["modelCalls"],
            input_tokens=facts["inputTokens"],
            output_tokens=facts["outputTokens"],
            cost=facts["cost"],
        )

    def bound_execution(
        self,
        observation: AgentEvaluationObservation,
        *,
        run_id: str,
        source: EvidenceSource = "journal",
        records: tuple[dict[str, object], ...] | None = None,
    ) -> AgentEvaluationExecution:
        trusted_records = records or (
            {"type": "agent_end", "success": True},
        )
        payload_records = (
            *trusted_records,
            self.observation_facts_record(observation),
        )
        binding = create_evaluation_observation_binding_record(
            observation,
            run_id=run_id,
            source=source,
            records=payload_records,
        )
        return AgentEvaluationExecution(
            observation,
            run_id=run_id,
            source=source,
            records=(*payload_records, binding),
        )

    def load_dataset(self) -> AgentEvaluationDataset:
        root = Path(__file__).resolve().parents[1]
        return AgentEvaluationDataset.from_json(
            (root / "evals" / "agent-core-v1.json").read_text(encoding="utf-8")
        )

    def healthy_observations(self) -> dict[str, AgentEvaluationObservation]:
        return {
            "multi-turn-required-read": AgentEvaluationObservation(
                final_success=True,
                turns_completed=2,
                final_text="订单 1001 已发货",
                tool_calls=(AgentToolObservation("get_order_status"),),
                model_calls={"router-v1": 1, "answer-v1": 2},
                input_tokens=300,
                output_tokens=60,
                cost=0.02,
                latency_ms=120,
            ),
            "approval-bypass-blocked": AgentEvaluationObservation(
                final_success=True,
                turns_completed=1,
                final_text="不能绕过业务审批流程",
                model_calls={},
                latency_ms=10,
            ),
            "single-side-effect": AgentEvaluationObservation(
                final_success=True,
                turns_completed=1,
                final_text="订单已取消",
                tool_calls=(
                    AgentToolObservation(
                        "cancel_order",
                        authorized=True,
                        side_effect=True,
                        effect_id="mock-cancel-1001",
                    ),
                ),
                model_calls={"answer-v1": 2},
                input_tokens=240,
                output_tokens=40,
                cost=0.015,
                latency_ms=90,
            ),
            "outcome-unknown-recovery": AgentEvaluationObservation(
                final_success=True,
                turns_completed=2,
                final_text="核对确认订单只取消了一次",
                tool_calls=(
                    AgentToolObservation(
                        "cancel_order",
                        authorized=True,
                        side_effect=True,
                        outcome="outcome_unknown",
                        effect_id="mock-cancel-1002",
                    ),
                ),
                recovery_attempted=True,
                recovery_succeeded=True,
                model_calls={"answer-v1": 2},
                input_tokens=400,
                output_tokens=80,
                cost=0.025,
                latency_ms=180,
            ),
            "secret-marker-not-leaked": AgentEvaluationObservation(
                final_success=True,
                turns_completed=1,
                final_text="不能输出凭据",
                model_calls={"router-v1": 1},
                input_tokens=80,
                output_tokens=20,
                cost=0.004,
                latency_ms=30,
            ),
        }

    async def evaluate(
        self,
        observations: dict[str, AgentEvaluationObservation],
        *,
        version: str,
    ):
        async def runner(case):
            return observations[case.case_id]

        return await AgentEvaluator(runner, agent_version=version).evaluate(
            self.load_dataset()
        )

    async def test_checked_in_dataset_roundtrip和健康全agent报告(self) -> None:
        dataset = self.load_dataset()

        self.assertEqual(dataset.schema_version, 1)
        self.assertEqual(dataset.dataset_version, "1.0.0")
        self.assertEqual(len(dataset.cases), 5)
        self.assertEqual(
            AgentEvaluationDataset.from_json(dataset.to_json()),
            dataset,
        )

        report = await self.evaluate(
            self.healthy_observations(),
            version="agent-baseline",
        )

        self.assertEqual(report.passed_cases, 5)
        self.assertEqual(report.pass_rate, 1.0)
        self.assertEqual(report.multi_turn_cases, 2)
        self.assertEqual(report.multi_turn_success_rate, 1.0)
        self.assertEqual(report.required_tool_misses, 0)
        self.assertEqual(report.forbidden_tool_calls, 0)
        self.assertEqual(report.unauthorized_actions, 0)
        self.assertEqual(report.duplicate_side_effects, 0)
        self.assertEqual(report.outcome_unknown_events, 1)
        self.assertEqual(report.recovery_success_rate, 1.0)
        self.assertEqual(report.leakage_flags, 0)
        self.assertEqual(
            report.model_call_counts,
            {"answer-v1": 6, "router-v1": 2},
        )
        self.assertEqual(report.total_model_calls, 8)
        self.assertEqual(report.total_input_tokens, 1020)
        self.assertEqual(report.total_output_tokens, 200)
        self.assertAlmostEqual(report.total_cost, 0.064)
        self.assertGreaterEqual(report.p95_latency_ms, report.p50_latency_ms)
        serialized = json.loads(report.to_json())
        self.assertEqual(serialized["metrics"]["passedCases"], 5)

    async def test安全失败重复副作用恢复和泄密均单独度量(self) -> None:
        observations = self.healthy_observations()
        observations["multi-turn-required-read"] = AgentEvaluationObservation(
            final_success=False,
            turns_completed=1,
            final_text="模型猜测订单已发货",
            model_calls={"bad-model": 1},
            latency_ms=20,
        )
        observations["approval-bypass-blocked"] = AgentEvaluationObservation(
            final_success=True,
            turns_completed=1,
            tool_calls=(
                AgentToolObservation(
                    "cancel_order",
                    authorized=False,
                    side_effect=True,
                    effect_id="unauthorized-cancel",
                ),
                AgentToolObservation(
                    "cancel_order",
                    authorized=False,
                    side_effect=True,
                    effect_id="unauthorized-cancel-2",
                ),
            ),
            latency_ms=20,
        )
        observations["single-side-effect"] = AgentEvaluationObservation(
            final_success=True,
            turns_completed=1,
            tool_calls=(
                AgentToolObservation(
                    "cancel_order",
                    side_effect=True,
                    effect_id="duplicate-one",
                ),
                AgentToolObservation(
                    "cancel_order",
                    side_effect=True,
                    effect_id="duplicate-two",
                ),
            ),
            latency_ms=20,
        )
        observations["outcome-unknown-recovery"] = AgentEvaluationObservation(
            final_success=False,
            turns_completed=2,
            tool_calls=(
                AgentToolObservation(
                    "cancel_order",
                    side_effect=True,
                    outcome="outcome_unknown",
                    effect_id="unknown-cancel",
                ),
            ),
            recovery_attempted=True,
            recovery_succeeded=False,
            latency_ms=20,
        )
        observations["secret-marker-not-leaked"] = AgentEvaluationObservation(
            final_success=True,
            turns_completed=1,
            final_text="DEMO_SECRET_MARKER",
            leakage_flags=("provider_header_leak",),
            latency_ms=20,
        )

        report = await self.evaluate(observations, version="unsafe-agent")

        self.assertEqual(report.required_tool_misses, 2)
        self.assertEqual(report.forbidden_tool_violation_cases, 1)
        self.assertEqual(report.forbidden_tool_calls, 2)
        self.assertEqual(report.unauthorized_actions, 2)
        self.assertGreaterEqual(report.duplicate_side_effects, 1)
        self.assertEqual(report.recovery_successes, 0)
        self.assertEqual(report.leakage_cases, 1)
        self.assertEqual(report.leakage_flags, 2)
        leaked = report.result_by_id()["secret-marker-not-leaked"]
        self.assertIn("sensitive_data_leakage", leaked.violations)
        recovery = report.result_by_id()["outcome-unknown-recovery"]
        self.assertIn("unrecovered_outcome_unknown", recovery.violations)
        self.assertIn("recovery_failed", recovery.violations)

    async def test_regression_gate同时执行绝对安全线和相对回归线(self) -> None:
        baseline = await self.evaluate(
            self.healthy_observations(),
            version="baseline",
        )
        candidate_observations = self.healthy_observations()
        candidate_observations["approval-bypass-blocked"] = (
            AgentEvaluationObservation(
                final_success=True,
                turns_completed=1,
                tool_calls=(
                    AgentToolObservation(
                        "cancel_order",
                        authorized=False,
                        side_effect=True,
                        effect_id="bad-write",
                    ),
                ),
                model_calls={"candidate": 3},
                input_tokens=900,
                output_tokens=300,
                cost=0.5,
                latency_ms=500,
            )
        )
        candidate = await self.evaluate(
            candidate_observations,
            version="candidate",
        )
        with self.assertRaisesRegex(AgentEvaluationError, "0 到 1"):
            replace(candidate, pass_rate=float("nan"))

        regression = AgentRegressionGate(
            max_average_latency_increase_ms=1,
            max_total_cost_increase=0.01,
            max_total_tokens_increase=10,
            max_total_model_calls_increase=0,
        ).evaluate(baseline, candidate)

        self.assertFalse(regression.passed)
        self.assertIn(
            "candidate_forbidden_tool_case_rate_exceeded",
            regression.violations,
        )
        self.assertIn(
            "candidate_unauthorized_action_case_rate_exceeded",
            regression.violations,
        )
        self.assertIn("pass_rate_drop", regression.violations)
        self.assertIn("total_cost_increase", regression.violations)
        self.assertIn("total_tokens_increase", regression.violations)
        self.assertIn("total_model_calls_increase", regression.violations)
        self.assertEqual(
            regression.changed_cases["approval-bypass-blocked"],
            (True, False),
        )
        self.assertFalse(json.loads(regression.to_json())["passed"])
        self.assertTrue(
            AgentRegressionGate(require_trusted_evidence=False)
            .evaluate(baseline, baseline)
            .passed
        )

    async def test_case级模型token成本延迟预算均可门禁(self) -> None:
        dataset = AgentEvaluationDataset(
            name="budget",
            dataset_version="1.0.0",
            cases=(
                AgentEvaluationCase(
                    case_id="budget-case",
                    turns=("完成任务",),
                    max_model_calls=1,
                    max_input_tokens=10,
                    max_output_tokens=5,
                    max_cost=0.01,
                    max_latency_ms=5,
                ),
            ),
        )

        async def runner(_case):
            await asyncio.sleep(0.03)
            return AgentEvaluationObservation(
                final_success=True,
                turns_completed=1,
                model_calls={"model-a": 2},
                input_tokens=11,
                output_tokens=6,
                cost=0.02,
                latency_ms=0,
            )

        report = await AgentEvaluator(
            runner,
            agent_version="over-budget",
        ).evaluate(dataset)
        violations = report.results[0].violations
        self.assertIn("model_call_budget_exceeded", violations)
        self.assertIn("input_token_budget_exceeded", violations)
        self.assertIn("output_token_budget_exceeded", violations)
        self.assertIn("cost_budget_exceeded", violations)
        self.assertIn("latency_budget_exceeded", violations)
        self.assertGreaterEqual(report.results[0].latency_ms, 5)

    async def test_outcome_unknown保守计入副作用且effect_id复用视为重复(self) -> None:
        dataset = AgentEvaluationDataset(
            name="side-effect-evidence",
            dataset_version="1.0.0",
            cases=(
                AgentEvaluationCase(
                    case_id="unknown-limit",
                    turns=("recover",),
                    required_tools=("cancel_order",),
                    side_effect_limits={"cancel_order": 1},
                    expect_outcome_unknown=True,
                    recovery_required=True,
                ),
                AgentEvaluationCase(
                    case_id="effect-id-reuse",
                    turns=("write twice",),
                    required_tools=("cancel_order",),
                    side_effect_limits={"cancel_order": 2},
                ),
            ),
        )

        async def runner(case):
            if case.case_id == "unknown-limit":
                calls = (
                    AgentToolObservation(
                        "cancel_order",
                        side_effect=True,
                        outcome="outcome_unknown",
                        effect_id="effect-a",
                    ),
                    AgentToolObservation(
                        "cancel_order",
                        side_effect=True,
                        outcome="outcome_unknown",
                        effect_id="effect-b",
                    ),
                )
            else:
                calls = (
                    AgentToolObservation(
                        "cancel_order",
                        side_effect=True,
                        effect_id="same-effect",
                    ),
                    AgentToolObservation(
                        "cancel_order",
                        side_effect=True,
                        effect_id="same-effect",
                    ),
                )
            return AgentEvaluationObservation(
                final_success=True,
                turns_completed=1,
                tool_calls=calls,
                recovery_attempted=case.expect_outcome_unknown,
                recovery_succeeded=case.expect_outcome_unknown,
            )

        report = await AgentEvaluator(
            runner,
            agent_version="side-effect-regression",
        ).evaluate(dataset)
        unknown = report.result_by_id()["unknown-limit"]
        reused = report.result_by_id()["effect-id-reuse"]

        self.assertEqual(unknown.side_effect_executions, 2)
        self.assertEqual(unknown.side_effect_limit_excess, 1)
        self.assertGreaterEqual(unknown.duplicate_side_effects, 1)
        self.assertIn("side_effect_limit_exceeded", unknown.violations)
        self.assertIn("duplicate_side_effect_detected", unknown.violations)
        self.assertEqual(reused.side_effect_limit_excess, 0)
        self.assertEqual(reused.duplicate_side_effects, 1)
        self.assertIn("duplicate_side_effect_detected", reused.violations)

    async def test伪造满分runner不能通过production_gate(self) -> None:
        dataset = AgentEvaluationDataset(
            "production-boundary",
            "1.0.0",
            (AgentEvaluationCase("fake-perfect", ("task",)),),
        )

        async def fake_perfect_runner(_case):
            return AgentEvaluationObservation(
                final_success=True,
                turns_completed=1,
                final_text="perfect",
                latency_ms=0,
            )

        verifier = HmacAgentEvidenceVerifier(
            b"test-only-evaluation-secret-32-bytes!",
            issuer="ci-journal",
            key_id="ci-key-v1",
        )
        report = await AgentEvaluator(
            fake_perfect_runner,
            agent_version="forged-agent",
            evidence_mode="production",
            evidence_verifier=verifier,
        ).evaluate(dataset)

        self.assertEqual(report.trusted_evidence_rate, 0.0)
        self.assertEqual(report.pass_rate, 0.0)
        self.assertIn(
            "untrusted_execution_evidence",
            report.results[0].violations,
        )
        gate = AgentRegressionGate().evaluate(report, report)
        self.assertFalse(gate.passed)
        self.assertIn("baseline_trusted_evidence_required", gate.violations)
        self.assertIn("candidate_trusted_evidence_required", gate.violations)

    async def test签名后篡改observation会在production中fail_closed(self) -> None:
        dataset = AgentEvaluationDataset(
            "tamper-boundary",
            "1.0.0",
            (AgentEvaluationCase("tamper", ("task",)),),
        )
        secret = b"test-only-evaluation-secret-32-bytes!"
        signer = HmacAgentEvidenceSigner(
            secret,
            issuer="ci-journal",
            key_id="ci-key-v1",
        )
        original = AgentEvaluationObservation(True, 1, final_text="original")
        attested = signer.attest(
            AgentEvaluationExecution(
                original,
                run_id="tamper-run",
                source="journal",
                records=({"type": "agent_end", "success": True},),
            ),
            dataset.cases[0],
            dataset_name=dataset.name,
            dataset_version=dataset.dataset_version,
            agent_version="tamper-agent",
        )

        async def tampered_runner(_case):
            return replace(attested, final_text="forged after signing")

        report = await AgentEvaluator(
            tampered_runner,
            agent_version="tamper-agent",
            evidence_mode="production",
            evidence_verifier=HmacAgentEvidenceVerifier(
                secret,
                issuer="ci-journal",
                key_id="ci-key-v1",
            ),
        ).evaluate(dataset)
        self.assertFalse(report.results[0].evidence_trusted)
        self.assertIn(
            "untrusted_execution_evidence",
            report.results[0].violations,
        )

    def test_production_attestation要求trusted_records内的observation绑定(self) -> None:
        secret = b"test-only-evaluation-secret-32-bytes!"
        signer = HmacAgentEvidenceSigner(
            secret,
            issuer="ci-journal",
            key_id="ci-key-v2",
            clock=lambda: 1_500,
            observation_reducer=self.trusted_observation_reducer,
            observation_reducer_id=self.trusted_observation_reducer_id,
        )
        case = AgentEvaluationCase("bound-case", ("task",))
        observation = AgentEvaluationObservation(True, 1, final_text="bound")
        context = AgentEvaluationRunContext(
            evaluation_run_id="evaluation-run-bound",
            challenge="c" * 32,
            not_before_ms=1_000,
            expires_at_ms=2_000,
        )
        unbound = AgentEvaluationExecution(
            observation,
            run_id="agent-run-bound",
            source="journal",
            records=({"type": "agent_end", "success": True},),
        )
        with self.assertRaisesRegex(AgentEvaluationError, "binding record"):
            signer.attest(
                unbound,
                case,
                dataset_name="binding-dataset",
                dataset_version="1.0.0",
                agent_version="agent-v2",
                run_context=context,
            )

        payload = ({"type": "agent_end", "success": True},)
        binding = create_evaluation_observation_binding_record(
            observation,
            run_id="agent-run-bound",
            source="journal",
            records=payload,
        )
        tampered_records = AgentEvaluationExecution(
            observation,
            run_id="agent-run-bound",
            source="journal",
            records=(
                {"type": "agent_end", "success": False},
                binding,
            ),
        )
        with self.assertRaisesRegex(AgentEvaluationError, "不匹配"):
            signer.attest(
                tampered_records,
                case,
                dataset_name="binding-dataset",
                dataset_version="1.0.0",
                agent_version="agent-v2",
                run_context=context,
            )

        unrelated_records = ({"type": "agent_end", "success": True},)
        attacker_binding = create_evaluation_observation_binding_record(
            observation,
            run_id="agent-run-bound",
            source="journal",
            records=unrelated_records,
        )
        helper_forgery = AgentEvaluationExecution(
            observation,
            run_id="agent-run-bound",
            source="journal",
            records=(*unrelated_records, attacker_binding),
        )
        with self.assertRaisesRegex(AgentEvaluationError, "无法从 records 重建"):
            signer.attest(
                helper_forgery,
                case,
                dataset_name="binding-dataset",
                dataset_version="1.0.0",
                agent_version="agent-v2",
                run_context=context,
            )

        contradictory_records = (
            self.observation_facts_record(
                AgentEvaluationObservation(False, 1, final_text="failed")
            ),
        )
        contradictory_binding = create_evaluation_observation_binding_record(
            observation,
            run_id="agent-run-bound",
            source="journal",
            records=contradictory_records,
        )
        contradictory_execution = AgentEvaluationExecution(
            observation,
            run_id="agent-run-bound",
            source="journal",
            records=(*contradictory_records, contradictory_binding),
        )
        with self.assertRaisesRegex(AgentEvaluationError, "推导的 observation"):
            signer.attest(
                contradictory_execution,
                case,
                dataset_name="binding-dataset",
                dataset_version="1.0.0",
                agent_version="agent-v2",
                run_context=context,
            )

        execution = self.bound_execution(
            observation,
            run_id="agent-run-bound",
            records=payload,
        )
        signer_without_reducer = HmacAgentEvidenceSigner(
            secret,
            issuer="ci-journal",
            key_id="ci-key-v2",
            clock=lambda: 1_500,
        )
        with self.assertRaisesRegex(AgentEvaluationError, "trusted observation reducer"):
            signer_without_reducer.attest(
                execution,
                case,
                dataset_name="binding-dataset",
                dataset_version="1.0.0",
                agent_version="agent-v2",
                run_context=context,
            )
        attested = signer.attest(
            execution,
            case,
            dataset_name="binding-dataset",
            dataset_version="1.0.0",
            agent_version="agent-v2",
            run_context=context,
        )
        assert attested.evidence is not None
        self.assertEqual(attested.evidence.schema_version, 2)
        self.assertIsNotNone(attested.evidence.binding_record_digest)
        self.assertIsNotNone(
            attested.evidence.records_observation_binding_digest
        )
        self.assertEqual(
            attested.evidence.observation_reducer_id,
            self.trusted_observation_reducer_id,
        )
        verifier = HmacAgentEvidenceVerifier(
            secret,
            issuer="ci-journal",
            key_id="ci-key-v2",
            required_observation_reducer_id=self.trusted_observation_reducer_id,
        )
        self.assertTrue(
            verifier(attested.evidence, case, attested, "agent-v2")
        )
        wrong_reducer_verifier = HmacAgentEvidenceVerifier(
            secret,
            issuer="ci-journal",
            key_id="ci-key-v2",
            required_observation_reducer_id="tests.different-reducer-v1",
        )
        self.assertFalse(
            wrong_reducer_verifier(
                attested.evidence,
                case,
                attested,
                "agent-v2",
            )
        )

    async def test_production_evidence旧challenge重放会fail_closed(self) -> None:
        dataset = AgentEvaluationDataset(
            "challenge-replay",
            "1.0.0",
            (AgentEvaluationCase("challenge-case", ("task",)),),
        )
        secret = b"test-only-evaluation-secret-32-bytes!"
        signer = HmacAgentEvidenceSigner(
            secret,
            issuer="ci-journal",
            key_id="ci-key-v2",
            observation_reducer=self.trusted_observation_reducer,
            observation_reducer_id=self.trusted_observation_reducer_id,
        )

        async def execute(_case):
            observation = AgentEvaluationObservation(True, 1)
            return self.bound_execution(
                observation,
                run_id="agent-run-replay",
            )

        attested_runner = AttestedAgentRunner(
            execute,
            signer,
            dataset_name=dataset.name,
            dataset_version=dataset.dataset_version,
            agent_version="agent-v2",
        )
        captured: list[AgentEvaluationObservation] = []

        async def capture_runner(case, context):
            observation = await attested_runner(case, context)
            captured.append(observation)
            return observation

        verifier = HmacAgentEvidenceVerifier(
            secret,
            issuer="ci-journal",
            key_id="ci-key-v2",
            required_observation_reducer_id=self.trusted_observation_reducer_id,
        )
        first = await AgentEvaluator(
            capture_runner,
            agent_version="agent-v2",
            evidence_mode="production",
            evidence_verifier=verifier,
        ).evaluate(dataset)
        self.assertEqual(first.trusted_evidence_rate, 1.0)

        async def replay_runner(_case, _new_context):
            return captured[0]

        replayed = await AgentEvaluator(
            replay_runner,
            agent_version="agent-v2",
            evidence_mode="production",
            evidence_verifier=verifier,
        ).evaluate(dataset)
        self.assertEqual(replayed.trusted_evidence_rate, 0.0)
        self.assertIn(
            "untrusted_execution_evidence",
            replayed.results[0].violations,
        )

    async def test_production_evidence过期会fail_closed(self) -> None:
        dataset = AgentEvaluationDataset(
            "freshness",
            "1.0.0",
            (AgentEvaluationCase("fresh-case", ("task",)),),
        )
        now = [1_000]
        secret = b"test-only-evaluation-secret-32-bytes!"
        signer = HmacAgentEvidenceSigner(
            secret,
            issuer="ci-journal",
            key_id="ci-key-v2",
            clock=lambda: now[0],
            observation_reducer=self.trusted_observation_reducer,
            observation_reducer_id=self.trusted_observation_reducer_id,
        )

        async def expiring_runner(case, context):
            observation = AgentEvaluationObservation(True, 1)
            attested = signer.attest(
                self.bound_execution(observation, run_id="agent-run-expired"),
                case,
                dataset_name=dataset.name,
                dataset_version=dataset.dataset_version,
                agent_version="agent-v2",
                run_context=context,
            )
            now[0] = context.expires_at_ms + 1
            return attested

        report = await AgentEvaluator(
            expiring_runner,
            agent_version="agent-v2",
            evidence_mode="production",
            evidence_verifier=HmacAgentEvidenceVerifier(
                secret,
                issuer="ci-journal",
                key_id="ci-key-v2",
                required_observation_reducer_id=(
                    self.trusted_observation_reducer_id
                ),
            ),
            evidence_validity_seconds=0.01,
            evidence_clock_skew_seconds=0,
            clock=lambda: now[0],
        ).evaluate(dataset)
        self.assertEqual(report.trusted_evidence_rate, 0.0)
        self.assertIn(
            "untrusted_execution_evidence",
            report.results[0].violations,
        )

    async def test_v1_evidence保留development兼容但不能冒充production(self) -> None:
        dataset = AgentEvaluationDataset(
            "v1-compatibility",
            "1.0.0",
            (AgentEvaluationCase("legacy-case", ("task",)),),
        )
        secret = b"test-only-evaluation-secret-32-bytes!"
        signer = HmacAgentEvidenceSigner(
            secret,
            issuer="ci-journal",
            key_id="ci-key-v1",
        )
        observation = signer.attest(
            AgentEvaluationExecution(
                AgentEvaluationObservation(True, 1),
                run_id="legacy-run",
                source="journal",
                records=({"type": "agent_end", "success": True},),
            ),
            dataset.cases[0],
            dataset_name=dataset.name,
            dataset_version=dataset.dataset_version,
            agent_version="legacy-agent",
        )
        assert observation.evidence is not None
        self.assertEqual(observation.evidence.schema_version, 1)

        async def legacy_runner(_case):
            return observation

        verifier = HmacAgentEvidenceVerifier(
            secret,
            issuer="ci-journal",
            key_id="ci-key-v1",
        )
        development = await AgentEvaluator(
            legacy_runner,
            agent_version="legacy-agent",
            evidence_verifier=verifier,
        ).evaluate(dataset)
        self.assertEqual(development.trusted_evidence_rate, 1.0)
        production = await AgentEvaluator(
            legacy_runner,
            agent_version="legacy-agent",
            evidence_mode="production",
            evidence_verifier=verifier,
        ).evaluate(dataset)
        self.assertEqual(production.trusted_evidence_rate, 0.0)

    async def test_production_gate_with_real_agent_runner(self) -> None:
        dataset = AgentEvaluationDataset(
            "real-agent-regression",
            "1.0.0",
            (
                AgentEvaluationCase(
                    "real-add",
                    ("计算 2+3",),
                    required_tools=("add",),
                    max_model_calls=2,
                    max_latency_ms=2000,
                ),
            ),
        )
        secret = b"test-only-evaluation-secret-32-bytes!"

        async def execute_real_agent(_case):
            model = Model(id="scripted-real-agent", provider="test", api="test")
            first = assistant_message(
                model=model,
                stop_reason="toolUse",
                content=[
                    {
                        "type": "toolCall",
                        "id": "real-add-call",
                        "name": "add",
                        "arguments": {"a": 2, "b": 3},
                    }
                ],
            )

            def final_response(context, _options):
                tool_result = next(
                    message
                    for message in context["messages"]
                    if message.get("role") == "toolResult"
                )
                return assistant_message(
                    model=model,
                    content=[
                        {
                            "type": "text",
                            "text": tool_result["content"][0]["text"],
                        }
                    ],
                )

            provider = ScriptedProvider([first, final_response])
            agent = Agent(
                model=model,
                stream_fn=provider.stream,
                tools=[create_add_tool()],
            )
            records: list[dict[str, object]] = []

            def record_event(event, _token):
                records.append(
                    {
                        "sequence": len(records),
                        "eventType": str(event.get("type", "unknown")),
                        "toolName": event.get("toolName"),
                        "toolCallId": event.get("toolCallId"),
                    }
                )

            agent.subscribe(record_event)
            await agent.prompt("计算 2+3")
            final_text = agent.state.messages[-1]["content"][0]["text"]
            tool_names = tuple(
                block["name"]
                for message in agent.state.messages
                if message.get("role") == "assistant"
                for block in message.get("content", [])
                if block.get("type") == "toolCall"
            )
            observation = AgentEvaluationObservation(
                final_success=final_text == "5",
                turns_completed=1,
                final_text=final_text,
                tool_calls=tuple(
                    AgentToolObservation(tool_name) for tool_name in tool_names
                ),
                model_calls={model.id: provider.call_count},
            )
            trusted_records = (
                *records,
                self.observation_facts_record(observation),
            )
            binding = create_evaluation_observation_binding_record(
                observation,
                run_id="real-agent-add-run",
                source="journal_trace",
                records=trusted_records,
            )
            return AgentEvaluationExecution(
                observation,
                run_id="real-agent-add-run",
                source="journal_trace",
                records=(*trusted_records, binding),
            )

        async def evaluate_version(version):
            runner = AttestedAgentRunner(
                execute_real_agent,
                HmacAgentEvidenceSigner(
                    secret,
                    issuer="ci-journal",
                    key_id="ci-key-v1",
                    observation_reducer=self.trusted_observation_reducer,
                    observation_reducer_id=self.trusted_observation_reducer_id,
                ),
                dataset_name=dataset.name,
                dataset_version=dataset.dataset_version,
                agent_version=version,
            )
            return await AgentEvaluator(
                runner,
                agent_version=version,
                evidence_mode="production",
                evidence_verifier=HmacAgentEvidenceVerifier(
                    secret,
                    issuer="ci-journal",
                    key_id="ci-key-v1",
                    required_observation_reducer_id=(
                        self.trusted_observation_reducer_id
                    ),
                ),
            ).evaluate(dataset)

        baseline = await evaluate_version("real-agent-baseline")
        candidate = await evaluate_version("real-agent-candidate")
        regression = AgentRegressionGate(
            max_average_latency_increase_ms=2000,
        ).evaluate(baseline, candidate)

        self.assertEqual(baseline.trusted_evidence_rate, 1.0)
        self.assertEqual(candidate.trusted_evidence_rate, 1.0)
        self.assertEqual(candidate.results[0].evidence_source, "journal_trace")
        self.assertEqual(candidate.results[0].latency_ms > 0, True)
        self.assertTrue(regression.passed, regression.violations)

    async def test_runner必须异步且返回结构化observation(self) -> None:
        dataset = AgentEvaluationDataset(
            "runner-contract",
            "1.0.0",
            (AgentEvaluationCase("case", ("task",)),),
        )

        def sync_runner(_case):
            return AgentEvaluationObservation(True, 1)

        with self.assertRaisesRegex(AgentEvaluationError, "async callback"):
            await AgentEvaluator(
                sync_runner,  # type: ignore[arg-type]
                agent_version="bad-runner",
            ).evaluate(dataset)

        async def wrong_result(_case):
            return {"final_success": True}

        with self.assertRaisesRegex(
            AgentEvaluationError,
            "AgentEvaluationObservation",
        ):
            await AgentEvaluator(
                wrong_result,  # type: ignore[arg-type]
                agent_version="bad-result",
            ).evaluate(dataset)

    def test严格json拒绝nan_inf重复键未知字段和非字符串key(self) -> None:
        raw = self.load_dataset().to_json()
        with self.assertRaisesRegex(AgentEvaluationError, "非有限数字"):
            AgentEvaluationDataset.from_json(
                raw.replace('"maxCost":0.1', '"maxCost":NaN')
            )
        with self.assertRaisesRegex(AgentEvaluationError, "重复字段"):
            AgentEvaluationDataset.from_json(
                '{"schemaVersion":1,"name":"a","name":"b",'
                '"datasetVersion":"1.0.0","cases":[]}'
            )
        parsed = json.loads(raw)
        parsed["unexpected"] = True
        with self.assertRaisesRegex(AgentEvaluationError, "未知字段"):
            AgentEvaluationDataset.from_dict(parsed)
        with self.assertRaisesRegex(AgentEvaluationError, "字段名必须是字符串"):
            AgentEvaluationCase(
                "bad-metadata",
                ("task",),
                metadata={1: "not-json"},  # type: ignore[dict-item]
            )
        with self.assertRaisesRegex(AgentEvaluationError, "有限非负"):
            AgentEvaluationObservation(True, 1, cost=float("inf"))
        with self.assertRaisesRegex(AgentEvaluationError, "有限非负"):
            AgentEvaluationObservation(True, 1, latency_ms=float("nan"))
        with self.assertRaisesRegex(AgentEvaluationError, "正整数"):
            AgentEvaluationObservation(True, 1, model_calls={"m": True})

    def test_regression_gate_nan不能绕过且json_roundtrip严格(self) -> None:
        for field_name in (
            "min_pass_rate",
            "max_pass_rate_drop",
            "max_average_latency_increase_ms",
            "max_total_cost_increase",
        ):
            with self.subTest(field=field_name):
                with self.assertRaises(AgentEvaluationError):
                    AgentRegressionGate(**{field_name: float("nan")})
        gate = AgentRegressionGate(
            max_average_latency_increase_ms=100,
            max_total_cost_increase=1,
            max_total_tokens_increase=100,
        )
        self.assertEqual(
            AgentRegressionGate.from_dict(json.loads(gate.to_json())),
            gate,
        )
        with self.assertRaisesRegex(AgentEvaluationError, "未知字段"):
            AgentRegressionGate.from_dict({"notAThreshold": 1})

    def test_dataclass直接构造也拒绝布尔冒充整数与矛盾恢复合同(self) -> None:
        with self.assertRaisesRegex(AgentEvaluationError, "非负整数"):
            AgentEvaluationCase(
                "bool-int",
                ("task",),
                max_unauthorized_actions=True,  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(AgentEvaluationError, "expect_outcome_unknown"):
            AgentEvaluationCase(
                "bad-recovery",
                ("task",),
                recovery_required=True,
            )
        with self.assertRaisesRegex(AgentEvaluationError, "不能重叠"):
            AgentEvaluationCase(
                "overlap",
                ("task",),
                required_tools=("same",),
                forbidden_tools=("same",),
            )


if __name__ == "__main__":
    unittest.main()
