from collections.abc import Sequence

revision: str = "3e1f0c7a9b2d"
down_revision: str | Sequence[str] | None = ("d15c37b0d793", "f5a6b7c8d9e0")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
