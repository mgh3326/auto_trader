from collections.abc import Sequence

revision: str = "1c9e8d7f6a5b"
down_revision: str | Sequence[str] | None = ("d4f1c5a3e2b1", "daf4130b13ce")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
