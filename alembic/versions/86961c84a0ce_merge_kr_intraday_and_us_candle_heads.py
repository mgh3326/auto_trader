from collections.abc import Sequence

revision: str = "86961c84a0ce"
down_revision: str | Sequence[str] | None = ("5c6d7e8f9012", "a9d6e4c2b1f0")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
