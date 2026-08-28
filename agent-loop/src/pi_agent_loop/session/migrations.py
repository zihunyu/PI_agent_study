"""Session Journal 的事件与快照版本迁移注册表。

历史事件保持只读。事件迁移采用读取时逐版本 upcast；快照是可重建缓存，
可以在验证迁移链后由 Journal 重写为新版本。
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any


class JournalMigrationError(RuntimeError):
    """迁移链缺失、版本非法或迁移器返回无效结果。"""


PayloadMigration = Callable[[dict[str, Any]], dict[str, Any]]
StateMigration = Callable[[Any], Any]


class EventMigrationRegistry:
    """按 Journal Kind + Event Type 注册逐版本事件 upcaster。"""

    def __init__(self) -> None:
        self._steps: dict[
            tuple[str, str, int], tuple[int, PayloadMigration]
        ] = {}

    def register(
        self,
        journal_kind: str,
        event_type: str,
        from_version: int,
        to_version: int,
        migration: PayloadMigration,
    ) -> None:
        _validate_identity(journal_kind, "journal_kind")
        _validate_identity(event_type, "event_type")
        _validate_step(from_version, to_version)
        key = (journal_kind, event_type, from_version)
        if key in self._steps:
            raise JournalMigrationError(
                "Event Migration 重复注册："
                f"{journal_kind}/{event_type}/v{from_version}"
            )
        self._steps[key] = (to_version, migration)

    def migrate(
        self,
        journal_kind: str,
        event_type: str,
        payload: dict[str, Any],
        from_version: int,
        target_version: int,
    ) -> tuple[dict[str, Any], int]:
        _validate_target(from_version, target_version)
        current = copy.deepcopy(payload)
        version = from_version
        while version < target_version:
            step = self._steps.get((journal_kind, event_type, version))
            if step is None:
                raise JournalMigrationError(
                    "缺少 Event Migration："
                    f"{journal_kind}/{event_type}/v{version}->v{version + 1}"
                )
            next_version, migration = step
            migrated = migration(copy.deepcopy(current))
            if not isinstance(migrated, dict):
                raise JournalMigrationError("Event Migration 必须返回对象")
            current = copy.deepcopy(migrated)
            version = next_version
        return current, version


class StateMigrationRegistry:
    """按 Projection Name 注册逐版本 Snapshot 状态迁移。"""

    def __init__(self) -> None:
        self._steps: dict[tuple[str, int], tuple[int, StateMigration]] = {}

    def register(
        self,
        projection_name: str,
        from_version: int,
        to_version: int,
        migration: StateMigration,
    ) -> None:
        _validate_identity(projection_name, "projection_name")
        _validate_step(from_version, to_version)
        key = (projection_name, from_version)
        if key in self._steps:
            raise JournalMigrationError(
                f"State Migration 重复注册：{projection_name}/v{from_version}"
            )
        self._steps[key] = (to_version, migration)

    def migrate(
        self,
        projection_name: str,
        state: Any,
        from_version: int,
        target_version: int,
    ) -> tuple[Any, int]:
        _validate_target(from_version, target_version)
        current = copy.deepcopy(state)
        version = from_version
        while version < target_version:
            step = self._steps.get((projection_name, version))
            if step is None:
                raise JournalMigrationError(
                    "缺少 State Migration："
                    f"{projection_name}/v{version}->v{version + 1}"
                )
            next_version, migration = step
            current = copy.deepcopy(migration(copy.deepcopy(current)))
            version = next_version
        return current, version


def _validate_identity(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise JournalMigrationError(f"{name} 不能为空")


def _validate_step(from_version: int, to_version: int) -> None:
    if (
        isinstance(from_version, bool)
        or isinstance(to_version, bool)
        or not isinstance(from_version, int)
        or not isinstance(to_version, int)
        or from_version < 1
        or to_version != from_version + 1
    ):
        raise JournalMigrationError("Migration 必须逐版本注册且版本从 1 开始")


def _validate_target(from_version: int, target_version: int) -> None:
    if (
        isinstance(from_version, bool)
        or isinstance(target_version, bool)
        or not isinstance(from_version, int)
        or not isinstance(target_version, int)
        or from_version < 1
        or target_version < from_version
    ):
        raise JournalMigrationError("Migration 目标版本无效")
