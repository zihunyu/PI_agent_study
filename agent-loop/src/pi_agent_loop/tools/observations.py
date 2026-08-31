"""文件 Observation/CAS 令牌。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .errors import WorkspaceToolError
from .path_policy import WorkspacePathPolicy
from .text_files import read_bytes_limited


@dataclass(frozen=True, slots=True)
class FileObservation:
    path: str
    version: str
    size: int
    mtime_ns: int


class FileObservationStore:
    """进程内 Observation 表；修改时仍会重新哈希磁盘内容。"""

    def __init__(self, policy: WorkspacePathPolicy) -> None:
        self._policy = policy
        self._observations: dict[str, FileObservation] = {}

    def observe(self, path: Path, data: bytes) -> FileObservation:
        stat = path.stat()
        observation = FileObservation(
            path=str(path),
            version=hashlib.sha256(data).hexdigest(),
            size=len(data),
            mtime_ns=stat.st_mtime_ns,
        )
        self._observations[self._policy.key(path)] = observation
        return observation

    def get(self, path: Path) -> FileObservation | None:
        return self._observations.get(self._policy.key(path))

    def assert_current(
        self,
        path: Path,
        expected_version: str | None,
        *,
        maximum_bytes: int = 20 * 1024 * 1024,
    ) -> bytes:
        observed = self.get(path)
        if observed is None or not expected_version:
            raise WorkspaceToolError(
                "observation_required",
                "覆盖或编辑已有文件前必须先 read，并传入 observation",
                path=str(path),
            )
        try:
            data = read_bytes_limited(path, maximum_bytes)
        except FileNotFoundError as error:
            raise WorkspaceToolError(
                "stale_observation",
                "文件在读取后已被删除，请重新检查",
                path=str(path),
                details={"expectedVersion": expected_version},
            ) from error
        actual = hashlib.sha256(data).hexdigest()
        if observed.version != expected_version or actual != expected_version:
            raise WorkspaceToolError(
                "stale_observation",
                "文件已在读取后发生变化，请重新 read",
                path=str(path),
                details={
                    "expectedVersion": expected_version,
                    "observedVersion": observed.version,
                    "actualVersion": actual,
                },
            )
        return data

    def forget(self, path: Path) -> None:
        self._observations.pop(self._policy.key(path), None)


def file_version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
