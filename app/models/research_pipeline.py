"""ROB-112 — Research pipeline ORM models.

5 tables: research_sessions, stage_analysis, research_summaries,
summary_stage_links, user_research_notes. All append-only except
research_sessions (status transitions allowed).
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.models.base import Base


class ResearchSession(Base):
    __tablename__ = "research_sessions"

    id = Column(Integer, primary_key=True, index=True)
    stock_info_id = Column(
        Integer, ForeignKey("stock_info.id"), nullable=False, index=True
    )
    research_run_id = Column(
        Integer,
        ForeignKey("research_runs.id"),
        nullable=True,
        index=True,
        comment="optional link to upstream ResearchRun candidate",
    )
    status = Column(
        String(16),
        nullable=False,
        default="open",
        comment="open|finalized|failed|cancelled",
    )
    started_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finalized_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','finalized','failed','cancelled')",
            name="ck_research_sessions_status",
        ),
    )

    stock_info = relationship("StockInfo")
    stage_analyses = relationship(
        "StageAnalysis", back_populates="session", cascade="all, delete-orphan"
    )
    summaries = relationship(
        "ResearchSummary", back_populates="session", cascade="all, delete-orphan"
    )
    notes = relationship(
        "UserResearchNote", back_populates="session", cascade="all, delete-orphan"
    )


class StageAnalysis(Base):
    __tablename__ = "stage_analysis"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(
        Integer, ForeignKey("research_sessions.id"), nullable=False, index=True
    )
    stage_type = Column(String(32), nullable=False)
    verdict = Column(String(16), nullable=False)
    confidence = Column(Integer, nullable=False, comment="0-100")
    signals = Column(
        JSONB, nullable=False, comment="validated by stage Pydantic schema"
    )
    raw_payload = Column(
        JSONB, nullable=True, comment="provider/LLM raw output for debugging"
    )
    source_freshness = Column(
        JSONB,
        nullable=True,
        comment="{newest_age_minutes,oldest_age_minutes,missing_sources,stale_flags,source_count}",
    )
    model_name = Column(String(100), nullable=True)
    prompt_version = Column(String(64), nullable=True)
    snapshot_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="latest data timestamp the stage observed",
    )
    executed_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="wall-clock analyzer execution time",
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "stage_type IN ('market','news','fundamentals','social')",
            name="ck_stage_analysis_stage_type",
        ),
        CheckConstraint(
            "verdict IN ('bull','bear','neutral','unavailable')",
            name="ck_stage_analysis_verdict",
        ),
        CheckConstraint(
            "confidence BETWEEN 0 AND 100", name="ck_stage_analysis_confidence_range"
        ),
        Index(
            "ix_stage_analysis_session_stage_executed",
            "session_id",
            "stage_type",
            "executed_at",
        ),
    )

    session = relationship("ResearchSession", back_populates="stage_analyses")


class ResearchSummary(Base):
    __tablename__ = "research_summaries"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(
        Integer, ForeignKey("research_sessions.id"), nullable=False, index=True
    )
    decision = Column(String(8), nullable=False, comment="buy|hold|sell")
    confidence = Column(Integer, nullable=False, comment="0-100")
    bull_arguments = Column(JSONB, nullable=False, default=list)
    bear_arguments = Column(JSONB, nullable=False, default=list)
    price_analysis = Column(
        JSONB,
        nullable=True,
        comment="{appropriate_buy_min/max,appropriate_sell_min/max,buy_hope_min/max,sell_target_min/max}",
    )
    reasons = Column(JSONB, nullable=True)
    detailed_text = Column(Text, nullable=True)
    warnings = Column(
        JSONB, nullable=True, comment="missing/unavailable/stale stage warnings"
    )
    model_name = Column(String(100), nullable=True)
    prompt_version = Column(String(64), nullable=True)
    raw_payload = Column(JSONB, nullable=True)
    token_input = Column(Integer, nullable=True)
    token_output = Column(Integer, nullable=True)
    executed_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "decision IN ('buy','hold','sell')", name="ck_research_summaries_decision"
        ),
        CheckConstraint(
            "confidence BETWEEN 0 AND 100",
            name="ck_research_summaries_confidence_range",
        ),
    )

    session = relationship("ResearchSession", back_populates="summaries")
    stage_links = relationship(
        "SummaryStageLink", back_populates="summary", cascade="all, delete-orphan"
    )


class SummaryStageLink(Base):
    __tablename__ = "summary_stage_links"

    id = Column(Integer, primary_key=True, index=True)
    summary_id = Column(
        Integer, ForeignKey("research_summaries.id"), nullable=False, index=True
    )
    stage_analysis_id = Column(
        Integer, ForeignKey("stage_analysis.id"), nullable=False, index=True
    )
    weight = Column(Float, nullable=False, default=1.0)
    direction = Column(String(8), nullable=False)
    rationale = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "weight >= 0 AND weight <= 1", name="ck_summary_stage_links_weight_range"
        ),
        CheckConstraint(
            "direction IN ('support','contradict','context')",
            name="ck_summary_stage_links_direction",
        ),
    )

    summary = relationship("ResearchSummary", back_populates="stage_links")


class UserResearchNote(Base):
    __tablename__ = "user_research_notes"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(
        Integer, ForeignKey("research_sessions.id"), nullable=False, index=True
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    body = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    session = relationship("ResearchSession", back_populates="notes")
