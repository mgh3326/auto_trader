from collections.abc import Sequence

revision: str = "a9d6e4c2b1f0"
down_revision: str | Sequence[str] | None = ("a1b2c3d4e5f6", "f8b6c4d2e1a3")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
