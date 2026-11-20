"""Fence-area coordination tables (hk 3795 T4c, design §4.6 F2 and F3s/F3/F4).

Neither table holds an amount or an authority value. ``mandate_basket_authority``
is the row a send-fence transaction share-locks and every writer of a clause,
profile, sleeve version or plan registration bumps; ``mandate_cap_mutex`` holds
one row per serialization key. DDL: ``alembic/versions/20261120_hk5651_mandate_fence.py``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    PrimaryKeyConstraint,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class MandateBasketAuthority(Base):
    __tablename__ = "mandate_basket_authority"
    __table_args__ = (
        PrimaryKeyConstraint("realm", "basket_key", name="pk_mandate_basket_authority"),
        CheckConstraint(
            "realm IN ('live','mock')", name="ck_mandate_basket_authority_realm"
        ),
        CheckConstraint(
            "char_length(btrim(basket_key)) BETWEEN 1 AND 120",
            name="ck_mandate_basket_authority_basket_key",
        ),
        CheckConstraint("authority_seq >= 0", name="ck_mandate_basket_authority_seq"),
        {"schema": "review"},
    )

    realm: Mapped[str] = mapped_column(Text, nullable=False)
    basket_key: Mapped[str] = mapped_column(Text, nullable=False)
    authority_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class MandateCapMutex(Base):
    __tablename__ = "mandate_cap_mutex"
    __table_args__ = (
        PrimaryKeyConstraint("mutex_key", name="pk_mandate_cap_mutex"),
        CheckConstraint(
            "scope IN ('sleeve','basket','global')", name="ck_mandate_cap_mutex_scope"
        ),
        CheckConstraint(
            "char_length(mutex_key) BETWEEN 1 AND 400", name="ck_mandate_cap_mutex_key"
        ),
        {"schema": "review"},
    )

    mutex_key: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
