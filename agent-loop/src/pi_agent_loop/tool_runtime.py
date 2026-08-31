"""普通 Agent Loop 与崩溃恢复共用的工具执行边界。

该模块把参数校验、Hook、Retry、Timeout、取消、资源锁、租户限流和工具事件
收敛到一个 Runtime。调度器只持有本地协调状态；需要跨进程互斥时可注入
``SQLiteResourceLockBackend`` 或实现 ``ResourceLockBackend`` 协议。
"""

from __future__ import annotations

import asyncio
import copy
import heapq
import inspect
import sqlite3
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from .async_utils import durable_to_thread
from .cancellation import CancellationToken, OperationCancelledError
from .messages import clone_message, error_tool_result, now_ms
from .retry.errors import (
    DefinitelyNotCommittedToolError,
    OutcomeUnknownToolError,
    RetryableToolError,
)
from .retry.tool import execute_tool_with_retry
from .runtime.telemetry import Telemetry, TelemetrySpan
from .tool_contract import ToolSecurityContract, tool_callable_identity
from .types import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentContext,
    AgentEvent,
    AgentMessage,
    AgentTool,
    AgentToolResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
    EventSink,
    ToolAuthorization,
    ToolDispatchContext,
    UNSET,
)


class ToolDispatchError(RuntimeError):
    """工具 Runtime 自身的配置、授权或调度错误。"""


class ToolAttemptAdmissionDenied(ToolDispatchError):
    """A trusted attempt-admission boundary denied entry to the handler."""

    def __init__(self, result: AgentToolResult) -> None:
        if not isinstance(result, AgentToolResult):
            raise TypeError("Tool Attempt Admission 拒绝结果必须是 AgentToolResult")
        super().__init__("Tool Attempt Admission 已拒绝本次 Handler 执行")
        self.result = copy.deepcopy(result)


class ResourceLockTimeoutError(ToolDispatchError):
    pass


class ResourceLeaseLostError(ToolDispatchError):
    """The distributed lease disappeared while the tool was still executing."""

    def __init__(self, message: str, *, outcome_unknown: bool) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


class TenantContextError(ToolDispatchError):
    """Trusted tenant context is missing, unknown, or conflicts with arguments."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ResourceLockBackendCapabilities:
    """Topology and atomicity guarantees of a resource-lock adapter."""

    backend_name: str
    supports_cross_process: bool
    supports_multi_host: bool
    atomic_multi_resource_acquire: bool
    supports_lease_renewal: bool
    # Ordinary owner UUID + lease renewal is not fencing.  True means acquire
    # returns a monotonically increasing token that the downstream can reject.
    supports_fencing_tokens: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.backend_name, str) or not self.backend_name.strip():
            raise ValueError("Resource Lock backend_name 不能为空")
        for name in (
            "supports_cross_process",
            "supports_multi_host",
            "atomic_multi_resource_acquire",
            "supports_lease_renewal",
            "supports_fencing_tokens",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"Resource Lock {name} 必须是布尔值")
        if self.supports_multi_host and not self.supports_cross_process:
            raise ValueError("多机 Resource Lock 必须同时支持跨进程")


@dataclass(frozen=True, slots=True)
class ResourceFencingLease:
    """An acquired resource-lock generation safe to pass to a downstream."""

    owner_token: str
    fencing_token: int
    fencing_scope: str

    def __post_init__(self) -> None:
        if not isinstance(self.owner_token, str) or not self.owner_token.strip():
            raise ValueError("Resource Fencing owner_token 不能为空")
        if (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token < 1
        ):
            raise ValueError("Resource Fencing token 必须是正整数")
        if not isinstance(self.fencing_scope, str) or not self.fencing_scope.strip():
            raise ValueError("Resource Fencing scope 不能为空")


class ResourceLockBackend(Protocol):
    """可替换的跨进程资源锁后端。"""

    async def acquire(
        self,
        resource_keys: tuple[str, ...],
        *,
        owner_token: str,
        access: str,
        timeout_seconds: float,
    ) -> bool | ResourceFencingLease | None: ...

    async def renew(
        self,
        resource_keys: tuple[str, ...],
        *,
        owner_token: str,
    ) -> bool: ...

    async def release(
        self,
        resource_keys: tuple[str, ...],
        *,
        owner_token: str,
    ) -> None: ...


class SQLiteResourceLockBackend:
    """SQLite 单机跨进程读写锁，使用短事务和可续租 Lease。

    It deliberately does not claim multi-host safety.  Use a real shared lock
    service adapter for workers running on different machines.
    """

    capabilities = ResourceLockBackendCapabilities(
        backend_name="sqlite-resource-lock",
        supports_cross_process=True,
        supports_multi_host=False,
        atomic_multi_resource_acquire=True,
        supports_lease_renewal=True,
        supports_fencing_tokens=True,
    )

    def __init__(
        self,
        path: str | Path,
        *,
        lease_seconds: float = 30,
        poll_interval_seconds: float = 0.02,
    ) -> None:
        if lease_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("资源锁 Lease 和轮询间隔必须大于 0")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = float(lease_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_resource_locks (
                    resource_key TEXT NOT NULL,
                    owner_token TEXT NOT NULL,
                    access_mode TEXT NOT NULL CHECK(access_mode IN ('read', 'write')),
                    lease_expires_at INTEGER NOT NULL,
                    PRIMARY KEY(resource_key, owner_token)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_resource_lease "
                "ON tool_resource_locks(resource_key, lease_expires_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_resource_fencing_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    namespace TEXT NOT NULL,
                    last_token INTEGER NOT NULL CHECK(last_token >= 0)
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO tool_resource_fencing_state("
                "singleton, namespace, last_token) VALUES (1, ?, 0)",
                (str(uuid4()),),
            )

    async def acquire(
        self,
        resource_keys: tuple[str, ...],
        *,
        owner_token: str,
        access: str,
        timeout_seconds: float,
    ) -> ResourceFencingLease | None:
        _validate_lock_request(resource_keys, owner_token, access, timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        while True:
            lease = await durable_to_thread(
                self._try_acquire_sync,
                resource_keys,
                owner_token,
                access,
            )
            if lease is not None:
                return lease
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(self.poll_interval_seconds, remaining))

    def _try_acquire_sync(
        self,
        resource_keys: tuple[str, ...],
        owner_token: str,
        access: str,
    ) -> ResourceFencingLease | None:
        now = int(time.time() * 1000)
        expires = now + int(self.lease_seconds * 1000)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM tool_resource_locks WHERE lease_expires_at <= ?",
                (now,),
            )
            placeholders = ",".join("?" for _ in resource_keys)
            rows = connection.execute(
                "SELECT resource_key, owner_token, access_mode "
                f"FROM tool_resource_locks WHERE resource_key IN ({placeholders})",
                resource_keys,
            ).fetchall()
            blocked = any(
                str(row["owner_token"]) != owner_token
                and (access == "write" or str(row["access_mode"]) == "write")
                for row in rows
            )
            if blocked:
                connection.execute("ROLLBACK")
                return None
            for key in resource_keys:
                connection.execute(
                    """
                    INSERT INTO tool_resource_locks(
                        resource_key, owner_token, access_mode, lease_expires_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(resource_key, owner_token) DO UPDATE SET
                        access_mode=excluded.access_mode,
                        lease_expires_at=excluded.lease_expires_at
                    """,
                    (key, owner_token, access, expires),
                )
            fencing_row = connection.execute(
                "SELECT namespace, last_token FROM tool_resource_fencing_state "
                "WHERE singleton = 1"
            ).fetchone()
            if fencing_row is None:
                raise ToolDispatchError("Resource Fencing 状态不存在")
            fencing_token = int(fencing_row["last_token"]) + 1
            connection.execute(
                "UPDATE tool_resource_fencing_state SET last_token = ? "
                "WHERE singleton = 1",
                (fencing_token,),
            )
            connection.execute("COMMIT")
            return ResourceFencingLease(
                owner_token=owner_token,
                fencing_token=fencing_token,
                fencing_scope=(
                    "tool-resource-lock:sqlite:"
                    f"{str(fencing_row['namespace'])}"
                ),
            )
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    async def renew(
        self,
        resource_keys: tuple[str, ...],
        *,
        owner_token: str,
    ) -> bool:
        return await durable_to_thread(
            self._renew_sync,
            resource_keys,
            owner_token,
        )

    def _renew_sync(self, resource_keys: tuple[str, ...], owner_token: str) -> bool:
        if not resource_keys:
            return True
        now = int(time.time() * 1000)
        expires = int(now + self.lease_seconds * 1000)
        placeholders = ",".join("?" for _ in resource_keys)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE tool_resource_locks SET lease_expires_at = ? "
                f"WHERE owner_token = ? AND resource_key IN ({placeholders}) "
                "AND lease_expires_at > ?",
                (expires, owner_token, *resource_keys, now),
            )
            connection.execute("COMMIT")
            return cursor.rowcount == len(resource_keys)

    async def release(
        self,
        resource_keys: tuple[str, ...],
        *,
        owner_token: str,
    ) -> None:
        if not resource_keys:
            return
        await durable_to_thread(self._release_sync, resource_keys, owner_token)

    def _release_sync(self, resource_keys: tuple[str, ...], owner_token: str) -> None:
        placeholders = ",".join("?" for _ in resource_keys)
        with closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM tool_resource_locks "
                f"WHERE owner_token = ? AND resource_key IN ({placeholders})",
                (owner_token, *resource_keys),
            )


class _FairPrioritySemaphore:
    def __init__(self, value: int) -> None:
        if value <= 0:
            raise ValueError("并发限制必须大于 0")
        self._value = value
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []
        self._sequence = 0
        self._lock = asyncio.Lock()

    async def acquire(self, priority: int, timeout: float | None = None) -> bool:
        """Acquire one slot and report whether the caller had to queue."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        async with self._lock:
            self._sequence += 1
            heapq.heappush(self._waiters, (-priority, self._sequence, future))
            self._grant_locked()
            queued = not future.done()
        try:
            if timeout is None:
                await future
            else:
                await asyncio.wait_for(asyncio.shield(future), timeout)
        except BaseException:
            async with self._lock:
                granted = (
                    future.done()
                    and not future.cancelled()
                    and future.exception() is None
                )
                if granted:
                    # Cancellation can race with set_result(). Return the slot
                    # because the caller never receives a lease in that case.
                    self._value += 1
                elif not future.done():
                    future.cancel()
                self._grant_locked()
            raise
        return queued

    async def release(self) -> None:
        async with self._lock:
            self._value += 1
            self._grant_locked()

    def _grant_locked(self) -> None:
        while self._value > 0 and self._waiters:
            _priority, _sequence, future = heapq.heappop(self._waiters)
            if future.done():
                continue
            self._value -= 1
            future.set_result(None)


@dataclass(slots=True)
class _RWWaiter:
    access: str
    future: asyncio.Future[None]


class _FairAsyncRWLock:
    def __init__(self) -> None:
        self._readers = 0
        self._writer = False
        self._waiters: deque[_RWWaiter] = deque()
        self._mutex = asyncio.Lock()

    async def acquire(self, access: str, timeout: float | None) -> None:
        loop = asyncio.get_running_loop()
        waiter = _RWWaiter(access, loop.create_future())
        async with self._mutex:
            self._waiters.append(waiter)
            self._grant_locked()
        try:
            if timeout is None:
                await waiter.future
            else:
                await asyncio.wait_for(asyncio.shield(waiter.future), timeout)
        except BaseException:
            async with self._mutex:
                granted = (
                    waiter.future.done()
                    and not waiter.future.cancelled()
                    and waiter.future.exception() is None
                )
                if granted:
                    if access == "read":
                        self._readers -= 1
                    else:
                        self._writer = False
                elif not waiter.future.done():
                    waiter.future.cancel()
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
                self._grant_locked()
            raise

    async def release(self, access: str) -> None:
        async with self._mutex:
            if access == "read":
                self._readers -= 1
            else:
                self._writer = False
            self._grant_locked()

    def _grant_locked(self) -> None:
        while self._waiters and self._waiters[0].future.done():
            self._waiters.popleft()
        if self._writer or not self._waiters:
            return
        first = self._waiters[0]
        if first.access == "write":
            if self._readers == 0:
                self._waiters.popleft()
                self._writer = True
                first.future.set_result(None)
            return
        while self._waiters and self._waiters[0].access == "read" and not self._writer:
            waiter = self._waiters.popleft()
            if waiter.future.done():
                continue
            self._readers += 1
            waiter.future.set_result(None)

    @property
    def idle(self) -> bool:
        return not self._writer and self._readers == 0 and not self._waiters


@dataclass(slots=True)
class _RegistryEntry:
    lock: _FairAsyncRWLock = field(default_factory=_FairAsyncRWLock)
    references: int = 0


class ResourceLockRegistry:
    """跨 Prompt 共享、自动清理的公平读写锁注册表。"""

    def __init__(self) -> None:
        self._entries: dict[str, _RegistryEntry] = {}
        self._mutex = asyncio.Lock()

    async def acquire_many(
        self,
        keys: tuple[str, ...],
        *,
        access: str,
        timeout_seconds: float,
    ) -> tuple[tuple[str, _RegistryEntry], ...]:
        if not keys or any(not isinstance(key, str) or not key.strip() for key in keys):
            raise ValueError("Resource Key 不能为空")
        if access not in {"read", "write"}:
            raise ValueError("Resource Lock access 必须是 read 或 write")
        if timeout_seconds <= 0:
            raise ValueError("Resource Lock timeout 必须大于 0")
        acquired: list[tuple[str, _RegistryEntry]] = []
        deadline = time.monotonic() + timeout_seconds
        try:
            for key in sorted(set(keys)):
                async with self._mutex:
                    entry = self._entries.setdefault(key, _RegistryEntry())
                    entry.references += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("等待 Resource Lock 超时")
                try:
                    await entry.lock.acquire(access, remaining)
                except BaseException:
                    await self._drop_reference(key, entry)
                    raise
                acquired.append((key, entry))
            return tuple(acquired)
        except BaseException:
            await self.release_many(tuple(acquired), access=access)
            raise

    async def release_many(
        self,
        acquired: tuple[tuple[str, _RegistryEntry], ...],
        *,
        access: str,
    ) -> None:
        for key, entry in reversed(acquired):
            await entry.lock.release(access)
            await self._drop_reference(key, entry)

    async def _drop_reference(self, key: str, entry: _RegistryEntry) -> None:
        async with self._mutex:
            entry.references -= 1
            if entry.references == 0 and entry.lock.idle:
                self._entries.pop(key, None)

    @property
    def size(self) -> int:
        return len(self._entries)


@dataclass(slots=True)
class ToolSchedulerStats:
    # ``queued`` is the cumulative admission count kept for compatibility.
    queued: int = 0
    completed: int = 0
    lock_timeouts: int = 0
    total_lock_wait_ms: float = 0.0
    waiting: int = 0
    active: int = 0
    max_active: int = 0
    tenant_throttles: int = 0
    parallel_throttles: int = 0
    resource_waits: int = 0
    max_waiting: int = 0


@dataclass(slots=True)
class _ScheduleLease:
    scheduler: "ToolScheduler"
    priority: int
    tenant_id: str | None
    resource_keys: tuple[str, ...]
    resource_access: str
    local_locks: tuple[tuple[str, _RegistryEntry], ...]
    owner_token: str | None
    resource_fencing_token: int | None
    resource_fencing_scope: str | None
    heartbeat: asyncio.Task[None] | None
    tenant_semaphore: _FairPrioritySemaphore | None
    barrier_access: str
    barrier_acquired: bool
    tool_name: str
    execution_mode: str
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        errors: list[BaseException] = []
        lease_lost = False
        if self.heartbeat is not None:
            if not self.heartbeat.done():
                self.heartbeat.cancel()
            heartbeat_results = await asyncio.gather(
                self.heartbeat,
                return_exceptions=True,
            )
            heartbeat_error = heartbeat_results[0]
            if isinstance(heartbeat_error, BaseException) and not isinstance(
                heartbeat_error,
                asyncio.CancelledError,
            ):
                lease_lost = True
                self.scheduler.telemetry.metrics.increment(
                    "resource_lock_lease_lost_total",
                    labels={"tool": self.tool_name},
                )
        if self.owner_token is not None and self.scheduler.distributed_backend is not None:
            try:
                await self.scheduler.distributed_backend.release(
                    self.resource_keys,
                    owner_token=self.owner_token,
                )
            except BaseException as error:
                errors.append(error)
        try:
            await self.scheduler.resource_locks.release_many(
                self.local_locks,
                access=self.resource_access,
            )
        except BaseException as error:
            errors.append(error)
        if self.tenant_semaphore is not None:
            try:
                await self.tenant_semaphore.release()
            except BaseException as error:
                errors.append(error)
        try:
            await self.scheduler._parallel.release()
        except BaseException as error:
            errors.append(error)
        if self.barrier_acquired:
            try:
                await self.scheduler._execution_barrier.release(self.barrier_access)
            except BaseException as error:
                errors.append(error)
        self.scheduler._record_release(
            tool_name=self.tool_name,
            tenant_id=self.tenant_id,
            execution_mode=self.execution_mode,
        )
        if lease_lost:
            await self.scheduler.telemetry.alert(
                "resource_lock_lease_lost",
                severity="critical",
                tool=self.tool_name,
            )
            raise ResourceLeaseLostError(
                f"工具 {self.tool_name} 的跨进程 Resource Lock Lease 已丢失",
                outcome_unknown=False,
            )
        if errors:
            raise errors[0]


class ToolScheduler:
    """公平优先级、读写资源锁、租户限流和跨进程锁调度器。"""

    def __init__(
        self,
        *,
        max_parallel_tools: int = 64,
        default_lock_timeout_seconds: float = 30,
        tenant_limits: dict[str, int] | None = None,
        distributed_backend: ResourceLockBackend | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        if default_lock_timeout_seconds <= 0:
            raise ValueError("default_lock_timeout_seconds 必须大于 0")
        self._parallel = _FairPrioritySemaphore(max_parallel_tools)
        self.default_lock_timeout_seconds = float(default_lock_timeout_seconds)
        self.tenant_limits = dict(tenant_limits or {})
        if any(not key or value <= 0 for key, value in self.tenant_limits.items()):
            raise ValueError("tenant_limits 必须是非空租户名到正整数的映射")
        self._tenant_semaphores = {
            key: _FairPrioritySemaphore(value)
            for key, value in self.tenant_limits.items()
        }
        self.resource_locks = ResourceLockRegistry()
        # Every active non-exclusive call holds a read lease; an exclusive call
        # holds the writer lease. This makes the barrier span all concurrent
        # dispatch()/dispatch_many() calls sharing this scheduler.
        self._execution_barrier = _FairAsyncRWLock()
        self.distributed_backend = distributed_backend
        self.telemetry = telemetry or Telemetry()
        self.stats = ToolSchedulerStats()
        self._parallel_waiting = 0
        self._tenant_waiting: dict[str, int] = {}
        self._tenant_active: dict[str, int] = {}

    async def acquire(
        self,
        tool: AgentTool,
        args: Any,
        *,
        tenant_id: str | None = None,
    ) -> _ScheduleLease:
        priority = tool.priority
        timeout = tool.lock_timeout_seconds or self.default_lock_timeout_seconds
        if tenant_id is not None and (
            not isinstance(tenant_id, str) or not tenant_id.strip()
        ):
            raise TenantContextError(
                f"工具 {tool.name} 收到无效的可信 Tenant ID",
                code="tenant_context_invalid",
            )
        tenant_id = tenant_id.strip() if tenant_id is not None else None
        declared_tenant = (
            tool.resolve_tenant_id(copy.deepcopy(args))
            if tool.resolve_tenant_id
            else None
        )
        if declared_tenant is not None and (
            not isinstance(declared_tenant, str) or not declared_tenant.strip()
        ):
            raise TenantContextError(
                f"工具 {tool.name} 返回了无效 Tenant ID",
                code="tenant_context_invalid",
            )
        if declared_tenant is not None:
            declared_tenant = declared_tenant.strip()
        if tenant_id is not None and declared_tenant not in {None, tenant_id}:
            raise TenantContextError(
                f"工具 {tool.name} 的参数 Tenant 与可信 Tenant 不一致",
                code="tenant_context_mismatch",
            )
        if self.tenant_limits and tenant_id is None:
            raise TenantContextError(
                f"工具 {tool.name} 缺少可信 Tenant Context",
                code="tenant_context_required",
            )
        if self.tenant_limits and tenant_id not in self._tenant_semaphores:
            raise TenantContextError(
                f"工具 {tool.name} 的可信 Tenant 未配置并发限制",
                code="tenant_context_unconfigured",
            )
        resource_keys = _resource_keys(tool, copy.deepcopy(args))
        access = tool.resource_access
        start = time.monotonic()
        deadline = start + timeout
        execution_mode = _execution_mode(tool, "parallel")
        self.stats.queued += 1
        self.stats.waiting += 1
        self.stats.max_waiting = max(self.stats.max_waiting, self.stats.waiting)
        self.telemetry.metrics.increment(
            "tool_scheduler_enqueued_total",
            labels={"tool": tool.name, "mode": execution_mode},
        )
        self._record_queue_depth()
        parallel_acquired = False
        tenant_acquired = False
        local_locks: tuple[tuple[str, _RegistryEntry], ...] = ()
        owner_token: str | None = None
        resource_fencing_token: int | None = None
        resource_fencing_scope: str | None = None
        heartbeat: asyncio.Task[None] | None = None
        barrier_access = "write" if execution_mode == "exclusive" else "read"
        barrier_acquired = False
        resource_started: float | None = None
        resource_wait_recorded = False
        tenant_semaphore = self._tenant_semaphores.get(tenant_id or "")
        metric_tenant_id = tenant_id if tenant_semaphore is not None else None
        wait_scope = (
            "resource_lock"
            if resource_keys
            else "tenant"
            if tenant_semaphore
            else "parallel"
        )
        waiting_registered = True
        timeout_alert = False
        try:
            wait_scope = "exclusive_barrier" if barrier_access == "write" else "shared_barrier"
            barrier_started = time.monotonic()
            await self._execution_barrier.acquire(
                barrier_access,
                _remaining_timeout(deadline),
            )
            barrier_acquired = True
            self.telemetry.metrics.observe(
                "tool_scheduler_wait_ms",
                max(0.0, (time.monotonic() - barrier_started) * 1000),
                labels={"tool": tool.name, "scope": wait_scope},
            )
            # 先等待资源锁，避免被同资源串行化的任务占住全局并发槽。
            if resource_keys:
                wait_scope = "resource_lock"
                resource_started = time.monotonic()
                local_locks = await self.resource_locks.acquire_many(
                    resource_keys,
                    access=access,
                    timeout_seconds=_remaining_timeout(deadline),
                )
                if self.distributed_backend is not None:
                    wait_scope = "distributed_resource_lock"
                    owner_token = str(uuid4())
                    acquired = await self.distributed_backend.acquire(
                        resource_keys,
                        owner_token=owner_token,
                        access=access,
                        timeout_seconds=_remaining_timeout(deadline),
                    )
                    if not acquired:
                        raise TimeoutError("等待跨进程 Resource Lock 超时")
                    capabilities = getattr(
                        self.distributed_backend,
                        "capabilities",
                        None,
                    )
                    if isinstance(acquired, ResourceFencingLease):
                        if acquired.owner_token != owner_token:
                            raise ToolDispatchError(
                                "Resource Lock 返回了不属于当前 owner 的 fencing lease"
                            )
                        resource_fencing_token = acquired.fencing_token
                        resource_fencing_scope = acquired.fencing_scope
                    elif (
                        isinstance(capabilities, ResourceLockBackendCapabilities)
                        and capabilities.supports_fencing_tokens
                    ):
                        raise ToolDispatchError(
                            "Resource Lock 声明支持 fencing，却没有返回 "
                            "ResourceFencingLease"
                        )
                    heartbeat = asyncio.create_task(
                        self._renew_distributed(resource_keys, owner_token),
                        name=f"tool-resource-heartbeat:{owner_token}",
                    )
                resource_wait_ms = max(0.0, (time.monotonic() - resource_started) * 1000)
                self.stats.resource_waits += 1
                for resource_key in resource_keys:
                    self.telemetry.record_resource_lock_wait(
                        resource_key,
                        resource_wait_ms,
                    )
                resource_wait_recorded = True
                self.telemetry.metrics.increment(
                    "resource_lock_acquisitions_total",
                    labels={
                        "tool": tool.name,
                        "access": access,
                        "backend": "distributed" if self.distributed_backend else "local",
                    },
                )
            if tenant_semaphore is not None:
                wait_scope = "tenant"
                tenant_started = time.monotonic()
                self._change_tenant_waiting(tenant_id or "", 1)
                try:
                    tenant_queued = await tenant_semaphore.acquire(
                        priority,
                        _remaining_timeout(deadline),
                    )
                finally:
                    self._change_tenant_waiting(tenant_id or "", -1)
                tenant_acquired = True
                tenant_wait_ms = max(0.0, (time.monotonic() - tenant_started) * 1000)
                self.telemetry.metrics.observe(
                    "tool_scheduler_wait_ms",
                    tenant_wait_ms,
                    labels={"tool": tool.name, "scope": "tenant"},
                )
                if tenant_queued:
                    self.stats.tenant_throttles += 1
                    self.telemetry.metrics.increment(
                        "tool_scheduler_tenant_throttled_total",
                        labels={"tool": tool.name, "tenant": tenant_id or ""},
                    )
            wait_scope = "parallel"
            parallel_started = time.monotonic()
            self._parallel_waiting += 1
            self._record_parallel_queue_depth()
            try:
                parallel_queued = await self._parallel.acquire(
                    priority,
                    _remaining_timeout(deadline),
                )
            finally:
                self._parallel_waiting -= 1
                self._record_parallel_queue_depth()
            parallel_acquired = True
            parallel_wait_ms = max(0.0, (time.monotonic() - parallel_started) * 1000)
            self.telemetry.metrics.observe(
                "tool_scheduler_wait_ms",
                parallel_wait_ms,
                labels={"tool": tool.name, "scope": "parallel"},
            )
            if parallel_queued:
                self.stats.parallel_throttles += 1
                self.telemetry.metrics.increment(
                    "tool_scheduler_parallel_throttled_total",
                    labels={"tool": tool.name},
                )
        except BaseException as error:
            if resource_started is not None and not resource_wait_recorded:
                resource_wait_ms = max(
                    0.0,
                    (time.monotonic() - resource_started) * 1000,
                )
                self.stats.resource_waits += 1
                for resource_key in resource_keys:
                    self.telemetry.record_resource_lock_wait(
                        resource_key,
                        resource_wait_ms,
                    )
            if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
                timeout_alert = True
                self.stats.lock_timeouts += 1
                self.telemetry.metrics.increment(
                    "tool_scheduler_timeouts_total",
                    labels={"tool": tool.name, "scope": wait_scope},
                )
                if wait_scope in {"resource_lock", "distributed_resource_lock"}:
                    self.telemetry.metrics.increment(
                        "resource_lock_timeouts_total",
                        labels={
                            "tool": tool.name,
                            "backend": (
                                "distributed"
                                if wait_scope == "distributed_resource_lock"
                                else "local"
                            ),
                        },
                    )
            elif isinstance(error, (asyncio.CancelledError, OperationCancelledError)):
                self.telemetry.metrics.increment(
                    "tool_scheduler_cancellations_total",
                    labels={"tool": tool.name, "scope": wait_scope},
                )
            cleanup_errors: list[BaseException] = []
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if owner_token is not None and self.distributed_backend is not None:
                try:
                    await self.distributed_backend.release(
                        resource_keys,
                        owner_token=owner_token,
                    )
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if local_locks:
                try:
                    await self.resource_locks.release_many(local_locks, access=access)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if tenant_acquired and tenant_semaphore is not None:
                try:
                    await tenant_semaphore.release()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if parallel_acquired:
                try:
                    await self._parallel.release()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if barrier_acquired:
                try:
                    await self._execution_barrier.release(barrier_access)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                self.telemetry.metrics.increment(
                    "tool_scheduler_cleanup_errors_total",
                    len(cleanup_errors),
                    labels={"tool": tool.name, "phase": "acquire"},
                )
                await self.telemetry.alert(
                    "tool_scheduler_cleanup_error",
                    severity="critical",
                    tool=tool.name,
                    phase="acquire",
                    errorCount=len(cleanup_errors),
                )
            if timeout_alert:
                await self.telemetry.alert(
                    "tool_scheduler_timeout",
                    tool=tool.name,
                    scope=wait_scope,
                    executionMode=execution_mode,
                )
            raise
        finally:
            queue_wait_ms = max(0.0, (time.monotonic() - start) * 1000)
            self.stats.total_lock_wait_ms += queue_wait_ms
            self.telemetry.metrics.observe(
                "tool_scheduler_queue_wait_ms",
                queue_wait_ms,
                labels={"tool": tool.name, "mode": execution_mode},
            )
            if waiting_registered:
                self.stats.waiting -= 1
                waiting_registered = False
                self._record_queue_depth()
        self.stats.active += 1
        self.stats.max_active = max(self.stats.max_active, self.stats.active)
        if metric_tenant_id is not None:
            self._tenant_active[metric_tenant_id] = (
                self._tenant_active.get(metric_tenant_id, 0) + 1
            )
        self._record_active_depth(metric_tenant_id)
        self.telemetry.metrics.increment(
            "tool_scheduler_acquisitions_total",
            labels={"tool": tool.name, "mode": execution_mode},
        )
        return _ScheduleLease(
            scheduler=self,
            priority=priority,
            tenant_id=metric_tenant_id,
            resource_keys=resource_keys,
            resource_access=access,
            local_locks=local_locks,
            owner_token=owner_token,
            resource_fencing_token=resource_fencing_token,
            resource_fencing_scope=resource_fencing_scope,
            heartbeat=heartbeat,
            tenant_semaphore=tenant_semaphore if tenant_acquired else None,
            barrier_access=barrier_access,
            barrier_acquired=barrier_acquired,
            tool_name=tool.name,
            execution_mode=execution_mode,
        )

    def _change_tenant_waiting(self, tenant_id: str, delta: int) -> None:
        value = self._tenant_waiting.get(tenant_id, 0) + delta
        if value <= 0:
            self._tenant_waiting.pop(tenant_id, None)
            value = 0
        else:
            self._tenant_waiting[tenant_id] = value
        self.telemetry.metrics.set_gauge(
            "tool_scheduler_queue_depth",
            value,
            labels={"scope": "tenant", "tenant": tenant_id},
        )

    def _record_queue_depth(self) -> None:
        self.telemetry.metrics.set_gauge(
            "tool_scheduler_queue_depth",
            self.stats.waiting,
            labels={"scope": "scheduler"},
        )

    def _record_parallel_queue_depth(self) -> None:
        self.telemetry.metrics.set_gauge(
            "tool_scheduler_queue_depth",
            self._parallel_waiting,
            labels={"scope": "parallel"},
        )

    def _record_active_depth(self, tenant_id: str | None) -> None:
        self.telemetry.metrics.set_gauge(
            "tool_scheduler_active",
            self.stats.active,
            labels={"scope": "scheduler"},
        )
        if tenant_id is not None:
            self.telemetry.metrics.set_gauge(
                "tool_scheduler_active",
                self._tenant_active.get(tenant_id, 0),
                labels={"scope": "tenant", "tenant": tenant_id},
            )

    def _record_release(
        self,
        *,
        tool_name: str,
        tenant_id: str | None,
        execution_mode: str,
    ) -> None:
        self.stats.active = max(0, self.stats.active - 1)
        if tenant_id is not None:
            value = max(0, self._tenant_active.get(tenant_id, 0) - 1)
            if value:
                self._tenant_active[tenant_id] = value
            else:
                self._tenant_active.pop(tenant_id, None)
        self._record_active_depth(tenant_id)
        self.telemetry.metrics.increment(
            "tool_scheduler_releases_total",
            labels={"tool": tool_name, "mode": execution_mode},
        )

    async def _renew_distributed(
        self,
        keys: tuple[str, ...],
        owner_token: str,
    ) -> None:
        backend = self.distributed_backend
        if backend is None:
            return
        lease_seconds = float(getattr(backend, "lease_seconds", 15.0))
        interval = max(0.01, min(5.0, lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            if not await backend.renew(keys, owner_token=owner_token):
                raise ToolDispatchError("跨进程 Resource Lock Lease 已丢失")


@dataclass(frozen=True, slots=True)
class _RegisteredTool:
    tool: AgentTool
    contract: ToolSecurityContract
    callable_identity: tuple[int | None, ...]
    generation: int


@dataclass(slots=True)
class PreparedToolCall:
    tool_call: dict[str, Any]
    tool: AgentTool
    args: Any
    dispatch_context: ToolDispatchContext = field(default_factory=ToolDispatchContext)
    trusted_tenant_id: str | None = None
    telemetry_started_at: float | None = field(default=None, repr=False)
    telemetry_span: TelemetrySpan | None = field(default=None, repr=False)
    runtime_registration_id: str | None = field(default=None, repr=False)
    tool_contract_digest: str | None = field(default=None, repr=False)
    tool_registration_generation: int | None = field(default=None, repr=False)


@dataclass(slots=True)
class ImmediateToolCall:
    result: AgentToolResult
    is_error: bool
    tool_call: dict[str, Any] | None = None
    args: Any = None
    batch_reject: bool = False


@dataclass(slots=True)
class ToolDispatchOutcome:
    tool_call: dict[str, Any]
    result: AgentToolResult
    is_error: bool
    args: Any = None
    result_message: AgentMessage | None = None


@dataclass(slots=True)
class ToolDispatchBatch:
    outcomes: list[ToolDispatchOutcome]
    messages: list[AgentMessage]
    terminate: bool


class ToolDispatchRuntime:
    """工具执行的唯一正式边界；普通运行和恢复都使用此类。"""

    def __init__(
        self,
        tools: Iterable[AgentTool],
        *,
        before_tool_call: Callable[..., Any] | None = None,
        after_tool_call: Callable[..., Any] | None = None,
        authorization: ToolAuthorization | None = None,
        require_identity: bool = False,
        # 旧版四参数回调仅保留兼容；新代码应使用 authorization +
        # ToolDispatchContext。
        authorize: Callable[[AgentTool, Any, Any, Any], Any] | None = None,
        retry_event_sink: Callable[[AgentEvent], Any] | None = None,
        default_tool_timeout_seconds: float | None = None,
        max_parallel_tools: int = 64,
        default_lock_timeout_seconds: float = 30,
        tenant_limits: dict[str, int] | None = None,
        distributed_lock_backend: ResourceLockBackend | None = None,
        max_update_tasks: int = 64,
        telemetry: Telemetry | None = None,
        default_tenant_id: str | None = None,
    ) -> None:
        if default_tool_timeout_seconds is not None and default_tool_timeout_seconds <= 0:
            raise ValueError("default_tool_timeout_seconds 必须大于 0")
        if max_update_tasks <= 0:
            raise ValueError("max_update_tasks 必须大于 0")
        if not isinstance(require_identity, bool):
            raise TypeError("require_identity 必须是布尔值")
        if default_tenant_id is not None and (
            not isinstance(default_tenant_id, str) or not default_tenant_id.strip()
        ):
            raise ValueError("default_tenant_id 必须是非空字符串或 None")
        self.tools: dict[str, AgentTool] = {}
        self._registrations: dict[str, _RegisteredTool] = {}
        self._registration_id = str(uuid4())
        self._next_tool_generation = 1
        self.register_tools(tools)
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.authorization = authorization
        self.require_identity = require_identity
        self.authorize = authorize
        self.retry_event_sink = retry_event_sink
        self.default_tool_timeout_seconds = default_tool_timeout_seconds
        self.max_update_tasks = max_update_tasks
        self.telemetry = telemetry or Telemetry()
        self.default_tenant_id = (
            default_tenant_id.strip() if default_tenant_id is not None else None
        )
        self.scheduler = ToolScheduler(
            max_parallel_tools=max_parallel_tools,
            default_lock_timeout_seconds=default_lock_timeout_seconds,
            tenant_limits=tenant_limits,
            distributed_backend=distributed_lock_backend,
            telemetry=self.telemetry,
        )

    def register_tools(self, tools: Iterable[AgentTool]) -> None:
        for tool in tools:
            if not isinstance(tool, AgentTool):
                raise TypeError("Tool Runtime 只能注册 AgentTool")
            existing = self._registrations.get(tool.name)
            if existing is not None:
                if existing.tool is not tool:
                    raise ToolDispatchError(
                        f"工具 {tool.name} 已注册；禁止同名不同实例覆盖"
                    )
                self.assert_registered_tool(tool)
                continue
            contract = ToolSecurityContract.capture(tool)
            registration = _RegisteredTool(
                tool=tool,
                contract=contract,
                callable_identity=tool_callable_identity(tool),
                generation=self._next_tool_generation,
            )
            self._next_tool_generation += 1
            self._registrations[tool.name] = registration
            self.tools[tool.name] = tool

    def assert_registered_tool(
        self,
        tool: AgentTool,
        *,
        expected_contract_digest: str | None = None,
    ) -> ToolSecurityContract:
        """Fail closed unless ``tool`` is this Runtime's exact sealed object."""

        registration = self._registrations.get(tool.name)
        if registration is None:
            raise ToolDispatchError(f"工具未注册：{tool.name}")
        if registration.tool is not tool:
            raise ToolDispatchError(
                f"工具 {tool.name} 与 Runtime 受信注册实例不一致"
            )
        current = ToolSecurityContract.capture(tool)
        if (
            current != registration.contract
            or tool_callable_identity(tool) != registration.callable_identity
        ):
            raise ToolDispatchError(
                f"工具 {tool.name} 的注册后安全合同或 Handler 已变化"
            )
        if (
            expected_contract_digest is not None
            and registration.contract.digest != expected_contract_digest
        ):
            raise ToolDispatchError(
                f"工具 {tool.name} 的安全合同 Digest 不匹配"
            )
        return registration.contract

    def tool_contract(self, name: str) -> ToolSecurityContract:
        registration = self._registrations.get(name)
        if registration is None:
            raise ToolDispatchError(f"工具未注册：{name}")
        self.assert_registered_tool(registration.tool)
        return registration.contract

    def _validate_prepared_registration(self, prepared: PreparedToolCall) -> None:
        registration = self._registrations.get(prepared.tool.name)
        if registration is None:
            raise ToolDispatchError(f"工具未注册：{prepared.tool.name}")
        if prepared.runtime_registration_id != self._registration_id:
            raise ToolDispatchError("Prepared Tool Call 不属于当前 Runtime")
        if prepared.tool_registration_generation != registration.generation:
            raise ToolDispatchError("Prepared Tool Call 的注册代际已经失效")
        if prepared.tool_contract_digest != registration.contract.digest:
            raise ToolDispatchError("Prepared Tool Call 的安全合同已经失效")
        self.assert_registered_tool(
            prepared.tool,
            expected_contract_digest=prepared.tool_contract_digest,
        )
        if prepared.tool.requires_approval:
            if self.authorization is None and self.authorize is None:
                raise ToolDispatchError(
                    f"工具 {prepared.tool.name} 要求审批，但 Runtime "
                    "缺少可信审批授权边界"
                )
            if prepared.dispatch_context.identity is None:
                raise ToolDispatchError(
                    f"工具 {prepared.tool.name} 的 Prepared Call 缺少可信执行身份"
                )

    async def prepare(
        self,
        tool_call: dict[str, Any],
        *,
        context: AgentContext,
        assistant_message: AgentMessage,
        cancellation: CancellationToken,
        dispatch_context: ToolDispatchContext | None = None,
        identity: Any = None,
        approval: Any = None,
    ) -> PreparedToolCall | ImmediateToolCall:
        canonical = self._canonicalize(tool_call, context=context)
        if isinstance(canonical, ImmediateToolCall):
            return canonical
        trusted_context = dispatch_context or ToolDispatchContext(
            identity=identity,
            approval=approval,
        )
        return await self._apply_dispatch_guards(
            canonical,
            context=context,
            assistant_message=assistant_message,
            cancellation=cancellation,
            dispatch_context=trusted_context,
        )

    def _canonicalize(
        self,
        tool_call: dict[str, Any],
        *,
        context: AgentContext,
    ) -> PreparedToolCall | ImmediateToolCall:
        raw_name = tool_call.get("name")
        tool_name = raw_name.strip() if isinstance(raw_name, str) else ""
        allowed_names = [tool.name for tool in context.tools]
        if len(allowed_names) != len(set(allowed_names)):
            return ImmediateToolCall(
                error_tool_result(
                    "当前轮 Tool 白名单包含重复名称",
                    details={"code": "tool_policy_invalid", "retryable": False},
                ),
                True,
                batch_reject=True,
            )
        if not tool_name or tool_name not in set(allowed_names):
            return ImmediateToolCall(
                error_tool_result(
                    f"当前轮不允许工具：{tool_name or '<empty>'}",
                    details={"code": "tool_not_allowed", "retryable": False},
                ),
                True,
                batch_reject=True,
            )
        tool = self.tools.get(tool_name)
        if tool is None:
            return ImmediateToolCall(
                error_tool_result(
                    f"工具不存在：{tool_name}",
                    details={"code": "tool_not_found", "retryable": False},
                ),
                True,
                batch_reject=True,
            )
        allowed_tool = next(
            (item for item in context.tools if item.name == tool_name),
            None,
        )
        if allowed_tool is not tool:
            return ImmediateToolCall(
                error_tool_result(
                    f"工具 {tool_name} 与当前轮受信注册实例不一致",
                    details={"code": "tool_policy_invalid", "retryable": False},
                ),
                True,
                batch_reject=True,
            )
        try:
            contract = self.assert_registered_tool(tool)
        except ToolDispatchError as error:
            return ImmediateToolCall(
                error_tool_result(
                    error,
                    details={"code": "tool_policy_invalid", "retryable": False},
                ),
                True,
                batch_reject=True,
            )
        raw_arguments = tool_call.get("arguments", {})
        if not isinstance(raw_arguments, dict):
            return ImmediateToolCall(
                error_tool_result(
                    f"工具 {tool_name} 的 arguments 必须是对象",
                    details={"code": "tool_arguments_invalid", "retryable": False},
                ),
                True,
                batch_reject=True,
            )
        try:
            raw_args = copy.deepcopy(raw_arguments)
            if tool.prepare_arguments is not None:
                raw_args = tool.prepare_arguments(raw_args)
            # validate_args 的返回值是唯一 canonical 快照。之后授权、Hook、
            # Scheduler 与 Tool Execute 都只能拿它的隔离副本，任何一方修改
            # 自己的对象都不能改变审计/审批实际对应的参数。
            args = copy.deepcopy(tool.validate_args(copy.deepcopy(raw_args)))
            if not isinstance(args, dict):
                raise TypeError("canonical arguments 必须是对象")
            canonical_tool_call = copy.deepcopy(tool_call)
            canonical_tool_call["name"] = tool_name
            canonical_tool_call["arguments"] = copy.deepcopy(args)
        except Exception as error:
            return ImmediateToolCall(
                error_tool_result(
                    error,
                    details={"code": "tool_arguments_invalid", "retryable": False},
                ),
                True,
                batch_reject=True,
            )
        return PreparedToolCall(
            copy.deepcopy(canonical_tool_call),
            tool,
            copy.deepcopy(args),
            runtime_registration_id=self._registration_id,
            tool_contract_digest=contract.digest,
            tool_registration_generation=self._registrations[tool.name].generation,
        )

    async def _apply_dispatch_guards(
        self,
        prepared: PreparedToolCall,
        *,
        context: AgentContext,
        assistant_message: AgentMessage,
        cancellation: CancellationToken,
        dispatch_context: ToolDispatchContext,
    ) -> PreparedToolCall | ImmediateToolCall:
        tool = prepared.tool
        canonical_tool_call = prepared.tool_call
        args = prepared.args
        try:
            if (
                tool.requires_approval
                and self.authorization is None
                and self.authorize is None
            ):
                raise PermissionError(
                    f"工具 {tool.name} 要求审批，但 Runtime 缺少可信审批授权边界"
                )
            if (
                self.require_identity
                or self.authorization is not None
                or tool.requires_approval
            ) and dispatch_context.identity is None:
                raise PermissionError(f"工具 {tool.name} 缺少可信执行身份")
            if self.authorization is not None:
                authorization_context = ToolDispatchContext(
                    identity=dispatch_context.identity,
                    approval=copy.deepcopy(dispatch_context.approval),
                    tenant_id=dispatch_context.tenant_id,
                    fencing_token=dispatch_context.fencing_token,
                    fencing_scope=dispatch_context.fencing_scope,
                    resource_fencing_token=(
                        dispatch_context.resource_fencing_token
                    ),
                    resource_fencing_scope=(
                        dispatch_context.resource_fencing_scope
                    ),
                )
                allowed = await _maybe_await(
                    self.authorization(
                        tool,
                        copy.deepcopy(args),
                        authorization_context,
                    )
                )
                if not allowed:
                    raise PermissionError(f"工具 {tool.name} 未通过执行授权")
            if self.authorize is not None:
                allowed = await _maybe_await(
                    self.authorize(
                        tool,
                        copy.deepcopy(args),
                        dispatch_context.identity,
                        dispatch_context.approval,
                    )
                )
                if not allowed:
                    raise PermissionError(f"工具 {tool.name} 未通过执行授权")
            if self.before_tool_call is not None:
                before = await _maybe_await(
                    self.before_tool_call(
                        BeforeToolCallContext(
                            assistant_message=copy.deepcopy(assistant_message),
                            tool_call=copy.deepcopy(canonical_tool_call),
                            args=copy.deepcopy(args),
                            context=_copy_agent_context(context),
                        ),
                        cancellation,
                    )
                )
                if isinstance(before, BeforeToolCallResult) and before.block:
                    return ImmediateToolCall(
                        error_tool_result(
                            before.reason or "工具执行已被阻止",
                            terminate=before.terminate,
                        ),
                        True,
                        copy.deepcopy(canonical_tool_call),
                        copy.deepcopy(args),
                    )
            if cancellation.cancelled:
                cancelled = _cancelled_before_dispatch(tool.name)
                cancelled.tool_call = copy.deepcopy(canonical_tool_call)
                cancelled.args = copy.deepcopy(args)
                return cancelled
            return PreparedToolCall(
                copy.deepcopy(canonical_tool_call),
                tool,
                copy.deepcopy(args),
                dispatch_context=_copy_tool_dispatch_context(dispatch_context),
                runtime_registration_id=prepared.runtime_registration_id,
                tool_contract_digest=prepared.tool_contract_digest,
                tool_registration_generation=prepared.tool_registration_generation,
            )
        except PermissionError:
            return ImmediateToolCall(
                error_tool_result(
                    "工具未通过执行授权",
                    details={"code": "permission_denied", "retryable": False},
                ),
                True,
                copy.deepcopy(canonical_tool_call),
                copy.deepcopy(args),
            )
        except Exception as error:
            return ImmediateToolCall(
                error_tool_result(error),
                True,
                copy.deepcopy(canonical_tool_call),
                copy.deepcopy(args),
            )

    async def dispatch(
        self,
        tool_call: dict[str, Any],
        *,
        context: AgentContext | None = None,
        assistant_message: AgentMessage | None = None,
        dispatch_context: ToolDispatchContext | None = None,
        identity: Any = None,
        approval: Any = None,
        cancellation: CancellationToken | None = None,
        emit: EventSink | None = None,
        emit_messages: bool = True,
        tenant_id: str | None = None,
        attempt_admission: Callable[[AgentTool, int], Any] | None = None,
    ) -> ToolDispatchOutcome:
        token = cancellation or CancellationToken()
        context = context or AgentContext(
            system_prompt="",
            messages=[],
            tools=list(self.tools.values()),
        )
        assistant_message = assistant_message or {
            "role": "assistant",
            "content": [copy.deepcopy(tool_call)],
        }
        trusted_context = _resolve_dispatch_context(
            dispatch_context,
            identity=identity,
            approval=approval,
            tenant_id=(
                tenant_id if tenant_id is not None else self.default_tenant_id
            ),
        )
        sink = emit or _discard_event
        started_at = time.monotonic()
        span = self._start_tool_span(tool_call)
        prepared = await self.prepare(
            tool_call,
            context=context,
            assistant_message=assistant_message,
            cancellation=token,
            dispatch_context=trusted_context,
        )
        if isinstance(prepared, ImmediateToolCall):
            emitted_call = copy.deepcopy(prepared.tool_call or tool_call)
            await _emit_tool_start(emitted_call, sink)
            outcome = ToolDispatchOutcome(
                emitted_call,
                prepared.result,
                prepared.is_error,
                copy.deepcopy(prepared.args),
            )
            await self._record_tool_outcome(
                tool_name=self._metric_tool_name(tool_call),
                outcome=outcome,
                started_at=started_at,
                span=span,
            )
        else:
            await _emit_tool_start(prepared.tool_call, sink)
            prepared.trusted_tenant_id = self._trusted_tenant_id(
                trusted_context.tenant_id
            )
            prepared.telemetry_started_at = started_at
            prepared.telemetry_span = span
            outcome = await self.dispatch_prepared(
                prepared,
                context=context,
                assistant_message=assistant_message,
                cancellation=token,
                emit=sink,
                attempt_admission=attempt_admission,
            )
        await _emit_tool_end(outcome, sink)
        if emit_messages:
            message = _tool_result_message(outcome)
            await _emit_result_message(message, sink)
        return outcome

    async def dispatch_prepared(
        self,
        prepared: PreparedToolCall,
        *,
        context: AgentContext,
        assistant_message: AgentMessage,
        cancellation: CancellationToken,
        emit: EventSink,
        tenant_id: str | None = None,
        attempt_admission: Callable[[AgentTool, int], Any] | None = None,
    ) -> ToolDispatchOutcome:
        self._validate_prepared_registration(prepared)
        trusted_tenant_id = (
            prepared.trusted_tenant_id
            if prepared.trusted_tenant_id is not None
            else self._trusted_tenant_id(tenant_id)
        )
        started_at = prepared.telemetry_started_at or time.monotonic()
        span = prepared.telemetry_span or self._start_tool_span(prepared.tool_call)
        await self.telemetry.log(
            "info",
            "tool_call_started",
            trace_id=span.trace_id,
            span_id=span.span_id,
            tool=prepared.tool.name,
            executionMode=_execution_mode(prepared.tool, "parallel"),
            priority=prepared.tool.priority,
        )
        try:
            result, is_error = await self._execute(
                prepared,
                cancellation,
                emit,
                telemetry_span=span,
                tenant_id=trusted_tenant_id,
                attempt_admission=attempt_admission,
            )
            self.scheduler.stats.completed += 1
            safety_result = (
                copy.deepcopy(result)
                if _result_is_outcome_unknown(result)
                else None
            )
            committed_success = (
                not is_error and _tool_may_have_side_effect(prepared.tool)
            )
            committed_result = copy.deepcopy(result)
            result, is_error = await self._after(
                prepared,
                result,
                is_error,
                context,
                assistant_message,
                cancellation,
            )
            # outcome_unknown 是副作用边界的不可降级安全事实。业务 after hook
            # 可以观察它，但不能把它改写成 success 或普通失败。
            if safety_result is not None:
                result = safety_result
                is_error = True
            elif committed_success and is_error:
                # A post-execution hook may redact/replace output, but it cannot
                # turn an already confirmed external commit into a retryable
                # failure.  Stop the loop with a safe success marker instead.
                result = _committed_tool_output_unavailable(
                    prepared.tool,
                    str(prepared.tool_call.get("id", "")),
                    committed_result,
                )
                is_error = False
            outcome = ToolDispatchOutcome(
                copy.deepcopy(prepared.tool_call),
                result,
                is_error,
                copy.deepcopy(prepared.args),
            )
            await self._record_tool_outcome(
                tool_name=prepared.tool.name,
                outcome=outcome,
                started_at=started_at,
                span=span,
            )
            return outcome
        except asyncio.CancelledError as error:
            await self._record_tool_exception(
                tool_name=prepared.tool.name,
                outcome="cancelled",
                started_at=started_at,
                span=span,
                error=error,
            )
            raise
        except BaseException as error:
            await self._record_tool_exception(
                tool_name=prepared.tool.name,
                outcome="error",
                started_at=started_at,
                span=span,
                error=error,
            )
            raise

    async def dispatch_many(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        context: AgentContext,
        assistant_message: AgentMessage,
        cancellation: CancellationToken,
        emit: EventSink,
        execution: str = "parallel",
        dispatch_context: ToolDispatchContext | None = None,
        identity: Any = None,
        approval: Any = None,
        tenant_id: str | None = None,
        attempt_admission: Callable[[AgentTool, int], Any] | None = None,
    ) -> ToolDispatchBatch:
        trusted_context = _resolve_dispatch_context(
            dispatch_context,
            identity=identity,
            approval=approval,
            tenant_id=(
                tenant_id if tenant_id is not None else self.default_tenant_id
            ),
        )
        checked_calls, batch_error = _validate_tool_call_batch(
            tool_calls,
            context=context,
            assistant_message=assistant_message,
        )
        if batch_error is not None:
            return await self._reject_tool_call_batch(
                checked_calls,
                reason=batch_error,
                emit=emit,
            )
        # Phase 1 是全批、无授权/无 Hook 的 canonical 预检。任何名称、当前轮
        # 白名单、参数结构或业务校验失败，整批在进入 guards 前拒绝。
        canonical_calls: list[PreparedToolCall] = []
        for tool_call in checked_calls:
            canonical = self._canonicalize(tool_call, context=context)
            if isinstance(canonical, ImmediateToolCall):
                return await self._reject_tool_call_batch(
                    checked_calls,
                    reason=(
                        "至少一个 Tool Call 未通过名称、当前轮白名单或 "
                        "canonical 参数校验"
                    ),
                    emit=emit,
                )
            canonical_calls.append(canonical)

        trusted_tenant_id = self._trusted_tenant_id(trusted_context.tenant_id)
        slots: list[ToolDispatchOutcome | None] = []
        scheduled: list[tuple[int, PreparedToolCall]] = []
        # Phase 2 仅在全批 canonical 校验成功后才进入授权与 before hook。
        for canonical in canonical_calls:
            tool_call = canonical.tool_call
            started_at = time.monotonic()
            span = self._start_tool_span(tool_call)
            prepared = await self._apply_dispatch_guards(
                canonical,
                context=context,
                assistant_message=assistant_message,
                cancellation=cancellation,
                dispatch_context=trusted_context,
            )
            if isinstance(prepared, ImmediateToolCall):
                emitted_call = copy.deepcopy(prepared.tool_call or tool_call)
                await _emit_tool_start(emitted_call, emit)
                outcome = ToolDispatchOutcome(
                    emitted_call,
                    prepared.result,
                    prepared.is_error,
                    copy.deepcopy(prepared.args),
                )
                await self._record_tool_outcome(
                    tool_name=self._metric_tool_name(tool_call),
                    outcome=outcome,
                    started_at=started_at,
                    span=span,
                )
                await _emit_tool_end(outcome, emit)
                slots.append(outcome)
            else:
                await _emit_tool_start(prepared.tool_call, emit)
                prepared.trusted_tenant_id = trusted_tenant_id
                prepared.telemetry_started_at = started_at
                prepared.telemetry_span = span
                index = len(slots)
                slots.append(None)
                scheduled.append((index, prepared))
                await _emit(
                    emit,
                    {
                        "type": "tool_execution_queued",
                        "toolCallId": str(prepared.tool_call.get("id", "")),
                        "toolName": prepared.tool.name,
                        "executionMode": _execution_mode(prepared.tool, execution),
                        "resourceKeyCount": len(
                            _resource_keys(
                                prepared.tool,
                                copy.deepcopy(prepared.args),
                            )
                        ),
                        "priority": prepared.tool.priority,
                    },
                )

        if execution == "sequential":
            await self._run_sequential(
                scheduled,
                slots,
                context,
                assistant_message,
                cancellation,
                emit,
                attempt_admission=attempt_admission,
            )
        else:
            pending: list[tuple[int, PreparedToolCall]] = []
            for indexed in scheduled:
                if _execution_mode(indexed[1].tool, execution) == "exclusive":
                    await self._run_group(
                        pending,
                        slots,
                        context,
                        assistant_message,
                        cancellation,
                        emit,
                        attempt_admission=attempt_admission,
                    )
                    pending = []
                    await self._run_group(
                        [indexed],
                        slots,
                        context,
                        assistant_message,
                        cancellation,
                        emit,
                        attempt_admission=attempt_admission,
                    )
                else:
                    pending.append(indexed)
            await self._run_group(
                pending,
                slots,
                context,
                assistant_message,
                cancellation,
                emit,
                attempt_admission=attempt_admission,
            )

        outcomes = [cast(ToolDispatchOutcome, item) for item in slots if item is not None]
        messages: list[AgentMessage] = []
        for outcome in outcomes:
            message = _tool_result_message(outcome)
            await _emit_result_message(message, emit)
            messages.append(message)
        terminate = bool(outcomes) and all(
            item.result.terminate is True for item in outcomes
        )
        return ToolDispatchBatch(outcomes, messages, terminate)

    async def _reject_tool_call_batch(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        reason: str,
        emit: EventSink,
    ) -> ToolDispatchBatch:
        """整批拒绝无效 ID，保证零授权、零 Hook、零 Tool Execute。"""

        outcomes: list[ToolDispatchOutcome] = []
        messages: list[AgentMessage] = []
        for tool_call in tool_calls:
            started_at = time.monotonic()
            span = self._start_tool_span(tool_call)
            await _emit_tool_start(tool_call, emit)
            outcome = ToolDispatchOutcome(
                copy.deepcopy(tool_call),
                error_tool_result(
                    f"整批工具均未执行：{reason}",
                    details={
                        "code": "tool_call_batch_rejected",
                        "synthetic": True,
                        "retryable": False,
                    },
                ),
                True,
            )
            await self._record_tool_outcome(
                tool_name=self._metric_tool_name(tool_call),
                outcome=outcome,
                started_at=started_at,
                span=span,
            )
            await _emit_tool_end(outcome, emit)
            message = _tool_result_message(outcome)
            await _emit_result_message(message, emit)
            outcomes.append(outcome)
            messages.append(message)
        return ToolDispatchBatch(outcomes, messages, False)

    async def _run_sequential(
        self,
        scheduled: list[tuple[int, PreparedToolCall]],
        slots: list[ToolDispatchOutcome | None],
        context: AgentContext,
        assistant_message: AgentMessage,
        cancellation: CancellationToken,
        emit: EventSink,
        *,
        attempt_admission: Callable[[AgentTool, int], Any] | None = None,
    ) -> None:
        for position, (index, prepared) in enumerate(scheduled):
            if cancellation.cancelled:
                for skipped_index, skipped in scheduled[position:]:
                    outcome = ToolDispatchOutcome(
                        skipped.tool_call,
                        error_tool_result(
                            "工具调用在执行前因 Agent 取消而跳过。",
                            details={
                                "code": "tool_aborted_before_dispatch",
                                "synthetic": True,
                            },
                        ),
                        True,
                    )
                    await self._record_tool_outcome(
                        tool_name=skipped.tool.name,
                        outcome=outcome,
                        started_at=skipped.telemetry_started_at or time.monotonic(),
                        span=skipped.telemetry_span or self._start_tool_span(skipped.tool_call),
                    )
                    await _emit_tool_end(outcome, emit)
                    slots[skipped_index] = outcome
                return
            outcome = await self.dispatch_prepared(
                prepared,
                context=context,
                assistant_message=assistant_message,
                cancellation=cancellation,
                emit=emit,
                attempt_admission=attempt_admission,
            )
            await _emit_tool_end(outcome, emit)
            slots[index] = outcome

    async def _run_group(
        self,
        group: list[tuple[int, PreparedToolCall]],
        slots: list[ToolDispatchOutcome | None],
        context: AgentContext,
        assistant_message: AgentMessage,
        cancellation: CancellationToken,
        emit: EventSink,
        *,
        attempt_admission: Callable[[AgentTool, int], Any] | None = None,
    ) -> None:
        if not group:
            return

        async def run_one(index: int, prepared: PreparedToolCall):
            outcome = await self.dispatch_prepared(
                prepared,
                context=context,
                assistant_message=assistant_message,
                cancellation=cancellation,
                emit=emit,
                attempt_admission=attempt_admission,
            )
            await _emit_tool_end(outcome, emit)
            return index, outcome

        ordered = sorted(group, key=lambda item: (-item[1].tool.priority, item[0]))
        tasks = [
            asyncio.create_task(
                run_one(index, prepared),
                name=f"tool-dispatch:{prepared.tool.name}:{prepared.tool_call.get('id', '')}",
            )
            for index, prepared in ordered
        ]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for index, outcome in results:
            slots[index] = outcome

    async def _execute(
        self,
        prepared: PreparedToolCall,
        cancellation: CancellationToken,
        emit: EventSink,
        *,
        telemetry_span: TelemetrySpan | None = None,
        tenant_id: str | None = None,
        attempt_admission: Callable[[AgentTool, int], Any] | None = None,
    ) -> tuple[AgentToolResult, bool]:
        tool = prepared.tool
        tool_call_id = str(prepared.tool_call.get("id", ""))
        tool_token = cancellation.create_child()
        timeout = tool.timeout_seconds or self.default_tool_timeout_seconds
        update_tasks: set[asyncio.Task[None]] = set()
        completed_update_errors: list[BaseException] = []
        coalesced_update: AgentToolResult | None = None
        accepting_updates = True
        attempt = 0

        def on_update(partial: AgentToolResult) -> None:
            nonlocal coalesced_update
            if not accepting_updates:
                self.telemetry.metrics.increment(
                    "tool_updates_dropped_total",
                    labels={"tool": tool.name, "reason": "late"},
                )
                return
            if len(update_tasks) >= self.max_update_tasks:
                coalesced_update = partial
                self.telemetry.metrics.increment(
                    "tool_update_backpressure_total",
                    labels={"tool": tool.name, "action": "coalesce"},
                )
                self.telemetry.record_backpressure(
                    "tool_updates",
                    {
                        "action": "coalesce",
                        "queuedEvents": len(update_tasks),
                        "queuedBytes": 0,
                    },
                )
                return
            task = asyncio.create_task(
                _emit(
                    emit,
                    {
                        "type": "tool_execution_update",
                        "toolCallId": tool_call_id,
                        "toolName": tool.name,
                        "args": copy.deepcopy(prepared.tool_call.get("arguments", {})),
                        "partialResult": _result_payload(partial),
                    },
                ),
                name=f"tool-update:{tool.name}:{tool_call_id}",
            )
            update_tasks.add(task)
            self.telemetry.metrics.increment(
                "tool_updates_total",
                labels={"tool": tool.name},
            )
            self.telemetry.record_queue_depth(
                "tool_updates",
                events=len(update_tasks),
                retained_bytes=0,
            )

            def update_done(done: asyncio.Task[None]) -> None:
                update_tasks.discard(done)
                self.telemetry.record_queue_depth(
                    "tool_updates",
                    events=len(update_tasks),
                    retained_bytes=0,
                )
                if done.cancelled():
                    return
                error = done.exception()
                if error is not None:
                    # 取走异常避免 asyncio 的“Task exception was never retrieved”，
                    # 并在工具清理完成后按严格 Listener 语义重新抛出。
                    completed_update_errors.append(error)

            task.add_done_callback(update_done)

        execution_started = asyncio.Event()
        handler_dispatch_started = asyncio.Event()

        async def execute_once() -> AgentToolResult:
            nonlocal attempt
            attempt += 1
            self.telemetry.metrics.increment(
                "tool_attempts_total",
                labels={
                    "tool": tool.name,
                    "kind": "initial" if attempt == 1 else "retry",
                },
            )
            try:
                lease = await self.scheduler.acquire(
                    tool,
                    copy.deepcopy(prepared.args),
                    tenant_id=tenant_id,
                )
            except (TimeoutError, asyncio.TimeoutError) as error:
                raise ResourceLockTimeoutError(
                    f"工具 {tool.name} 等待资源锁超时"
                ) from error
            if attempt_admission is not None:
                try:
                    admission = attempt_admission(tool, attempt)
                    if inspect.isawaitable(admission):
                        await cast(Awaitable[Any], admission)
                except BaseException as admission_error:
                    try:
                        await lease.release()
                    except BaseException as cleanup_error:
                        raise BaseExceptionGroup(
                            "Tool Attempt Admission 和 Lease 清理均失败",
                            [admission_error, cleanup_error],
                        )
                    raise
            # The call-level token is watched by _execute() for user/timeout
            # cancellation. Lease loss is attempt-local, so use a child token;
            # cancelling the parent here would race with cancel_wait and could
            # incorrectly turn resource_lease_lost into an ordinary cancellation.
            attempt_token = tool_token.create_child()
            primary_error: BaseException | None = None
            try:
                execution_started.set()
                async def invoke_tool() -> AgentToolResult:
                    await _emit(
                        emit,
                        {
                            "type": "tool_execution_dispatch_start",
                            "toolCallId": tool_call_id,
                            "toolName": tool.name,
                            "attempt": attempt,
                        },
                    )
                    if tool.execute_with_context is not None:
                        execution_context = _copy_tool_dispatch_context(
                            prepared.dispatch_context
                        )
                        if lease.resource_fencing_token is not None:
                            execution_context = _with_resource_fencing(
                                execution_context,
                                token=lease.resource_fencing_token,
                                scope=lease.resource_fencing_scope,
                            )
                        if (
                            tool.supports_resource_fencing
                            and execution_context.resource_fencing_token is None
                        ):
                            raise ToolDispatchError(
                                f"工具 {tool.name} 要求 Resource Fencing，"
                                "但锁后端没有提供单调 token"
                            )
                        handler_arguments = copy.deepcopy(prepared.args)
                        handler_dispatch_started.set()
                        return await tool.execute_with_context(
                            tool_call_id,
                            handler_arguments,
                            execution_context,
                            attempt_token,
                            on_update,
                        )
                    legacy_execute = tool.execute
                    if legacy_execute is None:
                        raise ToolDispatchError(
                            f"工具 {tool.name} 没有可用的 execute Handler"
                        )
                    handler_arguments = copy.deepcopy(prepared.args)
                    handler_dispatch_started.set()
                    return await legacy_execute(
                        tool_call_id,
                        handler_arguments,
                        attempt_token,
                        on_update,
                    )

                if lease.heartbeat is None:
                    return await invoke_tool()

                tool_task = asyncio.create_task(
                    invoke_tool(),
                    name=f"tool-leased-execute:{tool.name}:{tool_call_id}",
                )
                try:
                    done, _pending = await asyncio.wait(
                        {tool_task, lease.heartbeat},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if lease.heartbeat not in done:
                        return await tool_task

                    try:
                        await lease.heartbeat
                    except BaseException as lease_error:
                        if telemetry_span is not None:
                            telemetry_span.add_event(
                                "resource_lock_lease_lost",
                                errorType=type(lease_error).__name__,
                            )
                    attempt_token.cancel(
                        f"工具 {tool.name} 的跨进程 Resource Lock Lease 已丢失"
                    )
                    if not tool_task.done():
                        tool_task.cancel()
                    tool_result = (await asyncio.gather(
                        tool_task,
                        return_exceptions=True,
                    ))[0]
                    # Once a never-replay/write tool has entered execute(), the
                    # runtime cannot prove that cancellation happened before its
                    # external commit. Lease loss is therefore always outcome
                    # unknown. Safe tools can use the cooperative cancellation
                    # result to distinguish a clean abort from a returned result.
                    crossed_commit_boundary = (
                        tool.replay_policy == "never"
                        or isinstance(tool_result, AgentToolResult)
                    )
                    await _emit(
                        emit,
                        {
                            "type": "tool_resource_lease_lost",
                            "toolCallId": tool_call_id,
                            "toolName": tool.name,
                            "outcomeUnknown": crossed_commit_boundary,
                        },
                    )
                    raise ResourceLeaseLostError(
                        f"工具 {tool.name} 执行期间丢失跨进程 Resource Lock Lease",
                        outcome_unknown=crossed_commit_boundary,
                    )
                finally:
                    if not tool_task.done():
                        tool_task.cancel()
                    await asyncio.gather(tool_task, return_exceptions=True)
            except BaseException as error:
                primary_error = error
                raise
            finally:
                # 每个 Retry Attempt 独立持锁；Backoff 期间立即让出资源。
                attempt_token.detach()
                try:
                    await lease.release()
                except ResourceLeaseLostError as cleanup_error:
                    self.telemetry.metrics.increment(
                        "tool_scheduler_cleanup_errors_total",
                        labels={
                            "tool": tool.name,
                            "error": type(cleanup_error).__name__,
                        },
                    )
                    # A heartbeat failure can race just behind a completed tool
                    # task and only become visible during release(). Never let
                    # that late signal degrade to tool_execution_error.
                    if not isinstance(primary_error, ResourceLeaseLostError):
                        outcome_unknown = (
                            tool.replay_policy == "never"
                            or primary_error is None
                        )
                        try:
                            await _emit(
                                emit,
                                {
                                    "type": "tool_resource_lease_lost",
                                    "toolCallId": tool_call_id,
                                    "toolName": tool.name,
                                    "outcomeUnknown": outcome_unknown,
                                    "detectedDuring": "release",
                                },
                            )
                        except BaseException:
                            # Lease uncertainty is the primary safety result;
                            # an observer failure must not rewrite it.
                            self.telemetry.metrics.increment(
                                "tool_lease_lost_events_dropped_total",
                                labels={"tool": tool.name},
                            )
                        raise ResourceLeaseLostError(
                            f"工具 {tool.name} 执行期间丢失跨进程 Resource Lock Lease",
                            outcome_unknown=outcome_unknown,
                        ) from cleanup_error
                except BaseException as cleanup_error:
                    self.telemetry.metrics.increment(
                        "tool_scheduler_cleanup_errors_total",
                        labels={
                            "tool": tool.name,
                            "error": type(cleanup_error).__name__,
                        },
                    )
                    await self.telemetry.alert(
                        "tool_scheduler_cleanup_error",
                        severity="critical",
                        tool=tool.name,
                        errorType=type(cleanup_error).__name__,
                    )
                    if primary_error is None:
                        raise

        async def retry_event(event: AgentEvent) -> None:
            await self._record_retry_event(tool.name, event, telemetry_span)
            if self.retry_event_sink is not None:
                await _maybe_await(self.retry_event_sink(dict(event)))
            await _emit(emit, event)

        execution = asyncio.create_task(
            execute_tool_with_retry(
                execute=execute_once,
                policy=tool.retry_policy,
                cancellation=tool_token,
                tool_call_id=tool_call_id,
                tool_name=tool.name,
                emit=retry_event,
            ),
            name=f"tool-execute:{tool.name}:{tool_call_id}",
        )
        cancel_wait = asyncio.create_task(tool_token.wait())
        async def wait_timeout() -> None:
            await execution_started.wait()
            await asyncio.sleep(cast(float, timeout))

        timeout_wait = asyncio.create_task(wait_timeout()) if timeout is not None else None
        primary: BaseException | None = None
        try:
            waiters = {execution, cancel_wait}
            if timeout_wait is not None:
                waiters.add(timeout_wait)
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if execution in done:
                try:
                    return await execution, False
                except (asyncio.CancelledError, OperationCancelledError):
                    if tool.replay_policy == "never" and execution_started.is_set():
                        return _outcome_unknown_result(
                            tool,
                            tool_call_id,
                            reason="tool_cancelled_after_dispatch",
                            message=(
                                f"工具 {tool.name} 已进入执行后被取消，"
                                "无法确认外部副作用是否已经提交。"
                            ),
                        ), True
                    return error_tool_result(
                        f"工具 {tool.name} 已取消：{tool_token.reason}",
                        details={"code": "tool_cancelled"},
                    ), True
                except OutcomeUnknownToolError as error:
                    return error_tool_result(
                        "工具执行结果未知，需要协调核对",
                        details={
                            "code": "outcome_unknown",
                            "operationId": error.operation_id,
                            "reconciliationName": error.reconciliation_name,
                            "toolCallId": tool_call_id,
                            "toolName": tool.name,
                            "outcomeUnknown": True,
                            "retryable": False,
                        },
                    ), True
                except ToolAttemptAdmissionDenied as error:
                    return copy.deepcopy(error.result), True
                except DefinitelyNotCommittedToolError as error:
                    return error_tool_result(
                        error,
                        details={
                            "code": error.code,
                            "toolCallId": tool_call_id,
                            "toolName": tool.name,
                            "definitelyNotCommitted": True,
                            "retryable": False,
                        },
                    ), True
                except RetryableToolError as error:
                    return error_tool_result(
                        error,
                        details={
                            "code": error.code,
                            "retryable": True,
                            "attempts": error.attempts,
                            **({"retryId": error.retry_id} if error.retry_id else {}),
                        },
                    ), True
                except ResourceLockTimeoutError as error:
                    return error_tool_result(
                        error, details={"code": "resource_lock_timeout"}
                    ), True
                except ResourceLeaseLostError as error:
                    return error_tool_result(
                        (
                            "工具资源租约丢失，执行结果未知，需要协调核对"
                            if error.outcome_unknown
                            else "工具执行已因资源租约丢失而终止"
                        ),
                        details={
                            "code": (
                                "outcome_unknown"
                                if error.outcome_unknown
                                else "resource_lease_lost"
                            ),
                            "reason": "resource_lease_lost",
                            "toolCallId": tool_call_id,
                            "toolName": tool.name,
                            "outcomeUnknown": error.outcome_unknown,
                            "retryable": False,
                        },
                    ), True
                except TenantContextError as error:
                    return error_tool_result(
                        error,
                        details={"code": error.code, "retryable": False},
                    ), True
                except Exception as error:
                    if getattr(error, "outcome_unknown", False) is True:
                        return _outcome_unknown_result(
                            tool,
                            tool_call_id,
                            reason="business_exception",
                            message="工具执行结果未知，需要协调核对",
                        ), True
                    if (
                        _tool_may_have_side_effect(tool)
                        and handler_dispatch_started.is_set()
                    ):
                        return _outcome_unknown_result(
                            tool,
                            tool_call_id,
                            reason="tool_exception_after_dispatch",
                            message=(
                                f"工具 {tool.name} 已进入执行后发生异常，"
                                "无法确认外部副作用是否已经提交。"
                            ),
                        ), True
                    return error_tool_result(
                        error, details={"code": "tool_execution_error"}
                    ), True
            if timeout_wait is not None and timeout_wait in done:
                accepting_updates = False
                tool_token.cancel(f"工具 {tool.name} 执行超过 {timeout:g} 秒")
                await _cancel_and_wait(execution)
                if tool.replay_policy == "never" and execution_started.is_set():
                    return _outcome_unknown_result(
                        tool,
                        tool_call_id,
                        reason="tool_timeout_after_dispatch",
                        message=(
                            f"工具 {tool.name} 已进入执行后超时，"
                            "无法确认外部副作用是否已经提交。"
                        ),
                        timeout_seconds=timeout,
                    ), True
                return error_tool_result(
                    f"工具 {tool.name} 执行超时（限制 {timeout:g} 秒）",
                    details={
                        "code": "tool_timeout",
                        "toolName": tool.name,
                        "timeoutSeconds": timeout,
                    },
                ), True
            accepting_updates = False
            await _cancel_and_wait(execution)
            if tool.replay_policy == "never" and execution_started.is_set():
                return _outcome_unknown_result(
                    tool,
                    tool_call_id,
                    reason="tool_cancelled_after_dispatch",
                    message=(
                        f"工具 {tool.name} 已进入执行后被取消，"
                        "无法确认外部副作用是否已经提交。"
                    ),
                ), True
            return error_tool_result(
                f"工具 {tool.name} 已取消：{tool_token.reason}",
                details={
                    "code": "tool_cancelled",
                    "toolName": tool.name,
                    "reason": tool_token.reason,
                },
            ), True
        except BaseException as error:
            primary = error
            raise
        finally:
            accepting_updates = False
            if not execution.done():
                tool_token.cancel(f"工具 {tool.name} 调度被取消")
                execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            waits = [cancel_wait] + ([timeout_wait] if timeout_wait is not None else [])
            for waiter in waits:
                if waiter is not None and not waiter.done():
                    waiter.cancel()
            await asyncio.gather(
                *[item for item in waits if item is not None],
                return_exceptions=True,
            )
            errors: list[BaseException] = list(completed_update_errors)
            if update_tasks:
                results = await asyncio.gather(*update_tasks, return_exceptions=True)
                errors.extend(
                    item
                    for item in results
                    if isinstance(item, BaseException)
                    and not isinstance(item, asyncio.CancelledError)
                )
            if coalesced_update is not None:
                try:
                    await _emit(
                        emit,
                        {
                            "type": "tool_execution_update",
                            "toolCallId": tool_call_id,
                            "toolName": tool.name,
                            "args": copy.deepcopy(prepared.tool_call.get("arguments", {})),
                            "partialResult": _result_payload(coalesced_update),
                            "coalesced": True,
                        },
                    )
                except BaseException as error:
                    errors.append(error)
            tool_token.detach()
            if primary is None and errors:
                raise errors[0]

    def _start_tool_span(self, tool_call: dict[str, Any]) -> TelemetrySpan:
        tool = self.tools.get(str(tool_call.get("name", "")))
        return self.telemetry.start_span(
            "tool.call",
            attributes={
                "toolCallId": str(tool_call.get("id", "")),
                "tool": tool.name if tool is not None else "__unknown__",
                "executionMode": (
                    _execution_mode(tool, "parallel") if tool is not None else "unknown"
                ),
                "priority": tool.priority if tool is not None else 0,
                "resourceAccess": tool.resource_access if tool is not None else "unknown",
                "replayPolicy": tool.replay_policy if tool is not None else "unknown",
            },
        )

    def _metric_tool_name(self, tool_call: dict[str, Any]) -> str:
        tool = self.tools.get(str(tool_call.get("name", "")))
        return tool.name if tool is not None else "__unknown__"

    def _trusted_tenant_id(self, tenant_id: str | None) -> str | None:
        value = tenant_id if tenant_id is not None else self.default_tenant_id
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            raise TenantContextError(
                "可信 Tenant Context 必须是非空字符串",
                code="tenant_context_invalid",
            )
        return value.strip() if value is not None else None

    async def _record_retry_event(
        self,
        tool_name: str,
        event: AgentEvent,
        span: TelemetrySpan | None,
    ) -> None:
        event_type = str(event.get("type", ""))
        if span is not None:
            span.add_event(
                event_type or "tool_retry_event",
                attempt=event.get("attempt"),
                delayMs=event.get("delayMs"),
                errorCode=event.get("errorCode") or event.get("finalError"),
                success=event.get("success"),
            )
        if event_type == "tool_retry_scheduled":
            self.telemetry.metrics.increment(
                "tool_retries_total",
                labels={
                    "tool": tool_name,
                    "error": str(event.get("errorCode", "unknown")),
                },
            )
            delay_ms = event.get("delayMs")
            if isinstance(delay_ms, (int, float)) and not isinstance(delay_ms, bool):
                self.telemetry.metrics.observe(
                    "tool_retry_delay_ms",
                    max(0.0, float(delay_ms)),
                    labels={"tool": tool_name},
                )
        elif event_type == "tool_retry_finished":
            self.telemetry.metrics.increment(
                "tool_retry_sequences_total",
                labels={
                    "tool": tool_name,
                    "outcome": "success" if event.get("success") is True else "error",
                },
            )
        if event_type in {
            "tool_retry_scheduled",
            "tool_retry_attempt_start",
            "tool_retry_finished",
        }:
            await self.telemetry.log(
                (
                    "info"
                    if event_type != "tool_retry_finished" or event.get("success")
                    else "warning"
                ),
                event_type,
                trace_id=span.trace_id if span is not None else None,
                span_id=span.span_id if span is not None else None,
                tool=tool_name,
                attempt=event.get("attempt"),
                delayMs=event.get("delayMs"),
                errorCode=event.get("errorCode") or event.get("finalError"),
                success=event.get("success"),
            )

    async def _record_tool_outcome(
        self,
        *,
        tool_name: str,
        outcome: ToolDispatchOutcome,
        started_at: float,
        span: TelemetrySpan,
    ) -> None:
        category, code = _tool_outcome_category(outcome)
        duration_ms = max(0.0, (time.monotonic() - started_at) * 1000)
        self.telemetry.record_tool_finished(
            tool_name,
            outcome=category,
            duration_ms=duration_ms,
        )
        if outcome.is_error:
            self.telemetry.metrics.increment(
                "tool_errors_total",
                labels={"tool": tool_name, "code": code},
            )
        await self.telemetry.log(
            "info" if not outcome.is_error else "warning",
            "tool_call_finished",
            trace_id=span.trace_id,
            span_id=span.span_id,
            tool=tool_name,
            outcome=category,
            errorCode=code if outcome.is_error else None,
            durationMs=duration_ms,
        )
        if category in {
            "timeout",
            "resource_lock_timeout",
            "resource_lease_lost",
            "outcome_unknown",
        }:
            await self.telemetry.alert(
                f"tool_{category}",
                tool=tool_name,
                errorCode=code,
            )
        await span.finish(
            status=(
                "ok"
                if not outcome.is_error
                else "cancelled"
                if category == "cancelled"
                else "error"
            ),
            attributes={"outcome": category, "errorCode": code if outcome.is_error else None},
        )

    async def _record_tool_exception(
        self,
        *,
        tool_name: str,
        outcome: str,
        started_at: float,
        span: TelemetrySpan,
        error: BaseException,
    ) -> None:
        duration_ms = max(0.0, (time.monotonic() - started_at) * 1000)
        self.telemetry.record_tool_finished(
            tool_name,
            outcome=outcome,
            duration_ms=duration_ms,
        )
        self.telemetry.metrics.increment(
            "tool_errors_total",
            labels={"tool": tool_name, "code": type(error).__name__},
        )
        await self.telemetry.log(
            "warning",
            "tool_call_failed",
            trace_id=span.trace_id,
            span_id=span.span_id,
            tool=tool_name,
            outcome=outcome,
            errorType=type(error).__name__,
            durationMs=duration_ms,
        )
        await span.finish(
            status="cancelled" if outcome == "cancelled" else "error",
            error=error,
            attributes={"outcome": outcome},
        )

    async def _after(
        self,
        prepared: PreparedToolCall,
        result: AgentToolResult,
        is_error: bool,
        context: AgentContext,
        assistant_message: AgentMessage,
        cancellation: CancellationToken,
    ) -> tuple[AgentToolResult, bool]:
        if self.after_tool_call is None:
            return result, is_error
        try:
            override = await _maybe_await(
                self.after_tool_call(
                    AfterToolCallContext(
                        assistant_message=copy.deepcopy(assistant_message),
                        tool_call=copy.deepcopy(prepared.tool_call),
                        args=copy.deepcopy(prepared.args),
                        result=copy.deepcopy(result),
                        is_error=is_error,
                        context=_copy_agent_context(context),
                    ),
                    cancellation,
                )
            )
            if isinstance(override, AfterToolCallResult):
                result = AgentToolResult(
                    content=result.content if override.content is UNSET else list(override.content),
                    details=result.details if override.details is UNSET else override.details,
                    usage=result.usage if override.usage is UNSET else override.usage,
                    added_tool_names=result.added_tool_names,
                    terminate=(
                        result.terminate
                        if override.terminate is UNSET
                        else override.terminate
                    ),
                )
                if override.is_error is not UNSET:
                    is_error = bool(override.is_error)
        except Exception as error:
            if not is_error and _tool_may_have_side_effect(prepared.tool):
                return (
                    _committed_tool_output_unavailable(
                        prepared.tool,
                        str(prepared.tool_call.get("id", "")),
                        result,
                    ),
                    False,
                )
            return error_tool_result(error), True
        return result, is_error


def _execution_mode(tool: AgentTool, global_mode: str) -> str:
    if global_mode == "sequential":
        return "exclusive"
    mode = tool.execution_mode or "parallel"
    return "exclusive" if mode == "sequential" else mode


def _copy_agent_context(context: AgentContext) -> AgentContext:
    """复制 Hook 可观察状态，不复制 Tool callable 等装配对象。"""

    return AgentContext(
        system_prompt=context.system_prompt,
        messages=copy.deepcopy(context.messages),
        tools=list(context.tools),
    )


def _copy_tool_dispatch_context(
    context: ToolDispatchContext,
) -> ToolDispatchContext:
    return ToolDispatchContext(
        identity=context.identity,
        approval=copy.deepcopy(context.approval),
        tenant_id=context.tenant_id,
        fencing_token=context.fencing_token,
        fencing_scope=context.fencing_scope,
        resource_fencing_token=context.resource_fencing_token,
        resource_fencing_scope=context.resource_fencing_scope,
    )


def _with_resource_fencing(
    context: ToolDispatchContext,
    *,
    token: int,
    scope: str | None,
) -> ToolDispatchContext:
    if scope is None:
        raise ToolDispatchError("Resource Fencing token 缺少作用域")
    return ToolDispatchContext(
        identity=context.identity,
        approval=copy.deepcopy(context.approval),
        tenant_id=context.tenant_id,
        fencing_token=context.fencing_token,
        fencing_scope=context.fencing_scope,
        resource_fencing_token=token,
        resource_fencing_scope=scope,
    )


def _resolve_dispatch_context(
    dispatch_context: ToolDispatchContext | None,
    *,
    identity: Any,
    approval: Any,
    tenant_id: str | None,
) -> ToolDispatchContext:
    if dispatch_context is None:
        return ToolDispatchContext(
            identity=identity,
            approval=approval,
            tenant_id=tenant_id,
        )
    if not isinstance(dispatch_context, ToolDispatchContext):
        raise TypeError("dispatch_context 必须是 ToolDispatchContext")
    if identity is not None or approval is not None:
        raise TypeError("不能同时提供 dispatch_context 与旧版 identity/approval")
    if (
        tenant_id is not None
        and dispatch_context.tenant_id is not None
        and tenant_id.strip() != dispatch_context.tenant_id
    ):
        raise TenantContextError(
            "Tool Dispatch Context 与显式 Tenant ID 不一致",
            code="tenant_context_mismatch",
        )
    if dispatch_context.tenant_id is None and tenant_id is not None:
        return ToolDispatchContext(
            identity=dispatch_context.identity,
            approval=dispatch_context.approval,
            tenant_id=tenant_id,
            fencing_token=dispatch_context.fencing_token,
            fencing_scope=dispatch_context.fencing_scope,
            resource_fencing_token=dispatch_context.resource_fencing_token,
            resource_fencing_scope=dispatch_context.resource_fencing_scope,
        )
    return dispatch_context


def _historical_tool_call_ids(
    context: AgentContext,
    assistant_message: AgentMessage,
) -> set[str]:
    identifiers: set[str] = set()
    for message in context.messages:
        # 正常 Agent Loop 会先把本轮 Assistant Message 加入 Context；它不是
        # “历史”，只能按对象身份排除，不能因内容相等而放过真正的旧消息。
        if message is assistant_message or message.get("role") != "assistant":
            continue
        for block in message.get("content", []):
            if not isinstance(block, dict) or block.get("type") != "toolCall":
                continue
            value = block.get("id")
            if isinstance(value, str) and value.strip():
                identifiers.add(value.strip())
    return identifiers


def _validate_tool_call_batch(
    tool_calls: list[dict[str, Any]],
    *,
    context: AgentContext,
    assistant_message: AgentMessage,
) -> tuple[list[dict[str, Any]], str | None]:
    """在任何授权/Hook/副作用前校验整个批次的 Tool Call ID。"""

    historical = _historical_tool_call_ids(context, assistant_message)
    current: set[str] = set()
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw_call in enumerate(tool_calls):
        if not isinstance(raw_call, dict):
            call: dict[str, Any] = {
                "type": "toolCall",
                "name": "",
                "arguments": {},
            }
            reason = "不是对象"
        else:
            call = copy.deepcopy(raw_call)
            raw_id = call.get("id")
            identifier = raw_id.strip() if isinstance(raw_id, str) else ""
            if not identifier:
                reason = "ID 为空或不是字符串"
            elif identifier in historical:
                reason = "ID 已在历史中使用"
            elif identifier in current:
                reason = "ID 在当前批次中重复"
            else:
                reason = ""
                call["id"] = identifier
                current.add(identifier)
        if reason:
            replacement = f"rejected-tool-call-{uuid4()}"
            call["id"] = replacement
            current.add(replacement)
            errors.append(f"第 {index + 1} 个 Tool Call {reason}")
        normalized.append(call)
    return normalized, "；".join(errors) if errors else None


def _resource_keys(tool: AgentTool, args: Any) -> tuple[str, ...]:
    if tool.execution_mode != "resource_locked":
        return ()
    if tool.resolve_resource_keys is None:
        raise ToolDispatchError(f"工具 {tool.name} 缺少 resolve_resource_keys")
    raw = tool.resolve_resource_keys(args)
    values = [raw] if isinstance(raw, str) else list(raw)
    if not values or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ToolDispatchError(f"工具 {tool.name} 返回了无效 Resource Key")
    return tuple(sorted(set(value.strip() for value in values)))


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("等待 Tool Scheduler 超时")
    return remaining


def _outcome_unknown_result(
    tool: AgentTool,
    tool_call_id: str,
    *,
    reason: str,
    message: str,
    timeout_seconds: float | None = None,
) -> AgentToolResult:
    details: dict[str, Any] = {
        "code": "outcome_unknown",
        "outcomeUnknown": True,
        "reason": reason,
        "toolCallId": tool_call_id,
        "toolName": tool.name,
        "retryable": False,
    }
    if timeout_seconds is not None:
        details["timeoutSeconds"] = timeout_seconds
    return error_tool_result(message, details=details)


def _result_is_outcome_unknown(result: AgentToolResult) -> bool:
    return (
        isinstance(result.details, dict)
        and result.details.get("code") == "outcome_unknown"
        and result.details.get("outcomeUnknown") is True
    )


def _committed_tool_output_unavailable(
    tool: AgentTool,
    tool_call_id: str,
    result: AgentToolResult,
) -> AgentToolResult:
    """Preserve a confirmed commit without releasing uninspected output."""

    return AgentToolResult(
        content=[
            {
                "type": "text",
                "text": (
                    "工具已执行成功，但结果后处理未完成；"
                    "原始输出未公开，已停止后续自动执行。"
                ),
            }
        ],
        details={
            "code": "tool_output_unavailable_after_commit",
            "toolCallId": tool_call_id,
            "toolName": tool.name,
            "effectCommitted": True,
            "retryable": False,
        },
        usage=copy.deepcopy(result.usage),
        added_tool_names=None,
        terminate=True,
    )


def _tool_may_have_side_effect(tool: AgentTool) -> bool:
    """Classify unsafe tools from the sealed metadata available to Runtime."""

    return (
        tool.replay_policy == "never"
        or tool.requires_approval
        or tool.supports_resource_fencing
    )


def _tool_outcome_category(outcome: ToolDispatchOutcome) -> tuple[str, str]:
    if not outcome.is_error:
        return "success", "none"
    details = outcome.result.details
    code = str(details.get("code", "tool_error")) if isinstance(details, dict) else "tool_error"
    if code in {"tool_cancelled", "tool_aborted_before_dispatch"}:
        return "cancelled", code
    if code == "tool_timeout":
        return "timeout", code
    if code == "resource_lock_timeout":
        return "resource_lock_timeout", code
    if code == "resource_lease_lost":
        return "resource_lease_lost", code
    if code == "outcome_unknown":
        return "outcome_unknown", code
    return "error", code


def _cancelled_before_dispatch(tool_name: str) -> ImmediateToolCall:
    return ImmediateToolCall(
        error_tool_result(
            f"工具 {tool_name} 调用在执行前因 Agent 取消而跳过。",
            details={"code": "tool_aborted_before_dispatch", "synthetic": True},
        ),
        True,
    )


async def _cancel_and_wait(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _maybe_await(value: Any) -> Any:
    return await cast(Awaitable[Any], value) if inspect.isawaitable(value) else value


async def _emit(sink: EventSink, event: AgentEvent) -> None:
    await _maybe_await(sink(event))


async def _discard_event(_event: AgentEvent) -> None:
    return None


async def _emit_tool_start(tool_call: dict[str, Any], emit: EventSink) -> None:
    await _emit(
        emit,
        {
            "type": "tool_execution_start",
            "toolCallId": str(tool_call.get("id", "")),
            "toolName": str(tool_call.get("name", "")),
            "args": copy.deepcopy(tool_call.get("arguments", {})),
        },
    )


async def _emit_tool_end(outcome: ToolDispatchOutcome, emit: EventSink) -> None:
    message = _tool_result_message(outcome)
    await _emit(
        emit,
        {
            "type": "tool_execution_end",
            "toolCallId": str(outcome.tool_call.get("id", "")),
            "toolName": str(outcome.tool_call.get("name", "")),
            "result": _result_payload(outcome.result),
            "isError": outcome.is_error,
            "toolResultMessage": clone_message(message),
        },
    )


def _tool_result_message(outcome: ToolDispatchOutcome) -> AgentMessage:
    if outcome.result_message is not None:
        return outcome.result_message
    message: AgentMessage = {
        "role": "toolResult",
        "toolCallId": str(outcome.tool_call.get("id", "")),
        "toolName": str(outcome.tool_call.get("name", "")),
        "content": copy.deepcopy(outcome.result.content or []),
        "details": copy.deepcopy(outcome.result.details),
        "isError": outcome.is_error,
        "timestamp": now_ms(),
    }
    if outcome.result.usage is not None:
        message["usage"] = copy.deepcopy(outcome.result.usage)
    if outcome.result.added_tool_names:
        message["addedToolNames"] = list(outcome.result.added_tool_names)
    outcome.result_message = message
    return message


async def _emit_result_message(message: AgentMessage, emit: EventSink) -> None:
    await _emit(emit, {"type": "message_start", "message": clone_message(message)})
    await _emit(emit, {"type": "message_end", "message": message})


def _result_payload(result: AgentToolResult) -> dict[str, Any]:
    payload = {
        "content": copy.deepcopy(result.content),
        "details": copy.deepcopy(result.details),
    }
    if result.usage is not None:
        payload["usage"] = copy.deepcopy(result.usage)
    if result.added_tool_names:
        payload["addedToolNames"] = list(result.added_tool_names)
    if result.terminate is not None:
        payload["terminate"] = result.terminate
    return payload


def _validate_lock_request(
    keys: tuple[str, ...], owner: str, access: str, timeout: float
) -> None:
    if not keys or any(not key for key in keys):
        raise ValueError("Resource Key 不能为空")
    if not owner:
        raise ValueError("Resource Lock owner_token 不能为空")
    if access not in {"read", "write"}:
        raise ValueError("Resource Lock access 必须是 read 或 write")
    if timeout <= 0:
        raise ValueError("Resource Lock timeout 必须大于 0")


__all__ = [
    "DefinitelyNotCommittedToolError",
    "ImmediateToolCall",
    "PreparedToolCall",
    "ResourceLockBackend",
    "ResourceLockBackendCapabilities",
    "ResourceFencingLease",
    "ResourceLockTimeoutError",
    "ResourceLockRegistry",
    "SQLiteResourceLockBackend",
    "ToolDispatchBatch",
    "ToolDispatchError",
    "ToolAttemptAdmissionDenied",
    "ToolDispatchOutcome",
    "ToolDispatchRuntime",
    "ToolScheduler",
    "ToolSchedulerStats",
]
