"""Restricted, deterministic Plan dataflow and condition evaluation."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, cast

from .types import (
    PlanComparisonOperator,
    PlanExecutionError,
    PlanExecutionState,
    PlanResultContract,
    PlanResultReference,
    PlanStep,
    PlanValidationError,
)


@dataclass(frozen=True, slots=True)
class PlanConditionEvaluation:
    passed: bool
    description: str


def dependency_results(
    step: PlanStep,
    state: PlanExecutionState,
) -> dict[str, Any]:
    """Return deep-copied results for *direct* successful dependencies only."""

    results: dict[str, Any] = {}
    for step_id in step.depends_on:
        current = state.steps.get(step_id)
        if current is None or current.status != "succeeded":
            raise PlanExecutionError(
                f"Plan Step {step.step_id} 依赖尚未成功：{step_id}"
            )
        results[step_id] = copy.deepcopy(current.result)
    return results


def resolve_step_arguments(
    step: PlanStep,
    results: Mapping[str, Any],
) -> dict[str, Any]:
    resolved = copy.deepcopy(dict(step.arguments))
    for binding in step.argument_bindings:
        if binding.source.step_id not in step.depends_on:
            raise PlanExecutionError(
                f"Plan Step {step.step_id} 试图读取未声明依赖："
                f"{binding.source.step_id}"
            )
        resolved[binding.target] = resolve_reference(binding.source, results)
    contract = step.parameter_contract
    unsafe = step.write or step.replay_policy == "never"
    if contract is None:
        if unsafe and not resolved:
            raise PlanValidationError(
                f"Plan Step {step.step_id} 是 write/never 操作但没有可派发参数"
            )
    else:
        contract.validate(
            resolved,
            owner=f"Plan Step {step.step_id} resolved arguments",
            require_non_empty=unsafe,
        )
    _strict_json(resolved, "resolved arguments")
    return resolved


def evaluate_preconditions(
    step: PlanStep,
    results: Mapping[str, Any],
) -> tuple[PlanConditionEvaluation, ...]:
    evaluations: list[PlanConditionEvaluation] = []
    for index, condition in enumerate(step.preconditions, start=1):
        left = _resolve_declared(condition.left, step, results)
        right = (
            _resolve_declared(condition.expected_from, step, results)
            if condition.expected_from is not None
            else copy.deepcopy(condition.expected)
        )
        passed = compare_json(left, condition.operator, right)
        evaluations.append(
            PlanConditionEvaluation(
                passed,
                f"condition#{index}:{condition.operator}",
            )
        )
    return tuple(evaluations)


def validate_step_result(result: Any, contract: PlanResultContract | None) -> None:
    _strict_json(result, "Plan Step Result")
    if contract is None:
        return
    for index, rule in enumerate(contract.rules, start=1):
        actual = resolve_json_path(result, rule.path)
        if not compare_json(actual, rule.operator, rule.expected):
            raise PlanExecutionError(
                f"Plan Step postcondition #{index} 未通过：{rule.operator}"
            )


def resolve_reference(
    reference: PlanResultReference,
    results: Mapping[str, Any],
) -> Any:
    if reference.step_id not in results:
        raise PlanExecutionError(
            f"Plan Result Reference 不可用：{reference.step_id}"
        )
    return copy.deepcopy(resolve_json_path(results[reference.step_id], reference.path))


def resolve_json_path(value: Any, path: tuple[str | int, ...]) -> Any:
    current = value
    for part in path:
        if isinstance(part, str):
            if not isinstance(current, Mapping) or part not in current:
                raise PlanExecutionError(f"Plan JSON Path 缺少字段：{part}")
            current = current[part]
            continue
        if (
            isinstance(part, bool)
            or not isinstance(part, int)
            or not isinstance(current, list)
            or part < 0
            or part >= len(current)
        ):
            raise PlanExecutionError(f"Plan JSON Path 数组下标无效：{part}")
        current = current[part]
    return copy.deepcopy(current)


def compare_json(left: Any, operator: PlanComparisonOperator, right: Any) -> bool:
    if operator == "truthy":
        return bool(left)
    if operator == "falsy":
        return not bool(left)
    if operator == "eq":
        return _canonical_json(left) == _canonical_json(right)
    if operator == "ne":
        return _canonical_json(left) != _canonical_json(right)
    if operator in {"in", "not_in"}:
        if not isinstance(right, list):
            raise PlanExecutionError("Plan in/not_in 的右操作数必须是 JSON 数组")
        contains = any(
            _canonical_json(left) == _canonical_json(item) for item in right
        )
        return contains if operator == "in" else not contains
    left_number = _number(left)
    right_number = _number(right)
    if left_number is not None and right_number is not None:
        if operator == "gt":
            return left_number > right_number
        if operator == "gte":
            return left_number >= right_number
        if operator == "lt":
            return left_number < right_number
        if operator == "lte":
            return left_number <= right_number
    elif isinstance(left, str) and isinstance(right, str):
        if operator == "gt":
            return left > right
        if operator == "gte":
            return left >= right
        if operator == "lt":
            return left < right
        if operator == "lte":
            return left <= right
    else:
        raise PlanExecutionError(
            f"Plan {operator} 只允许比较两个有限数字或两个字符串"
        )
    raise PlanExecutionError(f"未知 Plan comparison operator：{operator}")


def result_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _resolve_declared(
    reference: PlanResultReference,
    step: PlanStep,
    results: Mapping[str, Any],
) -> Any:
    if reference.step_id not in step.depends_on:
        raise PlanExecutionError(
            f"Plan Step {step.step_id} 条件读取了未声明依赖：{reference.step_id}"
        )
    return resolve_reference(reference, results)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise PlanExecutionError(f"Plan 值不是严格 JSON：{error}") from error


def _strict_json(value: Any, owner: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise PlanExecutionError(f"{owner} 必须是严格 JSON：{error}") from error


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        raise PlanExecutionError("Plan 数值比较不能包含 NaN 或 Infinity")
    return cast(int | float, value)


__all__ = [
    "PlanConditionEvaluation",
    "compare_json",
    "dependency_results",
    "evaluate_preconditions",
    "resolve_json_path",
    "resolve_reference",
    "resolve_step_arguments",
    "result_digest",
    "validate_step_result",
]
