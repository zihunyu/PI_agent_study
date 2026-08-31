"""可信身份验证边界。"""

from .identity import (
    IdentityClaim,
    IdentityVerificationError,
    IdentityVerifier,
    StaticIdentityVerifier,
    VerifiedIdentity,
    VerifiedIdentityValidator,
    validate_local_identity_provenance,
)

__all__ = [
    "IdentityClaim",
    "IdentityVerificationError",
    "IdentityVerifier",
    "StaticIdentityVerifier",
    "VerifiedIdentity",
    "VerifiedIdentityValidator",
    "validate_local_identity_provenance",
]
