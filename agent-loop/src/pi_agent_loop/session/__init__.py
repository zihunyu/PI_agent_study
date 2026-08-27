"""Runtime Event 持久化、重放与崩溃恢复。"""

from .jsonl import JsonlRuntimeEventStore
from .operation_events import OperationEvent
from .operation_state import (
    ModelRequestState,
    OperationLogInvariantError,
    OperationState,
    ToolInvocationState,
    reduce_operation_event,
    replay_operation,
)
from .operation_store import (
    InMemoryOperationEventStore,
    JsonlOperationEventStore,
    OperationEventStore,
)
from .recorder import DurableOperationRecorder
from .recovery import RuntimeRecoveryManager
from .resume import (
    DurableSessionRecovery,
    OperationRecoveryPlan,
    RecoveryAction,
    RecoveryCallbacks,
    RecoveryExecutionResult,
)
from .replay import replay_runtime_events
from .store import InMemoryRuntimeEventStore, RuntimeEventStore

__all__ = [
    "DurableOperationRecorder",
    "DurableSessionRecovery",
    "InMemoryOperationEventStore",
    "InMemoryRuntimeEventStore",
    "JsonlOperationEventStore",
    "JsonlRuntimeEventStore",
    "ModelRequestState",
    "OperationEvent",
    "OperationEventStore",
    "OperationLogInvariantError",
    "OperationRecoveryPlan",
    "OperationState",
    "RecoveryAction",
    "RecoveryCallbacks",
    "RecoveryExecutionResult",
    "RuntimeEventStore",
    "RuntimeRecoveryManager",
    "ToolInvocationState",
    "reduce_operation_event",
    "replay_operation",
    "replay_runtime_events",
]
