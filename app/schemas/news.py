"""Pydantic schemas for news analysis requests and responses."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class NewsArticleCreate(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048, description="뉴스 기사 URL")
    title: str = Field(..., min_length=1, max_length=500, description="뉴스 제목")
    source: str | None = Field(None, max_length=100, description="뉴스 출처")
    author: str | None = Field(None, max_length=200, description="기자명")
    content: str | None = Field(None, description="기사 본문 (RSS 뉴스는 없을 수 있음)")
    summary: str | None = Field(None, description="기사 요약")
    stock_symbol: str | None = Field(None, max_length=20, description="관련 종목 코드")
    stock_name: str | None = Field(None, max_length=100, description="관련 종목명")
    published_at: datetime | None = Field(None, description="기사 발행일시")
    market: str = Field("kr", min_length=1, max_length=20, description="시장 구분")
    feed_source: str | None = Field(
        None,
        max_length=50,
        description=(
            "수집 경로 key "
            "(e.g., browser_naver_mainnews, browser_naver_research, mk_stock)"
        ),
    )
    keywords: list[str] | None = Field(None, description="키워드 배열")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        return v.strip()

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        return v.strip()

    @field_validator("source", "author", "feed_source")
    @classmethod
    def normalize_optional_text(cls, v: str | None) -> str | None:
        """Trim optional text fields; whitespace-only becomes None."""
        if v is None:
            return None
        value = v.strip()
        return value or None

    @field_validator("market")
    @classmethod
    def normalize_market(cls, v: str) -> str:
        return v.strip()

    @field_validator("keywords")
    @classmethod
    def normalize_keywords(cls, v: list[str] | None) -> list[str] | None:
        """Clean keyword array: trim, drop empty/whitespace entries."""
        if not v:
            return None
        cleaned = [item.strip() for item in v if item and item.strip()]
        return cleaned or None


class NewsArticleBulkCreate(BaseModel):
    articles: list[NewsArticleCreate] = Field(..., max_length=500)


class NewsIngestionRunCreate(BaseModel):
    run_uuid: str = Field(..., min_length=1, max_length=64)
    market: str = Field(..., min_length=1, max_length=20)
    feed_set: str = Field(..., min_length=1, max_length=100)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str = Field("success", pattern="^(success|partial|failed|dry_run_ok)$")
    source_counts: dict[str, int] = Field(default_factory=dict)
    error_message: str | None = None

    @field_validator("run_uuid", "market", "feed_set", "status")
    @classmethod
    def normalize_required_text(cls, v: str) -> str:
        return v.strip()


class NewsIngestArticle(NewsArticleCreate):
    """news-ingestor article payload mapped into auto_trader article fields."""

    model_config = ConfigDict(populate_by_name=True)

    fingerprint: str = Field(..., min_length=1, max_length=128)
    market: str = Field(..., min_length=1, max_length=20)
    feed_source: str = Field(
        ...,
        validation_alias="source",
        max_length=50,
        description="news-ingestor source/feed key",
    )
    source: str | None = Field(
        None,
        validation_alias="publisher",
        max_length=100,
        description="Publisher mapped to auto_trader news source",
    )
    canonical_url: str | None = Field(None, max_length=2048)
    published_at: datetime | None = Field(None, description="기사 발행일시")
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("canonical_url")
    @classmethod
    def normalize_canonical_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        return value or None

    @model_validator(mode="after")
    def map_ingestor_metadata(self) -> "NewsIngestArticle":
        if self.canonical_url:
            self.url = self.canonical_url
        metadata_keywords = [f"fingerprint:{self.fingerprint.strip()}"]
        if self.canonical_url:
            metadata_keywords.append(f"canonical_url:{self.canonical_url}")
        if self.keywords:
            metadata_keywords.extend(self.keywords)
        self.keywords = metadata_keywords
        return self


class NewsBulkIngestRequest(BaseModel):
    ingestion_run: NewsIngestionRunCreate
    articles: list[NewsIngestArticle] = Field(..., max_length=500)


class NewsBulkIngestResponse(BaseModel):
    success: bool
    run_uuid: str
    inserted_count: int
    skipped_count: int
    skipped_urls: list[str]


class NewsSourceCoverage(BaseModel):
    feed_source: str
    expected_count: int = 0
    stored_total: int = 0
    recent_24h: int = 0
    recent_6h: int = 0
    latest_published_at: datetime | None = None
    latest_scraped_at: datetime | None = None
    published_at_count: int = 0
    status: str = "unavailable"
    warnings: list[str] = []


class NewsReadinessResponse(BaseModel):
    market: str
    is_ready: bool
    is_stale: bool
    latest_run_uuid: str | None
    latest_status: str | None
    latest_finished_at: datetime | None
    latest_article_published_at: datetime | None
    source_counts: dict[str, int]
    source_coverage: list[NewsSourceCoverage] = []
    warnings: list[str]
    max_age_minutes: int


class BulkCreateResponse(BaseModel):
    success: bool
    inserted_count: int
    skipped_count: int
    skipped_urls: list[str]


class NewsArticleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    title: str
    source: str | None
    author: str | None
    content: str | None = Field(validation_alias="article_content")
    summary: str | None
    stock_symbol: str | None
    stock_name: str | None
    published_at: datetime | None = Field(validation_alias="article_published_at")
    scraped_at: datetime
    user_id: int | None
    created_at: datetime
    updated_at: datetime | None
    feed_source: str | None = None
    market: str = "kr"
    keywords: list[str] | None = None
    is_analyzed: bool = False


class NewsAnalysisResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    article_id: int
    model_name: str
    sentiment: str
    sentiment_score: float | None
    summary: str
    key_points: list[str]
    topics: list[str] | None
    price_impact: str | None
    price_impact_score: float | None
    confidence: int
    analysis_quality: str | None
    processing_time_ms: int | None
    created_at: datetime
    updated_at: datetime | None


class NewsAnalysisRequest(BaseModel):
    url: str = Field(..., description="분석할 뉴스 기사 URL")
    content: str = Field(..., min_length=1, description="기사 본문 (스크래핑된 텍스트)")
    title: str = Field(..., description="뉴스 제목")
    source: str | None = Field(None, description="뉴스 출처")
    stock_symbol: str | None = Field(None, description="관련 종목 코드 (선택사항)")
    stock_name: str | None = Field(None, description="관련 종목명 (선택사항)")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        return v.strip()

    @field_validator("content")
    @classmethod
    def validate_content(cls, v: str) -> str:
        return v.strip()


class NewsAnalysisResponse(BaseModel):
    article: NewsArticleResponse
    analysis: NewsAnalysisResultResponse | None = None


class NewsQueryParams(BaseModel):
    market: str | None = Field(None, description="시장 구분으로 필터링 (kr/us/crypto)")
    stock_symbol: str | None = Field(None, description="종목 코드로 필터링")
    sentiment: str | None = Field(
        None, description="감정으로 필터링 (positive/negative/neutral)"
    )
    source: str | None = Field(None, description="뉴스 출처로 필터링")
    hours: int | None = Field(None, ge=1, le=720, description="최근 N시간 이내 기사만")
    feed_source: str | None = Field(
        None,
        description=(
            "collection path key로 필터링 "
            "(e.g., browser_naver_mainnews, browser_naver_research, mk_stock)"
        ),
    )
    keyword: str | None = Field(None, description="키워드로 필터링")
    has_analysis: bool | None = Field(None, description="분석 완료 여부로 필터링")
    limit: int = Field(10, ge=1, le=100, description="반환할 뉴스 수")
    offset: int = Field(0, ge=0, description="건너뛸 뉴스 수")


class NewsListResponse(BaseModel):
    total: int = Field(..., description="전체 뉴스 수")
    items: list[NewsArticleResponse] = Field(..., description="뉴스 리스트")
    page_info: dict[str, Any] = Field(..., description="페이지 정보")
