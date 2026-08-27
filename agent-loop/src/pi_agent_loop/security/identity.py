"""可信身份接口和仅供本地测试的静态验证器。"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class IdentityClaim:
    principal_id: str
    credential: str


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    principal_id: str
    roles: frozenset[str]
    issuer: str
    verification_id: str


class IdentityVerifier(Protocol):
    async def verify(self, claim: IdentityClaim) -> VerifiedIdentity: ...


class IdentityVerificationError(PermissionError):
    pass


class StaticIdentityVerifier:
    """开发/测试骨架；生产环境必须替换为 OAuth、IAM 或企业认证。"""

    def __init__(
        self,
        principals: dict[str, tuple[str, set[str] | frozenset[str]]],
    ) -> None:
        self._principals = {
            principal_id: (
                _digest(credential),
                frozenset(roles),
            )
            for principal_id, (credential, roles) in principals.items()
        }

    async def verify(self, claim: IdentityClaim) -> VerifiedIdentity:
        record = self._principals.get(claim.principal_id)
        if record is None or not hmac.compare_digest(record[0], _digest(claim.credential)):
            raise IdentityVerificationError("身份凭证无效")
        return VerifiedIdentity(
            principal_id=claim.principal_id,
            roles=record[1],
            issuer="static-development-verifier",
            verification_id=str(uuid4()),
        )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
