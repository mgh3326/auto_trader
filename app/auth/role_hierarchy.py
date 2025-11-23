"""Centralized role hierarchy helpers."""
from enum import IntEnum

from app.models.trading import UserRole


class RoleHierarchy(IntEnum):
    """Role levels for comparison (viewer < trader < admin)."""

    viewer = 0
    trader = 1
    admin = 2

    @classmethod
    def from_user_role(cls, role: UserRole) -> "RoleHierarchy":
        """Convert UserRole to comparable hierarchy level."""
        return cls[role.value]


def has_min_role(user_role: UserRole, required_role: UserRole) -> bool:
    """Return True if user_role meets or exceeds required_role."""
    try:
        return RoleHierarchy.from_user_role(user_role) >= RoleHierarchy.from_user_role(
            required_role
        )
    except KeyError:
        return False
