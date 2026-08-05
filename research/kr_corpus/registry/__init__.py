"""Hash-bound registry for the KR-A0 packet candidates.

This package is deliberately separate from the legacy Stage-B implementation.
"""

from .exact_binding import (
    ArtifactPaths,
    CandidateBinding,
    CandidateRegistry,
    NeedsUpstream,
    RegistryStartRejected,
    VerifiedInputs,
    sha256_file,
)

__all__ = [
    "ArtifactPaths",
    "CandidateBinding",
    "CandidateRegistry",
    "NeedsUpstream",
    "RegistryStartRejected",
    "VerifiedInputs",
    "sha256_file",
]
