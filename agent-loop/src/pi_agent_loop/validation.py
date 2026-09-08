"""Task acceptance contracts checked against durable tool and artifact evidence."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from .artifacts import Artifact, ArtifactStore
from .cancellation import CancellationToken
from .context import content_digest
from .planning.closed_loop import ResultValidation


@dataclass(frozen=True, slots=True)
class ArtifactRequirement:
    name: str
    media_type: str = "text/markdown"
    minimum_bytes: int = 1
    required_sections: tuple[str, ...] = ()
    minimum_citations: int = 0
    required_json_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("artifact requirement name is required")
        if (
            type(self.minimum_bytes) is not int
            or self.minimum_bytes < 1
            or type(self.minimum_citations) is not int
            or self.minimum_citations < 0
        ):
            raise ValueError("artifact requirement limits are invalid")
        for values in (self.required_sections, self.required_json_keys):
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise ValueError(
                    "artifact requirement fields must be non-empty strings"
                )


@dataclass(frozen=True, slots=True)
class TaskContract:
    artifacts: tuple[ArtifactRequirement, ...] = ()
    required_tools: tuple[str, ...] = ()
    version: str = "1"
    max_repeated_observations: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("task contract version is required")
        if (
            type(self.max_repeated_observations) is not int
            or self.max_repeated_observations < 2
        ):
            raise ValueError("repeat threshold must be at least two")
        if len({item.name for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("duplicate artifact requirements")
        if any(
            not isinstance(name, str) or not name.strip()
            for name in self.required_tools
        ):
            raise ValueError("required_tools must contain stable tool names")

    @property
    def fingerprint(self) -> str:
        return content_digest(asdict(self))


def observation_results(observation: Any) -> list[dict[str, Any]]:
    """Extract results from persisted steps; assistant prose is not evidence."""
    results = []
    plans = {plan.plan_id: plan for plan in observation.plans}
    for execution in observation.executions:
        plan = plans[execution.state.plan_id]
        for step in plan.steps:
            state = execution.state.steps.get(step.step_id)
            if state is not None:
                results.append(
                    {
                        "intent": step.intent,
                        "arguments": dict(step.arguments),
                        "status": state.status,
                        "result": state.result,
                        "error": state.error,
                    }
                )
    return results


def artifact_ids(observation: Any) -> tuple[str, ...]:
    identifiers = []
    for item in observation_results(observation):
        if item["status"] != "succeeded" or not isinstance(item["result"], dict):
            continue
        details = item["result"].get("details")
        if isinstance(details, dict) and isinstance(details.get("artifactId"), str):
            identifiers.append(details["artifactId"])
    return tuple(dict.fromkeys(identifiers))


def _stable_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_result(item)
            for key, item in value.items()
            if key
            not in {"toolCallId", "requestId", "timestamp", "durationMs", "elapsedMs"}
        }
    if isinstance(value, list):
        return [_stable_result(item) for item in value]
    return value


class TaskResultValidator:
    def __init__(
        self,
        contract: TaskContract,
        artifacts: ArtifactStore,
        *,
        citation_resolver: Callable[..., Any] | None = None,
        checks: tuple[Callable[..., Any], ...] = (),
    ) -> None:
        if any(not callable(check) for check in checks):
            raise ValueError("task checks must be callable")
        if (
            any(item.minimum_citations for item in contract.artifacts)
            and citation_resolver is None
        ):
            raise ValueError("citation requirements need a trusted citation resolver")
        if not contract.artifacts and not checks:
            raise ValueError(
                "task completion requires an artifact contract or a trusted result check"
            )
        self.contract = contract
        self.artifacts = artifacts
        self.citation_resolver = citation_resolver
        self.checks = checks
        self.model_free = not checks

    async def __call__(
        self, observation: Any, cancellation: CancellationToken
    ) -> ResultValidation:
        cancellation.throw_if_cancelled()
        latest = observation.latest_execution
        if latest is not None and latest.state.phase == "waiting_approval":
            return ResultValidation.suspended("waiting for approval")
        if latest is not None and latest.state.phase == "manual_intervention":
            return ResultValidation.unknown(
                "execution requires reconciliation or manual intervention"
            )
        issues: list[str] = []
        results = observation_results(observation)
        successful = {
            item["intent"].removeprefix("tool.")
            for item in results
            if item["status"] == "succeeded"
        }
        for name in self.contract.required_tools:
            if name not in successful:
                issues.append("required tool evidence missing: " + name)
        available: dict[str, list[Artifact]] = {}
        for identifier in artifact_ids(observation):
            try:
                artifact = await self.artifacts.get(identifier)
            except (KeyError, ValueError):
                issues.append("artifact evidence missing or invalid")
                continue
            available.setdefault(artifact.name, []).append(artifact)
        accepted = []
        for requirement in self.contract.artifacts:
            candidates = available.get(requirement.name, [])
            if not candidates:
                issues.append("required artifact missing: " + requirement.name)
                continue
            artifact = candidates[-1]
            artifact_issues = await self._verify_artifact(
                requirement, artifact, cancellation
            )
            issues.extend(artifact_issues)
            if not artifact_issues:
                accepted.append(artifact.artifact_id)
        for check in self.checks:
            cancellation.throw_if_cancelled()
            pending = check(observation, cancellation)
            result = await pending if inspect.isawaitable(pending) else pending
            if not isinstance(result, ResultValidation):
                raise TypeError("task check must return ResultValidation")
            if result.status in {"outcome_unknown", "suspended"}:
                return result
            issues.extend(result.issues)
        if not issues and latest is not None and latest.state.phase == "completed":
            return ResultValidation.valid(
                details={"contract": self.contract.fingerprint, "artifacts": accepted}
            )
        threshold = self.contract.max_repeated_observations
        if len(results) >= threshold:
            fingerprints = [
                content_digest(_stable_result(item)) for item in results[-threshold:]
            ]
            if len(set(fingerprints)) == 1:
                return ResultValidation.unknown(
                    "no progress: identical action and observation repeated",
                    details={"repeatCount": threshold},
                )
        return ResultValidation.invalid(
            *(issues or ["execution did not reach a verified result"]),
            details={"contract": self.contract.fingerprint},
        )

    async def _verify_artifact(
        self,
        requirement: ArtifactRequirement,
        artifact: Artifact,
        token: CancellationToken,
    ) -> list[str]:
        issues = []
        if (
            artifact.media_type != requirement.media_type
            or len(artifact.data) < requirement.minimum_bytes
        ):
            issues.append("artifact type or size is invalid: " + requirement.name)
        text = artifact.data.decode("utf-8", "replace")
        for section in requirement.required_sections:
            if section not in text:
                issues.append("artifact section missing: " + section)
        if requirement.required_json_keys:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            if not isinstance(payload, dict) or not set(
                requirement.required_json_keys
            ).issubset(payload):
                issues.append("artifact JSON contract failed: " + requirement.name)
        if len(artifact.citations) < requirement.minimum_citations:
            issues.append("artifact lacks required citations: " + requirement.name)
        if self.citation_resolver is not None:
            for citation in artifact.citations:
                token.throw_if_cancelled()
                pending = self.citation_resolver(citation, token)
                resolved = await pending if inspect.isawaitable(pending) else pending
                if resolved is None or resolved is False:
                    issues.append(
                        "citation is unavailable or no longer authorized: " + citation
                    )
        return issues


class ArtifactResultSynthesizer:
    model_free = True

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    async def __call__(
        self, observation: Any, status: str, cancellation: CancellationToken
    ) -> str:
        cancellation.throw_if_cancelled()
        if status != "completed":
            return "任务尚未通过验收，当前状态：" + status
        artifacts = [
            await self.store.get(identifier) for identifier in artifact_ids(observation)
        ]
        return "任务已通过配置的验收检查。\n" + "\n".join(
            f"{item.name}：{item.artifact_id}（{len(item.data)} 字节）"
            for item in artifacts
        )
