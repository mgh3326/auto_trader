"""Investment report-scoped persistence (ROB-265).

Five entities under the ``review`` schema replace the legacy
``analysis_report*`` / ``watch_order_intent_ledger`` family.

* ``InvestmentReport`` — report header (one per published/draft report bundle).
* ``InvestmentReportItem`` — action/watch/risk items owned by a report.
* ``InvestmentReportItemDecision`` — operator decisions on items (audit).
* ``InvestmentWatchAlert`` — immutable activation snapshot of approved watch items.
* ``InvestmentWatchEvent`` — trigger events the scanner writes when an alert fires.

The shape is intentionally NOT backward-compatible with the legacy tables.
All writes must go through ``app.services.investment_reports.*`` (added in a
later plan). Direct ``INSERT/UPDATE/DELETE`` is forbidden once those services
land.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text

from app.models.base import Base
