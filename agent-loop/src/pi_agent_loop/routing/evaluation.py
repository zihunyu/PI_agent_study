"""Hybrid Router 的离线评估、阈值校准和模型版本回归。"""

from __future__ import annotations

import inspect
import json
import math
import statistics
import time
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from .types import RequestDecision

_VALID_STATUSES = frozenset(
    {
        "in_scope_no_tool",
        "in_scope_tool_ready",
        "in_scope_plan_required",
        "in_scope_need_clarification",
        "in_scope_capability_missing",
        "in_scope_approval_required",
        "permission_denied",
        "out_of_scope",
        "prohibited",
    }
)
_DEFAULT_SAFE_STATUSES = (
    "prohibited",
    "permission_denied",
    "out_of_scope",
    "in_scope_need_clarification",
)


class RouterEvaluationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RouterEvaluationCase:
    case_id: str
    prompt: str
    expected_status: str | None = None
    expected_intent: str | None = None
    requires_tool: bool = False
    expected_tools: tuple[str, ...] = ()
    adversarial: bool = False
    safe_statuses: tuple[str, ...] = _DEFAULT_SAFE_STATUSES
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise RouterEvaluationError("case_id 不能为空")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise RouterEvaluationError("prompt 不能为空")
        if self.expected_status is None and self.expected_intent is None:
            raise RouterEvaluationError(
                "评估案例至少提供 expected_status 或 expected_intent"
            )
        if self.expected_intent is not None and (
            not isinstance(self.expected_intent, str) or not self.expected_intent.strip()
        ):
            raise RouterEvaluationError("expected_intent 必须是非空字符串或 null")
        if not isinstance(self.requires_tool, bool) or not isinstance(self.adversarial, bool):
            raise RouterEvaluationError("requires_tool/adversarial 必须是布尔值")
        if self.adversarial and not self.safe_statuses:
            raise RouterEvaluationError("恶意案例必须声明至少一个安全状态")
        if self.expected_status is not None and self.expected_status not in _VALID_STATUSES:
            raise RouterEvaluationError("expected_status 不是合法 Router 状态")
        if any(status not in _VALID_STATUSES for status in self.safe_statuses):
            raise RouterEvaluationError("safe_statuses 包含非法 Router 状态")
        if any(not isinstance(tool, str) or not tool.strip() for tool in self.expected_tools):
            raise RouterEvaluationError("expected_tools 必须是非空字符串数组")
        if len(self.expected_tools) != len(set(self.expected_tools)):
            raise RouterEvaluationError("expected_tools 不能重复")
        if self.expected_tools and not self.requires_tool:
            raise RouterEvaluationError("声明 expected_tools 时 requires_tool 必须为 true")
        _strict_json(dict(self.metadata), "metadata")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RouterEvaluationCase":
        allowed = {
            "caseId",
            "prompt",
            "expectedStatus",
            "expectedIntent",
            "requiresTool",
            "expectedTools",
            "adversarial",
            "safeStatuses",
            "metadata",
        }
        unknown = set(value) - allowed
        if unknown:
            raise RouterEvaluationError(f"评估案例包含未知字段：{sorted(unknown)}")
        safe = value.get("safeStatuses")
        return cls(
            case_id=_required_text(value, "caseId"),
            prompt=_required_text(value, "prompt"),
            expected_status=_optional_text(value.get("expectedStatus")),
            expected_intent=_optional_text(value.get("expectedIntent")),
            requires_tool=_boolean(value.get("requiresTool", False), "requiresTool"),
            expected_tools=tuple(
                _text_list(value.get("expectedTools", []), "expectedTools")
            ),
            adversarial=_boolean(value.get("adversarial", False), "adversarial"),
            safe_statuses=(
                tuple(_text_list(safe, "safeStatuses"))
                if safe is not None
                else _DEFAULT_SAFE_STATUSES
            ),
            metadata=dict(_mapping(value.get("metadata", {}), "metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "prompt": self.prompt,
            "expectedStatus": self.expected_status,
            "expectedIntent": self.expected_intent,
            "requiresTool": self.requires_tool,
            "expectedTools": list(self.expected_tools),
            "adversarial": self.adversarial,
            "safeStatuses": list(self.safe_statuses),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RouterEvaluationDataset:
    name: str
    cases: tuple[RouterEvaluationCase, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise RouterEvaluationError("数据集名称不能为空")
        if self.schema_version != 1:
            raise RouterEvaluationError("不支持的 Router 数据集版本")
        if not self.cases:
            raise RouterEvaluationError("Router 数据集不能为空")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise RouterEvaluationError("Router 数据集 case_id 必须唯一")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RouterEvaluationDataset":
        if set(value) - {"schemaVersion", "name", "cases"}:
            raise RouterEvaluationError("Router 数据集包含未知字段")
        raw_cases = value.get("cases")
        if not isinstance(raw_cases, list):
            raise RouterEvaluationError("Router 数据集 cases 必须是数组")
        return cls(
            name=_required_text(value, "name"),
            cases=tuple(
                RouterEvaluationCase.from_dict(_mapping(case, "case"))
                for case in raw_cases
            ),
            schema_version=_integer(value.get("schemaVersion", 1), "schemaVersion"),
        )

    @classmethod
    def from_json(cls, value: str) -> "RouterEvaluationDataset":
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise RouterEvaluationError(f"Router 数据集 JSON 无效：{error}") from error
        return cls.from_dict(_mapping(parsed, "dataset"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "name": self.name,
            "cases": [case.to_dict() for case in self.cases],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False
        )


@dataclass(frozen=True, slots=True)
class RouterObservation:
    decision: RequestDecision
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RouterEvaluationError(f"{name} 必须是非负整数")
        if not isinstance(self.cost, (int, float)) or isinstance(self.cost, bool):
            raise RouterEvaluationError("cost 必须是非负数字")
        if not math.isfinite(float(self.cost)) or self.cost < 0:
            raise RouterEvaluationError("cost 必须是有限非负数字")


@dataclass(frozen=True, slots=True)
class RouterCaseResult:
    case_id: str
    expected_label: str
    actual_label: str
    expected_status: str | None
    actual_status: str
    expected_intent: str | None
    actual_intent: str | None
    correct: bool
    requires_tool: bool
    expected_tools: tuple[str, ...]
    selected_tools: tuple[str, ...]
    required_tool_missed: bool
    adversarial: bool
    adversarial_unsafe: bool
    confidence: float | None
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cost: float


@dataclass(frozen=True, slots=True)
class LabelMetrics:
    precision: float
    recall: float
    f1: float
    support: int


@dataclass(frozen=True, slots=True)
class RouterEvaluationReport:
    dataset_name: str
    model_version: str
    results: tuple[RouterCaseResult, ...]
    confusion_matrix: Mapping[str, Mapping[str, int]]
    per_label: Mapping[str, LabelMetrics]
    accuracy: float
    required_tool_cases: int
    required_tool_misses: int
    required_tool_miss_rate: float
    adversarial_cases: int
    adversarial_unsafe: int
    adversarial_attack_success_rate: float
    average_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    total_input_tokens: int
    total_output_tokens: int
    total_cost: float

    def result_by_id(self) -> dict[str, RouterCaseResult]:
        return {result.case_id: result for result in self.results}


class RouterEvaluator:
    """对任意具有 ``route(prompt)`` 的 Router 执行离线数据集。"""

    def __init__(self, router: Any, *, model_version: str) -> None:
        if not model_version.strip():
            raise RouterEvaluationError("model_version 不能为空")
        self.router = router
        self.model_version = model_version

    async def evaluate(
        self,
        dataset: RouterEvaluationDataset,
    ) -> RouterEvaluationReport:
        results: list[RouterCaseResult] = []
        for case in dataset.cases:
            started = time.perf_counter()
            value = self.router.route(case.prompt)
            resolved = await cast(Awaitable[Any], value) if inspect.isawaitable(value) else value
            latency_ms = (time.perf_counter() - started) * 1000
            observation = (
                resolved
                if isinstance(resolved, RouterObservation)
                else _observation_from_router(self.router, resolved)
            )
            if not isinstance(observation.decision, RequestDecision):
                raise RouterEvaluationError("Router 必须返回 RequestDecision 或 RouterObservation")
            decision = observation.decision
            expected_label = _expected_label(case)
            actual_label = _actual_label(case, decision)
            correct = (
                (case.expected_status is None or decision.status == case.expected_status)
                and (case.expected_intent is None or decision.intent == case.expected_intent)
            )
            required_tool_missed = case.requires_tool and (
                decision.tool_policy.mode not in {"required", "named"}
                or decision.status
                not in {"in_scope_tool_ready", "in_scope_approval_required"}
                or not decision.selected_tools
                or (
                    decision.tool_policy.mode == "named"
                    and decision.tool_policy.tool_name not in decision.selected_tools
                )
                or not set(case.expected_tools).issubset(decision.selected_tools)
            )
            adversarial_unsafe = case.adversarial and decision.status not in case.safe_statuses
            results.append(
                RouterCaseResult(
                    case_id=case.case_id,
                    expected_label=expected_label,
                    actual_label=actual_label,
                    expected_status=case.expected_status,
                    actual_status=decision.status,
                    expected_intent=case.expected_intent,
                    actual_intent=decision.intent,
                    correct=correct,
                    requires_tool=case.requires_tool,
                    expected_tools=case.expected_tools,
                    selected_tools=tuple(decision.selected_tools),
                    required_tool_missed=required_tool_missed,
                    adversarial=case.adversarial,
                    adversarial_unsafe=adversarial_unsafe,
                    confidence=decision.confidence,
                    latency_ms=latency_ms,
                    input_tokens=observation.input_tokens,
                    output_tokens=observation.output_tokens,
                    cost=float(observation.cost),
                )
            )
        return _build_report(dataset.name, self.model_version, results)


@dataclass(frozen=True, slots=True)
class ConfidenceSample:
    confidence: float
    correct: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0 <= float(self.confidence) <= 1
        ):
            raise RouterEvaluationError("confidence 必须在 0 到 1 之间")
        if not isinstance(self.correct, bool):
            raise RouterEvaluationError("correct 必须是布尔值")


@dataclass(frozen=True, slots=True)
class ConfidenceCalibration:
    threshold: float
    precision: float
    recall: float
    f1: float
    accepted: int
    rejected: int
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int


def calibrate_confidence_threshold(
    samples: Sequence[ConfidenceSample],
    *,
    candidate_thresholds: Sequence[float] | None = None,
) -> ConfidenceCalibration:
    """选择接受分类的 F1 最优阈值；同分时偏向更高阈值。"""

    if not samples:
        raise RouterEvaluationError("阈值校准样本不能为空")
    candidates = (
        sorted(set(float(value) for value in candidate_thresholds))
        if candidate_thresholds is not None
        else sorted({0.0, 1.0, *(float(item.confidence) for item in samples)})
    )
    if not candidates or any(not math.isfinite(value) or not 0 <= value <= 1 for value in candidates):
        raise RouterEvaluationError("候选阈值必须在 0 到 1 之间")
    reports = [_calibration_at(samples, threshold) for threshold in candidates]
    return max(
        reports,
        key=lambda item: (item.f1, item.precision, item.threshold),
    )


@dataclass(frozen=True, slots=True)
class RouterRegressionReport:
    baseline_version: str
    candidate_version: str
    passed: bool
    accuracy_delta: float
    required_tool_miss_rate_delta: float
    adversarial_attack_rate_delta: float
    average_latency_delta_ms: float
    total_cost_delta: float
    changed_cases: Mapping[str, tuple[str, str]]
    added_cases: tuple[str, ...]
    removed_cases: tuple[str, ...]
    violations: tuple[str, ...]


def compare_router_reports(
    baseline: RouterEvaluationReport,
    candidate: RouterEvaluationReport,
    *,
    max_accuracy_drop: float = 0.0,
    max_required_tool_miss_rate_increase: float = 0.0,
    max_adversarial_attack_rate_increase: float = 0.0,
    max_average_latency_increase_ms: float = math.inf,
    max_total_cost_increase: float = math.inf,
) -> RouterRegressionReport:
    """比较两个模型版本，并生成可作为 CI 门禁的结果。"""

    for name, value in (
        ("max_accuracy_drop", max_accuracy_drop),
        ("max_required_tool_miss_rate_increase", max_required_tool_miss_rate_increase),
        ("max_adversarial_attack_rate_increase", max_adversarial_attack_rate_increase),
        ("max_average_latency_increase_ms", max_average_latency_increase_ms),
        ("max_total_cost_increase", max_total_cost_increase),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or math.isnan(float(value))
            or value < 0
        ):
            raise RouterEvaluationError(f"{name} 不能小于 0")
    for name, value in (
        ("max_accuracy_drop", max_accuracy_drop),
        ("max_required_tool_miss_rate_increase", max_required_tool_miss_rate_increase),
        ("max_adversarial_attack_rate_increase", max_adversarial_attack_rate_increase),
    ):
        if value > 1:
            raise RouterEvaluationError(f"{name} 不能大于 1")
    baseline_cases = baseline.result_by_id()
    candidate_cases = candidate.result_by_id()
    added_cases = tuple(sorted(set(candidate_cases) - set(baseline_cases)))
    removed_cases = tuple(sorted(set(baseline_cases) - set(candidate_cases)))
    shared = set(baseline_cases) & set(candidate_cases)
    changed = {
        case_id: (
            baseline_cases[case_id].actual_label,
            candidate_cases[case_id].actual_label,
        )
        for case_id in sorted(shared)
        if baseline_cases[case_id].actual_label
        != candidate_cases[case_id].actual_label
    }
    accuracy_delta = candidate.accuracy - baseline.accuracy
    required_delta = (
        candidate.required_tool_miss_rate - baseline.required_tool_miss_rate
    )
    adversarial_delta = (
        candidate.adversarial_attack_success_rate
        - baseline.adversarial_attack_success_rate
    )
    latency_delta = candidate.average_latency_ms - baseline.average_latency_ms
    cost_delta = candidate.total_cost - baseline.total_cost
    violations: list[str] = []
    if baseline.dataset_name != candidate.dataset_name:
        violations.append("dataset_name_mismatch")
    if added_cases or removed_cases:
        violations.append("case_set_changed")
    if accuracy_delta < -max_accuracy_drop:
        violations.append("accuracy_drop")
    if required_delta > max_required_tool_miss_rate_increase:
        violations.append("required_tool_miss_rate_increase")
    if adversarial_delta > max_adversarial_attack_rate_increase:
        violations.append("adversarial_attack_rate_increase")
    if latency_delta > max_average_latency_increase_ms:
        violations.append("latency_increase")
    if cost_delta > max_total_cost_increase:
        violations.append("cost_increase")
    return RouterRegressionReport(
        baseline_version=baseline.model_version,
        candidate_version=candidate.model_version,
        passed=not violations,
        accuracy_delta=accuracy_delta,
        required_tool_miss_rate_delta=required_delta,
        adversarial_attack_rate_delta=adversarial_delta,
        average_latency_delta_ms=latency_delta,
        total_cost_delta=cost_delta,
        changed_cases=changed,
        added_cases=added_cases,
        removed_cases=removed_cases,
        violations=tuple(violations),
    )


def _build_report(
    dataset_name: str,
    model_version: str,
    results: list[RouterCaseResult],
) -> RouterEvaluationReport:
    labels = sorted(
        {result.expected_label for result in results}
        | {result.actual_label for result in results}
    )
    matrix: dict[str, dict[str, int]] = {
        expected: {actual: 0 for actual in labels} for expected in labels
    }
    for result in results:
        matrix[result.expected_label][result.actual_label] += 1
    per_label: dict[str, LabelMetrics] = {}
    for label in labels:
        true_positive = matrix[label][label]
        false_positive = sum(
            matrix[expected][label] for expected in labels if expected != label
        )
        false_negative = sum(
            matrix[label][actual] for actual in labels if actual != label
        )
        precision = _ratio(true_positive, true_positive + false_positive)
        recall = _ratio(true_positive, true_positive + false_negative)
        per_label[label] = LabelMetrics(
            precision=precision,
            recall=recall,
            f1=_f1(precision, recall),
            support=sum(matrix[label].values()),
        )
    required = [result for result in results if result.required_tool_missed]
    required_case_count = sum(result.requires_tool for result in results)
    adversarial = [result for result in results if result.adversarial_unsafe]
    adversarial_count = sum(result.adversarial for result in results)
    latencies = sorted(result.latency_ms for result in results)
    return RouterEvaluationReport(
        dataset_name=dataset_name,
        model_version=model_version,
        results=tuple(results),
        confusion_matrix=matrix,
        per_label=per_label,
        accuracy=_ratio(sum(result.correct for result in results), len(results)),
        required_tool_cases=required_case_count,
        required_tool_misses=len(required),
        required_tool_miss_rate=_ratio(len(required), required_case_count),
        adversarial_cases=adversarial_count,
        adversarial_unsafe=len(adversarial),
        adversarial_attack_success_rate=_ratio(len(adversarial), adversarial_count),
        average_latency_ms=statistics.fmean(latencies) if latencies else 0.0,
        p50_latency_ms=_percentile(latencies, 0.50),
        p95_latency_ms=_percentile(latencies, 0.95),
        total_input_tokens=sum(result.input_tokens for result in results),
        total_output_tokens=sum(result.output_tokens for result in results),
        total_cost=sum(result.cost for result in results),
    )


def _calibration_at(
    samples: Sequence[ConfidenceSample], threshold: float
) -> ConfidenceCalibration:
    true_positive = sum(item.correct and item.confidence >= threshold for item in samples)
    false_positive = sum(not item.correct and item.confidence >= threshold for item in samples)
    false_negative = sum(item.correct and item.confidence < threshold for item in samples)
    true_negative = sum(not item.correct and item.confidence < threshold for item in samples)
    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    accepted = true_positive + false_positive
    return ConfidenceCalibration(
        threshold=threshold,
        precision=precision,
        recall=recall,
        f1=_f1(precision, recall),
        accepted=accepted,
        rejected=len(samples) - accepted,
        true_positive=true_positive,
        false_positive=false_positive,
        false_negative=false_negative,
        true_negative=true_negative,
    )


def _expected_label(case: RouterEvaluationCase) -> str:
    if case.expected_intent is not None and case.expected_status is not None:
        return f"{case.expected_status}|{case.expected_intent}"
    return case.expected_intent or cast(str, case.expected_status)


def _actual_label(case: RouterEvaluationCase, decision: RequestDecision) -> str:
    if case.expected_intent is not None and case.expected_status is not None:
        return f"{decision.status}|{decision.intent or '<none>'}"
    if case.expected_intent is not None:
        return decision.intent or f"status:{decision.status}"
    return decision.status


def _observation_from_router(router: Any, decision: Any) -> RouterObservation:
    """Read optional measured metrics without changing the Router result contract."""

    metrics_fn = getattr(router, "evaluation_metrics", None)
    metrics = metrics_fn() if callable(metrics_fn) else None
    if metrics is None:
        return RouterObservation(decision=decision)
    if not isinstance(metrics, Mapping):
        raise RouterEvaluationError("Router evaluation_metrics() 必须返回对象")
    allowed = {"input_tokens", "output_tokens", "cost"}
    if set(metrics) - allowed:
        raise RouterEvaluationError("Router evaluation_metrics() 返回了未知字段")
    return RouterObservation(
        decision=decision,
        input_tokens=metrics.get("input_tokens", 0),
        output_tokens=metrics.get("output_tokens", 0),
        cost=metrics.get("cost", 0.0),
    )


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    position = max(0, math.ceil(percentile * len(values)) - 1)
    return values[position]


def _required_text(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise RouterEvaluationError(f"{key} 必须是非空字符串")
    return result


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RouterEvaluationError("可选文本字段必须是非空字符串或 null")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RouterEvaluationError(f"{name} 必须是对象")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise RouterEvaluationError(f"{name} 必须是布尔值")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RouterEvaluationError(f"{name} 必须是整数")
    return value


def _text_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise RouterEvaluationError(f"{name} 必须是非空字符串数组")
    return list(value)


def _strict_json(value: Any, name: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RouterEvaluationError(f"{name} 必须是严格 JSON：{error}") from error
