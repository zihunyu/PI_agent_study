"""可信身份接口和仅供本地测试的静态验证器。"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias
from uuid import uuid4


_IDENTITY_PROVENANCE_VERSION = "pi-agent-loop-verified-identity-v1"
# This process-local seal prevents callers from turning an arbitrary duck object or
# a directly constructed ``VerifiedIdentity`` into a trusted Approval principal.
# Production identity adapters can supply their own validator to ApprovalService;
# they should verify an OIDC/JWT/IAM proof instead of relying on this local seal.
_IDENTITY_PROVENANCE_KEY = secrets.token_bytes(32)


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
    _provenance: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("principal_id", self.principal_id),
            ("issuer", self.issuer),
            ("verification_id", self.verification_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"VerifiedIdentity {name} 必须是非空字符串")
        if not isinstance(self.roles, frozenset) or any(
            not isinstance(role, str) or not role.strip() for role in self.roles
        ):
            raise TypeError("VerifiedIdentity roles 必须是非空字符串 frozenset")
        if self._provenance is not None and (
            not isinstance(self._provenance, str) or not self._provenance
        ):
            raise ValueError("VerifiedIdentity provenance 必须是非空字符串或 None")


VerifiedIdentityValidator: TypeAlias = Callable[[VerifiedIdentity], bool]


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
        return _issue_local_verified_identity(
            principal_id=claim.principal_id,
            roles=record[1],
            issuer="static-development-verifier",
            verification_id=str(uuid4()),
        )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_local_identity_provenance(identity: VerifiedIdentity) -> bool:
    """Validate the default verifier provenance without trusting caller fields.

    The default Approval boundary accepts only identities issued by this module's
    verifier in the current process.  A production adapter can inject a validator
    that checks its own cryptographic proof while retaining the exact-type check
    performed by :class:`ApprovalService`.
    """

    if type(identity) is not VerifiedIdentity or identity._provenance is None:
        return False
    expected = _identity_provenance(
        principal_id=identity.principal_id,
        roles=identity.roles,
        issuer=identity.issuer,
        verification_id=identity.verification_id,
    )
    return hmac.compare_digest(identity._provenance, expected)


def _issue_local_verified_identity(
    *,
    principal_id: str,
    roles: frozenset[str],
    issuer: str,
    verification_id: str,
) -> VerifiedIdentity:
    provenance = _identity_provenance(
        principal_id=principal_id,
        roles=roles,
        issuer=issuer,
        verification_id=verification_id,
    )
    return VerifiedIdentity(
        principal_id=principal_id,
        roles=roles,
        issuer=issuer,
        verification_id=verification_id,
        _provenance=provenance,
    )


def _identity_provenance(
    *,
    principal_id: str,
    roles: frozenset[str],
    issuer: str,
    verification_id: str,
) -> str:
    payload = json.dumps(
        {
            "version": _IDENTITY_PROVENANCE_VERSION,
            "principalId": principal_id,
            "roles": sorted(roles),
            "issuer": issuer,
            "verificationId": verification_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.new(
        _IDENTITY_PROVENANCE_KEY,
        payload,
        hashlib.sha256,
    ).hexdigest()
