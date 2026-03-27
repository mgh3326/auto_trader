from collections.abc import Sequence

revision: str = "2d4e6f8a0b1c"
down_revision: str | Sequence[str] | None = ("672f39265fed", "f3a4b5c6d7e8")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
