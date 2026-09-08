"""Durable child sessions, inbox delivery and conservatively shared budgets.

The journal reserves each message's entire allocation before admitting it. An
allocation is never recycled automatically, including failed/unknown calls. This
makes the aggregate ceiling valid across restarts and concurrent controllers.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4

from .cancellation import CancellationToken
from .context import content_digest
from .harness.durable_agent_host import DurableAgentHost
from .harness.session_runtime import SessionWriterBusyError
from .planning import ClosedLoopBudget
from .session.journal import JournalConflictError, JournalPrincipal, SessionEventJournal
from .session.operation_store import ClaimLease
from .session.records import JournalRecordStore


@dataclass(frozen=True, slots=True)
class ChildSessionRequest:
    parent_session_id: str
    session_id: str
    tenant_id: str
    child_id: str
    message_id: str
    prompt: str
    budget: ClosedLoopBudget
    deadline_at: float


class DurableChildSessionManager:
    """Trusted factories create fully governed Hosts, never raw worker callbacks.

    A factory receives ChildSessionRequest and must bind its session, tenant and
    budget exactly. Reopening uses the same state directory/keys and session
    configuration. Approval still goes through the child Host's verified APIs.
    """

    def __init__(
        self,
        journal: SessionEventJournal,
        principal: JournalPrincipal,
        parent_session_id: str,
        *,
        host_factories: Mapping[str, Callable[..., Any]],
        budget: ClosedLoopBudget,
        version: str,
        max_children: int = 16,
        max_messages: int = 64,
        lease_seconds: float = 30,
    ) -> None:
        if not host_factories or any(
            not isinstance(name, str) or not name or not callable(factory)
            for name, factory in host_factories.items()
        ):
            raise ValueError("child factories must be an explicit non-empty mapping")
        if (
            not version
            or type(max_children) is not int
            or not 1 <= max_children <= 128
            or type(max_messages) is not int
            or not 1 <= max_messages <= 256
        ):
            raise ValueError("invalid child manager configuration")
        if budget.max_duration_seconds is None or budget.max_model_calls is None:
            raise ValueError(
                "child orchestration requires finite duration and model-call ceilings"
            )
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 300:
            raise ValueError("child lease must be between 1 and 300 seconds")
        self.records = JournalRecordStore(
            journal, principal, parent_session_id, "child_sessions"
        )
        self.factories = dict(host_factories)
        self.budget = budget
        self.max_children = max_children
        self.max_messages = max_messages
        self.lease_seconds = lease_seconds
        self.version = content_digest(
            {
                "version": version,
                "factories": sorted(host_factories),
                "budget": asdict(budget),
                "children": max_children,
                "messages": max_messages,
            }
        )
        self._active: dict[asyncio.Task[Any], CancellationToken] = {}
        self._closed = False

    async def _state(self) -> tuple[dict[str, Any], int]:
        events = await self.records.read("inbox")
        if not events:
            return {
                "version": self.version,
                "deadlineAt": time.time()
                + float(self.budget.max_duration_seconds or 0),
                "messages": [],
            }, -1
        value = copy.deepcopy(events[-1].payload)
        if value.get("version") != self.version:
            raise ValueError(
                "child configuration changed; explicit migration is required"
            )
        return value, events[-1].sequence

    async def _change(
        self,
        change: Callable[[dict[str, Any]], Any],
        *,
        claim: ClaimLease | None = None,
    ) -> Any:
        for _ in range(32):
            state, sequence = await self._state()
            before = content_digest(state)
            result = change(state)
            if content_digest(state) == before and sequence >= 0:
                return result
            try:
                await self.records.append(
                    "inbox",
                    "child_inbox_updated",
                    state,
                    expected_version=sequence,
                    claim=claim,
                    lease_seconds=self.lease_seconds,
                )
                return result
            except JournalConflictError:
                if (
                    claim is not None
                    and not await self.records.journal.verify_fenced_claim(
                        self.records.principal, claim
                    )
                ):
                    raise JournalConflictError(
                        "child controller fenced claim is no longer valid"
                    ) from None
                await asyncio.sleep(0)
        raise JournalConflictError("child inbox remained contended")

    async def enqueue(
        self,
        child_id: str,
        message_id: str,
        prompt: str,
        *,
        factory: str,
        allocation: ClosedLoopBudget,
    ) -> str:
        if self._closed:
            raise RuntimeError("child manager is closed")
        self.records.operation_id(child_id)
        self.records.operation_id(message_id)
        if (
            factory not in self.factories
            or not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt.encode()) > 16_384
        ):
            raise ValueError("unknown factory or invalid child prompt")
        if (
            allocation.max_model_calls is None
            or allocation.max_duration_seconds is None
        ):
            raise ValueError("child allocation needs finite model calls and duration")
        session_id = "child-" + content_digest(
            {"parent": self.records.session_id, "child": child_id}
        )
        message = {
            "childId": child_id,
            "messageId": message_id,
            "sessionId": session_id,
            "factory": factory,
            "prompt": prompt,
            "allocation": asdict(allocation),
        }

        def admit(state: dict[str, Any]) -> str:
            messages = state["messages"]
            existing = next(
                (item for item in messages if item["messageId"] == message_id), None
            )
            if existing is not None:
                if any(existing[key] != value for key, value in message.items()):
                    raise ValueError("message_id is already bound to different work")
                return existing["sessionId"]
            if time.time() >= state["deadlineAt"]:
                raise TimeoutError("parent task deadline expired")
            siblings = [item for item in messages if item["childId"] == child_id]
            if siblings and any(
                item["factory"] != factory or item["allocation"] != asdict(allocation)
                for item in siblings
            ):
                raise ValueError(
                    "a child session must keep its factory and budget configuration"
                )
            if (
                len(messages) >= self.max_messages
                or len({item["childId"] for item in messages} | {child_id})
                > self.max_children
            ):
                raise ValueError("child inbox capacity exceeded")
            if (
                sum(len(item["prompt"].encode()) for item in messages)
                + len(prompt.encode())
                > 512 * 1024
            ):
                raise ValueError("child inbox input capacity exceeded")
            for name in (
                "max_plan_steps",
                "max_step_attempts",
                "max_tool_calls",
                "max_model_calls",
                "max_tokens",
                "max_cost",
            ):
                ceiling = getattr(self.budget, name)
                requested = getattr(allocation, name)
                if ceiling is not None and (
                    requested is None
                    or requested + sum(item["allocation"][name] for item in messages)
                    > ceiling
                ):
                    raise ValueError(f"shared child budget exceeded: {name}")
            messages.append(
                {
                    **message,
                    "status": "queued",
                    "cancelRequested": False,
                    "acknowledged": False,
                    "result": None,
                }
            )
            return session_id

        return await self._change(admit)

    async def status(self) -> dict[str, Any]:
        state, _ = await self._state()
        return state

    async def results(
        self, *, include_acknowledged: bool = False
    ) -> tuple[dict[str, Any], ...]:
        state, _ = await self._state()
        return tuple(
            {
                "messageId": item["messageId"],
                "childId": item["childId"],
                "sessionId": item["sessionId"],
                **copy.deepcopy(item["result"]),
            }
            for item in state["messages"]
            if item["result"] is not None
            and (include_acknowledged or not item["acknowledged"])
        )

    async def acknowledge(self, message_id: str, delivery_id: str) -> None:
        def update(state: dict[str, Any]) -> None:
            item = self._message(state, message_id)
            if item["result"] is None or item["result"]["deliveryId"] != delivery_id:
                raise ValueError("unknown child result delivery")
            item["acknowledged"] = True

        await self._change(update)

    async def cancel(self, child_id: str | None = None) -> None:
        def update(state: dict[str, Any]) -> None:
            for item in state["messages"]:
                if (child_id is None or item["childId"] == child_id) and item[
                    "status"
                ] not in {"completed", "failed", "cancelled", "manual_intervention"}:
                    item["cancelRequested"] = True

        await self._change(update)
        # A running controller also polls this durable bit; another process
        # receives cancellation without relying on a shared Python token.

    @staticmethod
    def _message(state: dict[str, Any], message_id: str) -> dict[str, Any]:
        for item in state["messages"]:
            if item["messageId"] == message_id:
                return item
        raise KeyError(message_id)

    async def run_next(
        self, child_id: str, *, cancellation: CancellationToken | None = None
    ) -> dict[str, Any] | None:
        if self._closed:
            raise RuntimeError("child manager is closed")
        self.records.operation_id(child_id)
        token = cancellation or CancellationToken()
        token.throw_if_cancelled()
        task = asyncio.current_task()
        assert task is not None
        self._active[task] = token
        journal, principal = self.records.journal, self.records.principal
        claim = None
        monitor = None
        host = None
        try:
            resource = content_digest(
                {"parent": self.records.session_id, "child": child_id}
            )
            claim = await journal.acquire_fenced_claim(
                principal,
                "durable_child",
                resource,
                str(uuid4()),
                lease_seconds=self.lease_seconds,
            )
            if claim is None:
                return None
            state, _ = await self._state()
            item = next(
                (
                    item
                    for item in state["messages"]
                    if item["childId"] == child_id
                    and item["status"]
                    not in {"completed", "failed", "cancelled", "manual_intervention"}
                ),
                None,
            )
            if item is None:
                return None
            message_id = item["messageId"]
            if item["cancelRequested"] or time.time() >= state["deadlineAt"]:
                return await self._finish(
                    message_id,
                    {
                        "status": "cancelled",
                        "responseText": "Child work cancelled before dispatch.",
                        "planId": None,
                        "approvalIds": [],
                    },
                    claim,
                )
            request = ChildSessionRequest(
                self.records.session_id,
                item["sessionId"],
                principal.tenant_id,
                child_id,
                message_id,
                item["prompt"],
                ClosedLoopBudget(**item["allocation"]),
                state["deadlineAt"],
            )

            async def renew() -> None:
                while True:
                    await asyncio.sleep(min(0.25, self.lease_seconds / 3))
                    if not await journal.renew_fenced_claim(
                        principal, claim, lease_seconds=self.lease_seconds
                    ):
                        token.cancel("child controller lease lost")
                        task.cancel("child controller lease lost")
                        return
                    current, _ = await self._state()
                    if (
                        self._message(current, message_id)["cancelRequested"]
                        or time.time() >= current["deadlineAt"]
                    ):
                        token.cancel("child work cancelled or parent deadline expired")
                        task.cancel("child work cancelled or parent deadline expired")
                        return

            async def guarded_renew() -> None:
                try:
                    await renew()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    token.cancel("child controller renewal failed")
                    task.cancel("child controller renewal failed")

            monitor = asyncio.create_task(guarded_renew(), name="durable-child-lease")
            async with asyncio.timeout(max(0.001, request.deadline_at - time.time())):
                try:
                    value = self.factories[item["factory"]](request)
                    host = await value if inspect.isawaitable(value) else value
                except SessionWriterBusyError:
                    # Parent and child writer leases renew independently. A
                    # crashed parent's claim can expire before the child writer
                    # claim. Keep the inbox pending until both permit admission.
                    # Unsupported stores and other startup failures still raise.
                    return None
                self._validate_host(host, request)
                result = await host.submit_task(
                    message_id, request.prompt, cancellation=token
                )
                autonomous = result.autonomous_result
                if autonomous is None:
                    raise RuntimeError(
                        "child Host returned no durable autonomous result"
                    )
                return await self._finish(
                    message_id,
                    {
                        "status": autonomous.status,
                        "responseText": autonomous.response_text,
                        "planId": autonomous.plan_id,
                        "approvalIds": list(autonomous.pending_approval_ids),
                    },
                    claim,
                )
        finally:

            async def cleanup() -> None:
                if monitor is not None:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                try:
                    if host is not None:
                        await host.close()
                finally:
                    if claim is not None:
                        await journal.release_fenced_claim(principal, claim)

            closing = asyncio.create_task(cleanup(), name="durable-child-cleanup")
            cancelled = False
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    cancelled = True
            self._active.pop(task, None)
            closing.result()
            if cancelled:
                raise asyncio.CancelledError

    def _validate_host(self, host: Any, request: ChildSessionRequest) -> None:
        if not isinstance(host, DurableAgentHost) or not host.general_task_mode:
            raise TypeError("child factory must return a general DurableAgentHost")
        if (
            host.session_id != request.session_id
            or host.agent.tenant_id != request.tenant_id
        ):
            raise ValueError(
                "child Host tenant/session does not match its durable descriptor"
            )
        runner = host.autonomous_plan_runner
        if (
            runner is None
            or runner.budget != request.budget
            or host.session_writer_lease is None
            or host.session_metadata is None
        ):
            raise ValueError(
                "child Host needs the exact reserved budget and an exclusive session writer"
            )

    async def _finish(
        self, message_id: str, result: dict[str, Any], claim: ClaimLease
    ) -> dict[str, Any]:
        if len(result["responseText"].encode()) > 65_536:
            raise ValueError(
                "child result exceeds mailbox limit; return an artifact reference"
            )
        result = {
            **result,
            "deliveryId": content_digest(
                {
                    "parent": self.records.session_id,
                    "message": message_id,
                    "result": result,
                }
            ),
        }

        def update(state: dict[str, Any]) -> dict[str, Any]:
            item = self._message(state, message_id)
            if item["result"] is not None and item["status"] in {
                "completed",
                "failed",
                "cancelled",
                "manual_intervention",
            }:
                if item["result"] != result:
                    raise ValueError("terminal child result cannot be replaced")
                return copy.deepcopy(item["result"])
            item["result"] = result
            item["status"] = result["status"]
            item["acknowledged"] = False
            return copy.deepcopy(result)

        return await self._change(update, claim=claim)

    async def aclose(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        tasks = tuple(task for task in self._active if task is not current)
        for task in tasks:
            task.cancel("child manager shutting down; durable inbox can resume")
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class DurableHostWorker:
    """Adapter for the existing MultiAgentOrchestrator Worker interface."""

    def __init__(
        self,
        manager: DurableChildSessionManager,
        factory: str,
        allocation: ClosedLoopBudget,
    ) -> None:
        self.manager = manager
        self.factory = factory
        self.allocation = allocation

    async def __call__(self, request: Any) -> str:
        if request.tenant_id != self.manager.records.principal.tenant_id:
            raise PermissionError("worker request tenant does not match child manager")
        key = content_digest(
            {
                "run": request.run_id,
                "task": request.task_id,
                "replica": request.replica_index,
            }
        )
        prompt = json.dumps(
            {
                "task": request.prompt,
                "dependencyResults": {
                    name: {"status": value.status, "output": value.output}
                    for name, value in sorted(request.dependency_results.items())
                },
            },
            ensure_ascii=False,
        )
        await self.manager.enqueue(
            key, key, prompt, factory=self.factory, allocation=self.allocation
        )
        result = await self.manager.run_next(key, cancellation=request.cancellation)
        if result is None:
            result = next(
                (
                    item
                    for item in await self.manager.results(include_acknowledged=True)
                    if item["messageId"] == key
                ),
                None,
            )
        if result is None or result["status"] != "completed":
            raise RuntimeError(
                "child work is pending, suspended or requires intervention"
            )
        return result["responseText"]
