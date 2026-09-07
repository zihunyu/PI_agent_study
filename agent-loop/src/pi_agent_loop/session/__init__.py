"""Runtime Event 持久化、重放与崩溃恢复。"""

from .catalog import (
    ConversationSession,
    ConversationSessionNotFoundError,
    SessionConfigurationMismatchError,
    WorkspaceCatalogError,
    WorkspaceNotFoundError,
    WorkspaceProject,
    WorkspaceSessionCatalog,
)
from .context_projection import (
    SessionContext,
    SessionContextProjection,
    SessionContextProjectionError,
)
from .jsonl import JsonlRuntimeEventStore
from .journal import (
    EnvironmentJournalKeyProvider,
    JournalAccessDenied,
    JournalAccessPolicy,
    JournalConflictError,
    JournalCorruptionError,
    JournalDeadlineExceeded,
    JournalFencedClaimLostError,
    JournalEncryptionKey,
    JournalKeyProvider,
    JournalPrincipal,
    JournalRedactionPolicy,
    ProjectionReplay,
    SessionEvent,
    SessionEventSpec,
    SessionJournalError,
    SessionSnapshot,
    SQLiteSessionEventJournal,
    StaticJournalKeyProvider,
)
from .journal_adapters import (
    SessionJournalOperationEventStore,
    SessionJournalRetryEventStore,
    SessionJournalRuntimeEventStore,
)
from .legacy_import import (
    LegacyConversationImportError,
    LegacyConversationImportResult,
    import_legacy_jsonl_conversation,
)
from .migrations import (
    EventMigrationRegistry,
    JournalMigrationError,
    StateMigrationRegistry,
)
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
    ClaimLease,
    InMemoryOperationEventStore,
    JsonlOperationEventStore,
    OperationEventStore,
    OperationStoreConflictError,
    OperationStoreDeadlineExceeded,
)
from .sqlite import (
    SQLiteOperationEventStore,
    SQLiteRuntimeEventStore,
    SQLiteRuntimeStoreMigrationRequiredError,
    migrate_legacy_sqlite_runtime_events,
)
from .recorder import DurableOperationRecorder
from .recovery import RuntimeRecoveryClaimError, RuntimeRecoveryManager
from .resume import (
    DurableSessionRecovery,
    OperationRecoveryPlan,
    RecoveryAction,
    RecoveryCallbacks,
    RecoveryExecutionResult,
)
from .replay import replay_runtime_events
from .store import (
    InMemoryRuntimeEventStore,
    RuntimeEventStore,
    RuntimeStoreConflictError,
)

from .journal import SessionEventJournal, SessionJournalCapabilities, SynchronousSessionEventJournal, validate_session_event_journal
from .journal_adapters import JournalOperationStore

__all__ = [
    "DurableOperationRecorder",
    "DurableSessionRecovery",
    "ConversationSession",
    "ConversationSessionNotFoundError",
    "ClaimLease",
    "InMemoryOperationEventStore",
    "InMemoryRuntimeEventStore",
    "ApprovalSnapshot",
    "EnvironmentJournalKeyProvider",
    "EventMigrationRegistry",
    "JournalAccessDenied",
    "JournalAccessPolicy",
    "JournalConflictError",
    "JournalCorruptionError",
    "JournalDeadlineExceeded",
    "JournalFencedClaimLostError",
    "JournalEncryptionKey",
    "JournalKeyProvider",
    "JournalMigrationError",
    "JournalPrincipal",
    "JournalRedactionPolicy",
    "JsonlOperationEventStore",
    "JsonlRuntimeEventStore",
    "LegacyConversationImportError",
    "LegacyConversationImportResult",
    "ModelRequestState",
    "OperationEvent",
    "OperationEventStore",
    "OperationLogInvariantError",
    "OperationStoreConflictError",
    "OperationStoreDeadlineExceeded",
    "OperationRecoveryPlan",
    "OperationState",
    "ProjectionReplay",
    "RecoveryAction",
    "RecoveryCallbacks",
    "RecoveryExecutionResult",
    "RuntimeEventStore",
    "RuntimeRecoveryClaimError",
    "RuntimeRecoveryManager",
    "RuntimeStoreConflictError",
    "SessionConfigurationMismatchError",
    "SessionContext",
    "SessionContextProjection",
    "SessionContextProjectionError",
    "SessionEvent",
    "SessionEventSpec",
    "SessionJournalError",
    "SessionJournalOperationEventStore",
    "SessionJournalRetryEventStore",
    "SessionJournalRuntimeEventStore",
    "SessionSnapshot",
    "SQLiteSessionEventJournal",
    "SQLiteOperationEventStore",
    "SQLiteRuntimeEventStore",
    "SQLiteRuntimeStoreMigrationRequiredError",
    "ToolInvocationState",
    "StateMigrationRegistry",
    "StaticJournalKeyProvider",
    "WriteSnapshot",
    "WorkspaceCatalogError",
    "WorkspaceNotFoundError",
    "WorkspaceProject",
    "WorkspaceSessionCatalog",
    "import_legacy_jsonl_conversation",
    "migrate_legacy_sqlite_runtime_events",
    "reduce_operation_event",
    "replay_operation",
    "replay_runtime_events",
]

__all__ += ['SessionEventJournal', 'SessionJournalCapabilities', 'SynchronousSessionEventJournal', 'validate_session_event_journal', 'JournalOperationStore']
