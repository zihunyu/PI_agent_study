"""Runtime Event 持久化、重放与崩溃恢复。"""

from .jsonl import JsonlRuntimeEventStore
from .operation_events import OperationEvent
from .operation_state import (
    ApprovalSnapshot,
    ModelRequestState,
    OperationLogInvariantError,
    OperationState,
    ToolInvocationState,
    WriteSnapshot,
    reduce_operation_event,
    replay_operation,
)
from .operation_store import (
    InMemoryOperationEventStore,
    JsonlOperationEventStore,
    OperationEventStore,
    OperationStoreConflictError,
)
from .sqlite import SQLiteOperationEventStore, SQLiteRuntimeEventStore
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
    "ApprovalSnapshot",
    "JsonlOperationEventStore",
    "JsonlRuntimeEventStore",
    "ModelRequestState",
    "OperationEvent",
    "OperationEventStore",
    "OperationLogInvariantError",
    "OperationStoreConflictError",
    "OperationRecoveryPlan",
    "OperationState",
    "RecoveryAction",
    "RecoveryCallbacks",
    "RecoveryExecutionResult",
    "RuntimeEventStore",
    "RuntimeRecoveryManager",
    "SQLiteOperationEventStore",
    "SQLiteRuntimeEventStore",
    "ToolInvocationState",
    "WriteSnapshot",
    "reduce_operation_event",
    "replay_operation",
    "replay_runtime_events",
]
