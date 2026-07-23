import json
import os
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

ApiRateLimitEntry = dict[str, int | float]
ApiRateLimitMap = dict[str, ApiRateLimitEntry]


DEFAULT_KIS_API_RATE_LIMITS: ApiRateLimitMap = {
    "FHKST03010100|/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice": {
        "rate": 20,
        "period": 1.0,
    },
    "FHPST04830000|/uapi/domestic-stock/v1/quotations/daily-short-sale": {
        "rate": 20,
        "period": 1.0,
    },
    "FHKST03010230|/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice": {
        "rate": 20,
        "period": 1.0,
    },
    # ROB-485: get_execution_strength (주식현재가 체결, tick rows)
    "FHKST01010300|/uapi/domestic-stock/v1/quotations/inquire-ccnl": {
        "rate": 20,
        "period": 1.0,
    },
    # ROB-753: batch /invest KIS fallback uses these current-price endpoints.
    # Live measurement showed same-endpoint concurrent bursts can fail most US
    # symbols, while sequential calls succeed. Keep the default process-local
    # limiter conservative; operators can override with KIS_API_RATE_LIMITS.
    "FHKST01010100|/uapi/domestic-stock/v1/quotations/inquire-price": {
        "rate": 1,
        "period": 0.2,
    },
    "HHDFS00000300|/uapi/overseas-price/v1/quotations/price": {
        "rate": 1,
        "period": 0.2,
    },
    # ROB-951: mock-US buy preflight and the read-only probe use VTTS3007R.
    "VTTS3007R|/uapi/overseas-stock/v1/trading/inquire-psamount": {
        "rate": 10,
        "period": 1.0,
    },
    "TTTC8434R|/uapi/domestic-stock/v1/trading/inquire-balance": {
        "rate": 10,
        "period": 1.0,
    },
    "TTTC8001R|/uapi/domestic-stock/v1/trading/inquire-daily-ccld": {
        "rate": 10,
        "period": 1.0,
    },
    "TTTC8036R|/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl": {
        "rate": 10,
        "period": 1.0,
    },
    # ROB-585 (absorbed by ROB-645): order TRs throttled to 8/s so batch orders
    # stay under the KIS ledger limit (EGW00215 '초당 거래건수 초과'). ROB-645
    # removes all order re-POST retries, so this pre-send wait is the only guard
    # against the rate limit — orders that still exceed it fail fast, never re-sent.
    "TTTC0012U|/uapi/domestic-stock/v1/trading/order-cash": {"rate": 8, "period": 1.0},
    "VTTC0012U|/uapi/domestic-stock/v1/trading/order-cash": {"rate": 8, "period": 1.0},
    "TTTC0011U|/uapi/domestic-stock/v1/trading/order-cash": {"rate": 8, "period": 1.0},
    "VTTC0011U|/uapi/domestic-stock/v1/trading/order-cash": {"rate": 8, "period": 1.0},
    "TTTC0013U|/uapi/domestic-stock/v1/trading/order-rvsecncl": {
        "rate": 8,
        "period": 1.0,
    },
    "VTTC0013U|/uapi/domestic-stock/v1/trading/order-rvsecncl": {
        "rate": 8,
        "period": 1.0,
    },
    "TTTT1002U|/uapi/overseas-stock/v1/trading/order": {"rate": 8, "period": 1.0},
    "VTTT1002U|/uapi/overseas-stock/v1/trading/order": {"rate": 8, "period": 1.0},
    "TTTT1006U|/uapi/overseas-stock/v1/trading/order": {"rate": 8, "period": 1.0},
    "VTTT1006U|/uapi/overseas-stock/v1/trading/order": {"rate": 8, "period": 1.0},
    "VTTT1001U|/uapi/overseas-stock/v1/trading/order": {"rate": 8, "period": 1.0},
    "TTTT1004U|/uapi/overseas-stock/v1/trading/order-rvsecncl": {
        "rate": 8,
        "period": 1.0,
    },
    "VTTT1004U|/uapi/overseas-stock/v1/trading/order-rvsecncl": {
        "rate": 8,
        "period": 1.0,
    },
}

DEFAULT_UPBIT_API_RATE_LIMITS: ApiRateLimitMap = {
    "GET /v1/accounts": {"rate": 30, "period": 1.0},
    "GET /v1/order": {"rate": 30, "period": 1.0},
    "GET /v1/orders/closed": {"rate": 30, "period": 1.0},
    "GET /v1/ticker": {"rate": 10, "period": 1.0},
}

_DEFAULT_API_RATE_LIMITS_BY_FIELD: dict[str, ApiRateLimitMap] = {
    "kis_api_rate_limits": DEFAULT_KIS_API_RATE_LIMITS,
    "upbit_api_rate_limits": DEFAULT_UPBIT_API_RATE_LIMITS,
}


def _copy_api_rate_limit_map(api_rate_limits: ApiRateLimitMap) -> ApiRateLimitMap:
    return {
        endpoint_key: dict(limit_config)
        for endpoint_key, limit_config in api_rate_limits.items()
    }


def _default_kis_api_rate_limits() -> ApiRateLimitMap:
    return _copy_api_rate_limit_map(DEFAULT_KIS_API_RATE_LIMITS)


def _default_upbit_api_rate_limits() -> ApiRateLimitMap:
    return _copy_api_rate_limit_map(DEFAULT_UPBIT_API_RATE_LIMITS)


def _parse_api_rate_limit_overrides(value: Any) -> ApiRateLimitMap:
    if value is None:
        return {}
    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return {}
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON for API rate limits: {exc}") from exc
        value = parsed
    if not isinstance(value, dict):
        raise ValueError("API rate limits must be a JSON object")
    if not value:
        return {}

    overrides: ApiRateLimitMap = {}
    for endpoint_key, limit_config in value.items():
        if not isinstance(limit_config, dict):
            raise ValueError(
                f"API rate limit override for '{endpoint_key}' must be a JSON object"
            )
        overrides[str(endpoint_key)] = dict(limit_config)
    return overrides


def _merge_api_rate_limits(
    defaults: ApiRateLimitMap, overrides: Any
) -> ApiRateLimitMap:
    merged = _copy_api_rate_limit_map(defaults)
    parsed_overrides = _parse_api_rate_limit_overrides(overrides)
    for endpoint_key, limit_config in parsed_overrides.items():
        merged_entry = dict(merged.get(endpoint_key, {}))
        merged_entry.update(limit_config)
        merged[endpoint_key] = merged_entry
    return merged


def _merge_api_rate_limits_for_source(defaults: ApiRateLimitMap, value: Any) -> Any:
    try:
        return _merge_api_rate_limits(defaults, value)
    except ValueError:
        return value


class _MergedApiRateLimitSource(PydanticBaseSettingsSource):
    def __init__(self, wrapped_source: PydanticBaseSettingsSource) -> None:
        super().__init__(wrapped_source.settings_cls)
        self._wrapped_source = wrapped_source

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        return self._wrapped_source.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        source_data = dict(self._wrapped_source())
        for field_name, defaults in _DEFAULT_API_RATE_LIMITS_BY_FIELD.items():
            if field_name in self.current_state:
                source_data.pop(field_name, None)
                continue
            if field_name in source_data:
                source_data[field_name] = _merge_api_rate_limits_for_source(
                    defaults, source_data[field_name]
                )
        return source_data


def _load_settings() -> "Settings":
    settings_class = globals()["Settings"]
    loaded_settings = settings_class()
    if not isinstance(loaded_settings, Settings):
        raise TypeError("Settings bootstrap returned an unexpected object")
    return loaded_settings


class Settings(BaseSettings):
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        _ = settings_cls
        return (
            init_settings,
            _MergedApiRateLimitSource(env_settings),
            _MergedApiRateLimitSource(dotenv_settings),
            file_secret_settings,
        )

    # KIS
    kis_app_key: str
    kis_app_secret: str
    kis_base_url: str = "https://openapi.koreainvestment.com:9443"
    kis_access_token: str | None = None  # 최초엔 비워두고 자동 발급
    kis_account_no: str | None = None  # 계좌번호 (예: "12345678-01")

    # KIS official mock/sandbox account. Disabled by default and must be
    # explicitly configured by the runtime environment.
    kis_mock_enabled: bool = False
    kis_mock_app_key: str | None = None
    kis_mock_app_secret: str | None = None
    kis_mock_base_url: str = "https://openapivts.koreainvestment.com:29443"
    kis_mock_account_no: str | None = None
    kis_mock_access_token: str | None = None
    kis_mock_scalping_enabled: bool = False

    # ROB-671: gate the aggressive "unsettled regular-session buy → 15:30 death"
    # expiry downgrade. Default off — a regular-session BUY keeps expected_expiry
    # at 20:00 KST (conservative). Flip to true ONLY after a live measurement
    # confirms the 15:30 death is session expiry (not a D+2 unsettled-cash cancel).
    kis_regular_buy_unsettled_expiry_1530: bool = False

    # ROB-471: US get_quote 가격 소스 선택. True → KIS 해외 현재가(HHDFS00000300)
    # primary + Yahoo fast_info fallback. False → Yahoo primary(레거시).
    # 라이브 파싱 이상 시 operator가 US_QUOTE_KIS_PRIMARY=false 로 즉시 롤백.
    us_quote_kis_primary: bool = True

    # Kiwoom Securities mock account. Disabled by default; mock-only foundation
    # added in ROB-97. Live URL is recorded so the runtime can defensively
    # reject it — no code path may target the live host in this PR.
    kiwoom_mock_enabled: bool = False
    kiwoom_mock_app_key: str | None = None
    kiwoom_mock_app_secret: str | None = None
    kiwoom_mock_account_no: str | None = None
    kiwoom_mock_base_url: str = "https://mockapi.kiwoom.com"
    kiwoom_base_url: str = "https://api.kiwoom.com"  # live disabled in this PR
    kiwoom_mock_access_token: str | None = None

    # ROB-867: US-only Kiwoom mock namespace. Same mock host, separate
    # app_key/app_secret/account_no; never reads or falls back to KR settings.
    kiwoom_mock_us_enabled: bool = False
    kiwoom_mock_us_app_key: str | None = None
    kiwoom_mock_us_app_secret: str | None = None
    kiwoom_mock_us_account_no: str | None = None

    # ROB-908: surface Alpaca paper read/preview/confirm-gated order/ledger tools
    # in the DEFAULT profile (mock_alpaca operator session runs on DEFAULT, not a
    # separate us-paper instance). Flag-gated off by default, mirroring the
    # ROB-601/ROB-867 kiwoom-mock DEFAULT gate; the automated-submit tool stays
    # US_PAPER-only regardless (ROB-842 governance).
    alpaca_paper_default_tools_enabled: bool = False

    # Toss Securities Open API. Live-only, disabled by default. ROB-530 adds
    # read-only client support; order mutations are handled by follow-up issues.
    toss_api_enabled: bool = False
    toss_api_client_id: str | None = None
    toss_api_client_secret: SecretStr | None = None
    toss_api_account_seq: int | None = None
    toss_api_base_url: str | None = None
    toss_live_order_mutations_enabled: bool = False

    # ROB-866: gate for the scheduleless Toss manual-activity sweep TaskIQ task.
    # Default off — the sweep runs manually (dry_run MCP tool) first; recurrence is
    # a separate decision after manual reps. Only gates the auto-run task; the MCP
    # tool itself is operator-driven and gated by TOSS_API_ENABLED for reads.
    toss_manual_activity_sweep_enabled: bool = False

    # ROB-701/ROB-828: Redis cache-aside for the per-symbol Toss sellable-
    # quantity fanout on /invest home, account-panel, and MCP holdings.
    # Successful sell mutations and confirmed fills invalidate the symbol.
    # enabled=False => cache always misses => fanout-every-load.
    toss_sellable_cache_enabled: bool = True
    toss_sellable_cache_ttl_seconds: float = 600.0

    # ROB-710: per-market layer-order flip for /invest batch current-price reads.
    # False (default) => today's KIS → Toss → snapshot order, byte-identical.
    # True => TOSS batch → KIS per-symbol → snapshot (reserve KIS app-key TPS for
    # OHLCV/US-intraday/live-orders; Toss MARKET_DATA batch stayed up through the
    # 2026-07-04 KIS maintenance). Data gate CLEARED 2026-07-06: ROB-709 A/B go bars
    # passed BOTH markets (KR 0-tick exact; US median 0 bps / max ~1.45 bps) and
    # ROB-708 (US live-last endpoint) is merged. Remaining discipline is canary
    # sequencing: flip KR first, observe, then US — both shipped False. Instantly
    # revertible: set back to False and the next /invest load is KIS-first again.
    invest_quotes_toss_first_kr: bool = False
    invest_quotes_toss_first_us: bool = False

    # ROB-576 — Toss fill notifications are inert until explicitly enabled by
    # the operator. Toss auto-reconcile gates live with the task flags below.
    toss_fill_notify_enabled: bool = False
    toss_approval_hash_mode: str = "optional"  # off | optional | warn | required

    # ROB-653 P6-B — kis_live/crypto place-order approval-hash enforcement level.
    # off | optional | warn | required. optional = no behavior change.
    order_approval_hash_mode: str = "optional"

    # ROB-668 — session-aware NXT order preflight rollout level.
    # off | optional | warn | required. Default warn-first: preview always warns,
    # place blocks a live send only when set to 'required'.
    toss_nxt_preflight_mode: str = "warn"

    # KIS WebSocket
    kis_ws_is_mock: bool = False  # Mock 모드 (테스트용)
    kis_ws_hts_id: str = ""  # HTS ID (WebSocket 인증용)
    kis_ws_reconnect_delay_seconds: int = 5  # 재연결 대기 시간 (초)
    kis_ws_max_reconnect_attempts: int = 10  # 최대 재연결 시도 횟수
    kis_ws_ping_interval: int = 30  # Ping 전송 간격 (초)
    kis_ws_ping_timeout: int = 10  # Ping 응답 대기 시간 (초)
    # ROB-321: read-only quote WS daemon/smoke gate (default off).
    kis_mock_scalping_ws_enabled: bool = False
    # ROB-321 PR4b: per-run order-mutation gate for the scalping daemon. Without
    # it the daemon dry-runs (preview only, no mock order, no ledger write).
    kis_mock_scalping_ws_confirm: bool = False
    # Phase 2 — daily demo scalping review + buy&hold benchmark flow (default-off).
    binance_demo_scalping_review_flow_enabled: bool = False
    # Phase 3 — gate for the LLM decision-injection MCP tool (default-off).
    binance_demo_scalping_enabled: bool = False
    # ROB-845 — isolated canonical Binance Demo / Alpaca Paper experiment
    # façade. The dedicated MCP profile must remain physically absent unless
    # the operator opts in; startup also requires MCP bearer authentication.
    PAPER_EXECUTION_ENABLED: bool = False
    # ROB-849 — immutable BTC/ETH cohort scheduler. Disabled means the TaskIQ
    # label is absent; direct task calls audit recoverable claims before returning.
    PAPER_COHORT_ENABLED: bool = False
    PAPER_COHORT_CRON: str = "* * * * *"
    # ROB-848 — authenticated caller id -> validation role. Empty/unmapped is
    # intentionally fail-closed; caller-owned payload roles are never accepted.
    PAPER_VALIDATION_ACTOR_ROLES: dict[str, str] = Field(default_factory=dict)
    # Bound to the authenticated PAPER_EXECUTION bearer token at the process
    # composition boundary. Never derive this principal from a caller header.
    PAPER_VALIDATION_AUTHENTICATED_ACTOR_ID: str = ""
    # ROB-1038 — service/user principal bound to the MCP bearer-authenticated
    # forecast evidence write boundary. Empty is fail-closed for corporate-action
    # promotion, legacy attestation, and terminal supersession. Never derive this
    # identity from forecast JSON or a caller-supplied identity header.
    FORECAST_EVIDENCE_AUTHENTICATED_ACTOR_ID: str = ""

    # KIS Rate Limiting (HTTP API)
    kis_rate_limit_rate: int = 19  # 초당 최대 요청 수 (안전 마진으로 20-1)
    kis_rate_limit_period: float = 1.0  # 윈도우 기간 (초)

    # KIS Per-API Rate Limits (JSON map: "TR_ID|/path" -> {"rate": int, "period": float})
    kis_api_rate_limits: Annotated[ApiRateLimitMap, NoDecode] = Field(
        default_factory=_default_kis_api_rate_limits
    )

    # Upbit Rate Limiting (HTTP API)
    upbit_rate_limit_rate: int = 10  # 초당 최대 요청 수
    upbit_rate_limit_period: float = 1.0  # 윈도우 기간 (초)

    # Upbit Per-API Rate Limits (JSON map: "METHOD /path" -> {"rate": int, "period": float})
    upbit_api_rate_limits: Annotated[ApiRateLimitMap, NoDecode] = Field(
        default_factory=_default_upbit_api_rate_limits
    )

    upbit_ohlcv_cache_enabled: bool = True
    upbit_public_read_model_cache_enabled: bool = True
    upbit_ohlcv_cache_max_days: int = 400
    upbit_ohlcv_cache_lock_ttl_seconds: int = 10
    yahoo_ohlcv_cache_enabled: bool = True
    yahoo_ohlcv_cache_max_days: int = 400
    yahoo_ohlcv_cache_lock_ttl_seconds: int = 10
    kis_ohlcv_cache_enabled: bool = True
    kis_ohlcv_cache_max_days: int = 400
    kis_ohlcv_cache_max_hours: int = 400 * 24
    kis_ohlcv_cache_lock_ttl_seconds: int = 10

    # ROB-638: fetch-layer Redis cache for the slowly-changing analyze provider
    # fetches (KR naver snapshot, US yfinance bundle, US finnhub profile).
    # Default on in production; forced off in tests (tests/conftest.py) so no
    # test can touch a real Redis unless it explicitly patches the cache client.
    analyze_fetch_cache_enabled: bool = True

    # ROB-688: bound the get_sector_peers KR peer fanout so the concurrent burst
    # to m.stock.naver.com stops tripping Naver server-side throttling.
    naver_peer_fetch_concurrency: int = 5
    naver_peer_fetch_timeout_seconds: float = 5.0

    # ROB-688: short-TTL fail-open Redis cache for the get_sector_peers KR
    # /basic+/integration bundle and the sector page. Intraday staleness of
    # current_price/change_pct up to the TTL is acceptable for a comparison tool.
    naver_peer_cache_enabled: bool = True
    naver_peer_cache_ttl_seconds: int = 600

    # API Rate Limit Retry Settings (429 handling)
    api_rate_limit_retry_429_max: int = 2  # 429 에러 시 최대 재시도 횟수
    api_rate_limit_retry_429_base_delay: float = 0.2  # 지수 백오프 기본 대기 시간 (초)

    # ROB-699: per-process in-process circuit breaker for KIS transport connect
    # failures (e.g. KIS maintenance). Closed = pure passthrough; open = fail-fast
    # so /invest KIS→Toss fallbacks fire in ~0ms instead of burning the connect
    # timeout on every call. Default ON; False = complete no-op.
    kis_circuit_breaker_enabled: bool = True
    kis_circuit_breaker_failure_threshold: int = (
        5  # consecutive connect-failures -> open
    )
    kis_circuit_breaker_cooldown_seconds: int = (
        45  # open -> half-open cooldown (monotonic s)
    )
    # Telegram
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_chat_ids_str: str | None = None

    # Discord Webhooks
    discord_webhook_us: str | None = None
    discord_webhook_kr: str | None = None
    discord_webhook_crypto: str | None = None
    discord_webhook_alerts: str | None = None

    # ROB-99 — crypto pending-order reminders
    crypto_pending_order_alert_enabled: bool = False
    crypto_pending_order_alert_channel_id: str = "1500719153508515870"
    crypto_pending_order_failure_channel_id: str = "1500722535678083102"
    crypto_pending_order_alert_webhook_url: str | None = None
    crypto_pending_order_failure_webhook_url: str | None = None
    crypto_pending_order_discord_bot_token: SecretStr | None = None

    # Strategy
    top_n: int = 30
    drop_pct: float = -3.0  # '-3'은 -3 %
    # Scheduler
    cron: str = "0 * * * *"  # 매시 정각

    # ROB-26 — research-run refresh schedules
    research_run_refresh_enabled: bool = False
    research_run_refresh_user_id: int | None = None
    research_run_refresh_market_hours_only: bool = True

    # ROB-208 — market events rolling scheduler + activation gate
    market_events_ingest_commit_enabled: bool = False
    market_events_rolling_window_days_back: int = 7
    market_events_rolling_window_days_forward: int = 60
    market_events_rolling_window_max_partitions_per_run: int = 90

    @property
    def telegram_chat_ids(self) -> list[str]:
        """Return configured Telegram chat IDs.

        `TELEGRAM_CHAT_IDS_STR` supports comma-separated multi-chat delivery.
        `TELEGRAM_CHAT_ID` remains supported as the legacy single-chat form.
        """
        if self.telegram_chat_ids_str:
            return [
                chat_id.strip()
                for chat_id in self.telegram_chat_ids_str.split(",")
                if chat_id.strip()
            ]
        if not self.telegram_chat_id:
            return []
        return [self.telegram_chat_id.strip()]

    @field_validator("research_run_refresh_user_id", mode="before")
    @classmethod
    def _parse_optional_user_id(cls, v: Any) -> int | None:
        if v == "" or v is None:
            return None
        return int(v)

    @field_validator("kis_api_rate_limits", "upbit_api_rate_limits", mode="before")
    @classmethod
    def parse_api_rate_limits(cls, v: Any) -> ApiRateLimitMap:
        return _parse_api_rate_limit_overrides(v)

    @field_validator(
        "toss_approval_hash_mode",
        "order_approval_hash_mode",
        "toss_nxt_preflight_mode",
        mode="before",
    )
    @classmethod
    def _validate_approval_hash_mode(cls, v: Any) -> str:
        # ROB-659: fail-loud on an out-of-enum approval-hash mode. Without this,
        # a typo (e.g. "requird") passes ``mode != "off"`` at the read sites but
        # matches neither the "required" nor "warn" branch, silently degrading to
        # optional-level behavior — a safety-adjacent footgun for P6 rollout.
        # Case/whitespace are normalized so the exact-string comparisons downstream
        # ("off"/"required"/"warn") can't be defeated by "Required" / " required ".
        allowed = {"off", "optional", "warn", "required"}
        normalized = v.strip().lower() if isinstance(v, str) else v
        if normalized not in allowed:
            raise ValueError(
                f"approval hash mode must be one of {sorted(allowed)}, got {v!r}"
            )
        return normalized

    def get_redis_url(self) -> str:
        """Redis 연결 URL 생성"""
        if self.redis_url:
            # 사용자가 직접 redis_url을 설정한 경우
            return self.redis_url

        # 개별 설정으로부터 URL 생성
        protocol = "rediss://" if self.redis_ssl else "redis://"
        auth_part = ""
        if self.redis_password:
            auth_part = f":{self.redis_password}@"

        url = (
            f"{protocol}{auth_part}{self.redis_host}:{self.redis_port}/{self.redis_db}"
        )
        return url

    opendart_api_key: str
    opendart_daily_request_budget: int = 18000
    DATABASE_URL: str
    upbit_access_key: str
    upbit_secret_key: str

    # Finnhub API (optional - for news and fundamentals)
    finnhub_api_key: str | None = None

    # ROB-434 — US market_valuation Finnhub fallback (field-fill). When ON and
    # FINNHUB_API_KEY is set, default_valuation_fetcher backfills valuation fields
    # yahoo .info left null (operator "ROE rows 0") from company_basic_financials.
    # Default False → inert until an operator enables it. No key → also inert.
    market_valuation_finnhub_fallback_enabled: bool = False

    # WiseFn KR earnings calendar (ROB-171)
    # Default False until the upstream contract is confirmed; CI never calls live.
    wisefn_earnings_enabled: bool = False

    # ROB-204 — Prefect/manual US screener snapshot writes stay dry-run unless explicitly enabled.
    invest_screener_snapshots_commit_enabled: bool = False

    # ROB-449 — get_retail_sentiment live Naver 종목토론 fetch is OFF by default. The source
    # is a ToS-sensitive UGC surface; the tool returns status="disabled" until an operator
    # explicitly enables it after a ToS/endpoint review. Aggregate counts only (never raw text).
    retail_sentiment_live_enabled: bool = False

    # ROB-595: 비공식 토스 컨슈머 API (wts-info-api) 신호 — ToS 리뷰 전까지 비활성
    toss_consumer_signals_enabled: bool = False

    # ROB-281 — Gates cron registration for KR/US screener snapshot scheduled refreshes.
    # When False, scheduled tasks remain defined as broker tasks (so operators can still
    # kick them manually via ``taskiq kick``) but no cron entries are registered. Pairs
    # with ``invest_screener_snapshots_commit_enabled`` (the DB write gate) so the
    # production rollout can move schedule → dry-run-on-cron → commit-on-cron in stages.
    invest_screener_schedule_enabled: bool = False

    # ROB-438 — recurring schedulers for the valuation + investor-flow snapshots
    # (the other inputs the screener depends on). Same double-gate as invest_screener:
    # *_schedule_enabled registers cron (default off → manual kick only); *_commit_enabled
    # allows DB writes (default off → dry-run-on-cron). Operator flips both to activate.
    market_valuation_schedule_enabled: bool = False
    market_valuation_snapshots_commit_enabled: bool = False
    investor_flow_schedule_enabled: bool = False
    investor_flow_snapshots_commit_enabled: bool = False

    # ROB-222 — Naver momentum/theme event snapshot writes stay dry-run unless explicitly enabled.
    invest_momentum_events_commit_enabled: bool = False
    invest_momentum_events_scheduler_enabled: bool = False
    invest_momentum_events_scheduler_cron: str = "*/10 9-15 * * 1-5"
    invest_momentum_events_scheduler_page_size: int = 50
    invest_momentum_events_scheduler_trade_types: str = "KRX,NXT"
    invest_momentum_events_scheduler_order_types: str = "up,quantTop,priceTop,searchTop"

    # KRX (한국거래소) 정보데이터시스템
    krx_member_id: str | None = None
    krx_password: str | None = None

    # Upbit 매수 설정
    upbit_buy_amount: int = 10000  # 분할 매수 금액 (기본 10만원)
    upbit_min_krw_balance: int = upbit_buy_amount + 5000  # 최소 KRW 잔고 (기본 10만원)

    # Redis 설정
    redis_url: str | None = None  # .env에서 설정하거나 None으로 두면 개별 설정 사용
    redis_host: str = "localhost"
    redis_port: int = 6381
    redis_db: int = 0
    redis_password: str | None = None
    redis_ssl: bool = False

    # Redis 연결 풀 설정
    # ROB-469 PR2: widened from 10 — a tight shared ceiling caused pool contention
    # when several MCP tool fan-outs ran at once.
    redis_max_connections: int = 20
    redis_socket_timeout: int = 5
    redis_socket_connect_timeout: int = 5

    # Sentry
    SENTRY_DSN: str = ""
    SENTRY_ENVIRONMENT: str | None = None
    SENTRY_TRACES_SAMPLE_RATE: float = 1.0
    SENTRY_PROFILES_SAMPLE_RATE: float = 1.0
    SENTRY_SEND_DEFAULT_PII: bool = True
    SENTRY_ENABLE_LOG_EVENTS: bool = True
    SENTRY_MCP_INCLUDE_PROMPTS: bool = True
    SENTRY_DEBUG: bool = False

    # Monitoring test route exposure
    EXPOSE_MONITORING_TEST_ROUTES: bool = False

    # External AI agent gateway integration (formerly OpenClaw)
    AGENT_GATEWAY_URL: str = "http://localhost:18789/hooks/agent"
    AGENT_GATEWAY_TOKEN: str = ""
    AGENT_GATEWAY_CALLBACK_TOKEN: str = ""  # shared secret for inbound callback auth
    AGENT_GATEWAY_CALLBACK_URL: str = "http://localhost:8000/api/v1/agent/callback"
    AGENT_GATEWAY_SCREENER_CALLBACK_URL: str = (
        "http://localhost:8000/api/screener/callback"
    )
    AGENT_GATEWAY_ENABLED: bool = False

    # Hermes review-trigger notification (ROB-265 Plan 4). Replaces the
    # agent-gateway watch-alert delivery for ``investment_watch_events``.
    # When ``HERMES_ENABLED`` is False the client skips the HTTP call and
    # returns ``status='skipped'`` — useful for tests and disabled-env runs.
    HERMES_WEBHOOK_URL: str = "http://localhost:18790/hooks/review-trigger"
    HERMES_TOKEN: str = ""
    HERMES_ENABLED: bool = False

    # ROB-566: watch 트리거 알림 전송 수단. "hermes_webhook"(default, 현행 Prefect
    # 수신기로 HTTP POST) | "python_direct"(in-process TradeNotifier 렌더, ROB-558 체결과 동형).
    WATCH_NOTIFY_TRANSPORT: Literal["hermes_webhook", "python_direct"] = (
        "hermes_webhook"
    )

    # ROB-337 Slice 2 — watch validity review job. Default off; the task and
    # CLI are scheduleless / dry-run-default even when this is set.
    WATCH_VALIDITY_REVIEW_ENABLED: bool = False

    # MCP caller identity fallback for non-HTTP/manual runs
    mcp_caller_agent_id_fallback: str | None = Field(
        default=None,
        validation_alias="MCP_CALLER_AGENT_ID",
    )

    DAILY_SCAN_ENABLED: bool = False
    DAILY_SCAN_CRASH_THRESHOLD: float = 0.05
    DAILY_SCAN_CRASH_HOLDING_THRESHOLD: float = 0.04
    DAILY_SCAN_CRASH_TOP10_THRESHOLD: float = 0.06
    DAILY_SCAN_CRASH_TOP30_THRESHOLD: float = 0.08
    DAILY_SCAN_CRASH_TOP50_THRESHOLD: float = 0.10
    DAILY_SCAN_CRASH_TOP100_THRESHOLD: float = 0.2
    DAILY_SCAN_CRASH_TOP_RANK_LIMIT: int = 50
    DAILY_SCAN_CRASH_NEAR_MISS_RATIO: float = 0.8
    DAILY_SCAN_RSI_OVERBOUGHT: float = 70.0
    DAILY_SCAN_RSI_OVERSOLD: float = 35.0
    DAILY_SCAN_FNG_LOW: int = 10
    DAILY_SCAN_FNG_HIGH: int = 80
    DAILY_SCAN_TOP_COINS_COUNT: int = 30

    # JWT Authentication settings
    SECRET_KEY: str
    ALGORITHM: Literal["HS256", "HS384", "HS512"] = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    SESSION_BLACKLIST_FAIL_SAFE: bool = True
    SESSION_BLACKLIST_DB_FALLBACK: bool = True

    PUBLIC_API_PATHS: Annotated[list[str], NoDecode] = []

    # news-ingestor machine-to-machine bulk ingest authentication
    NEWS_INGESTOR_INGEST_TOKEN: str = ""
    NEWS_INGESTOR_INGEST_TOKEN_HEADER: str = "X-News-Ingestor-Token"

    # research-reports machine-to-machine bulk ingest authentication
    RESEARCH_REPORTS_INGEST_TOKEN: str = ""
    RESEARCH_REPORTS_INGEST_TOKEN_HEADER: str = "X-Research-Reports-Ingest-Token"
    RESEARCH_REPORTS_FRESHNESS_MAX_AGE_HOURS: int = 24
    RESEARCH_REPORTS_INGEST_COMMIT_ENABLED: bool = False

    # ROB-287 — Hermes-initiated HTTP ingest authentication. Mirror of the
    # research-reports / news-ingestor token pattern: a single shared secret
    # gates the entire ``/trading/api/investment-reports/hermes/*`` family.
    # Required by the AuthMiddleware token branch; if unset, all four HTTP
    # endpoints respond ``403 "token not configured"`` regardless of body.
    HERMES_INGEST_TOKEN: str = ""
    HERMES_INGEST_TOKEN_HEADER: str = "X-Hermes-Ingest-Token"

    # ROB-491 — external news-relevance judgment job surface. Same prefix-token
    # shape as the Hermes branch; gates the whole
    # ``/trading/api/news-relevance/*`` family (pending read + judgment
    # ingest). Unset → all endpoints respond ``403 "token not configured"``.
    NEWS_RELEVANCE_INGEST_TOKEN: str = ""
    NEWS_RELEVANCE_INGEST_TOKEN_HEADER: str = "X-News-Relevance-Ingest-Token"
    # ROB-506 — TaskIQ async judgment worker for symbol_news_relevance
    # pending rows. Default-off: get_news never enqueues and commit-mode
    # task runs return "disabled" until the operator flips the flag. The
    # webhook is the external Hermes-compatible judgment boundary — no
    # in-process LLM provider, no OpenRouter credential in this repo.
    # Distinct namespace from HERMES_* (notification) and
    # NEWS_RELEVANCE_INGEST_* (inbound token) on purpose.
    NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED: bool = False
    NEWS_RELEVANCE_JUDGMENT_WEBHOOK_URL: str = ""
    NEWS_RELEVANCE_JUDGMENT_TOKEN: str = ""
    NEWS_RELEVANCE_JUDGMENT_TIMEOUT_S: float = 120.0
    NEWS_RELEVANCE_JUDGMENT_BATCH_LIMIT: int = 50
    # ROB-916 — default-disabled gate for scripts/backfill_news_related_symbols.py.
    # Read-only DB access + additive news_article_related_symbols inserts only
    # (never touches symbol_news_relevance excluded/pending state); the gate
    # exists so the script can't run against a target DB unattended even in
    # --dry-run.
    NEWS_RELATED_SYMBOLS_BACKFILL_ENABLED: bool = False

    # ROB-510 — Finnhub news fetch reliability (per-attempt timeout + bounded retry)
    FINNHUB_NEWS_TIMEOUT_S: float = 8.0
    FINNHUB_NEWS_MAX_ATTEMPTS: int = 3

    # ROB-211 execution ledger ships inert; commit/backfill activation is a separate approval-gated ops change.
    EXECUTION_LEDGER_COMMIT_ENABLED: bool = False

    # ROB-404 — kis_mock execution-event consumer + periodic reconcile.
    # Default off: the consumer runs reconcile in dry-run preflight and the
    # periodic taskiq task returns paused until an operator flips these.
    KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED: bool = False
    KIS_MOCK_RECONCILE_PERIODIC_ENABLED: bool = False

    # ROB-844 — scheduleless Binance Demo abandoned-reservation reconcile.
    # The canonical ``binance_demo_scalping_enabled`` master above and the
    # reconcile gate must both be enabled. Do not duplicate the case-insensitive
    # BINANCE_DEMO_SCALPING_ENABLED alias here. Broker reads stay dry-run until
    # the independent confirm gate is enabled; candidates younger than one hour
    # are never queried.
    BINANCE_DEMO_RESERVATION_RECONCILE_ENABLED: bool = False
    BINANCE_DEMO_RESERVATION_RECONCILE_CONFIRM: bool = False
    BINANCE_DEMO_RESERVATION_RECONCILE_MIN_AGE_SECONDS: int = 3600

    # ROB-475 / ROB-574 — paused periodic auto-reconcile for KIS live KR orders.
    # Default off; operator flips + adds recurrence outside this repo.
    # ROB-487 adds a second default-off gate: flipping only the legacy flag
    # is no longer enough — a deployment must carry the fail-closed reconcile
    # semantics AND pass the safety review before unattended booking runs.
    KIS_LIVE_AUTO_RECONCILE_ENABLED: bool = False
    KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED: bool = False

    # ROB-574 — paused periodic auto-reconcile for Toss live KR/US orders.
    # Default off and scheduleless in this repo. Recurrence belongs to the
    # operator automation layer; unattended booking requires both gates.
    TOSS_LIVE_AUTO_RECONCILE_ENABLED: bool = False
    TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED: bool = False
    # ROB-757 — Toss REST fill poller. Default off; read-only broker scan plus
    # evidence-gated local booking only after operator activation.
    TOSS_FILL_POLL_ENABLED: bool = False
    TOSS_FILL_POLL_CRON: str = "*/2 * * * *"
    TOSS_FILL_POLL_LOOKBACK_DAYS: int = 7
    TOSS_FILL_POLL_CLOSED_PAGE_CAP: int = 20
    TOSS_FILL_POLL_RECONCILE_LIMIT: int = 100
    TOSS_FILL_POLL_MARKET_GATE_ENABLED: bool = True
    # ROB-402 — watch auto_execute_mock. Default off: the merged PR is inert
    # (no real mock orders) until an operator flips this.
    WATCH_AUTO_EXECUTE_MOCK_ENABLED: bool = False
    # ROB-405 Slice A — mock roundtrip → trade_journal bridge. Default off:
    # no journals are created until an operator flips this.
    MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED: bool = False
    # ROB-405 Slice B — auto journal verdict. Default off.
    JOURNAL_VERDICT_AUTO_ENABLED: bool = False
    # ROB-405 Slice C — journal counterfactual sync. Default off.
    JOURNAL_COUNTERFACTUAL_ENABLED: bool = False
    # ROB-405 Slice E — watch follow-up report-item link. Default off.
    WATCH_FOLLOW_UP_LINK_ENABLED: bool = False

    # ROB-269 Phase 2 — gates BOTH the 4 MCP snapshot tools AND the
    # /trading/api/investment-snapshots/* GET router. Default off: code is
    # importable but unreachable from caller surfaces until flipped post-merge.
    # See docs/superpowers/plans/2026-05-19-rob-269-phase-2-mcp-api.md §2.
    INVESTMENT_SNAPSHOTS_MCP_ENABLED: bool = False
    # ROB-838 — frozen analysis evidence create/read MCP surface. Default off;
    # disabled tools are physically absent from every MCP profile.
    ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED: bool = False
    # ROB-459 P3 — context_get(draft_policy="advisory_only")에서 baseline으로 admit할
    # advisory 프로필을 운영자가 확장(default {HERMES_ADVISOR, CLAUDE_ADVISOR}와 UNION).
    # 빈 값이면 기본만. 스모크/테스트 프로필은 명시하지 않는 한 제외(fail-closed).
    INVESTMENT_ADVISORY_DRAFT_PROFILES: Annotated[list[str], NoDecode] = []
    # ROB-800 — allowlist of MCP caller agent ids permitted to place a
    # sanctioned loss_cut exit intent. Default = single Trader agent
    # (backcompat with prior implicit policy).
    LOSS_CUT_ALLOWED_AGENT_IDS: Annotated[list[str], NoDecode] = [
        "6b2192cc-14fa-4335-b572-2fe1e0cb54a7"
    ]
    # ROB-269 Phase 3 — gates service-side stale-gate enforcement on report
    # ingestion when account_scope='kis_live' + snapshot_bundle_uuid present.
    # DB CHECK ck_investment_reports_no_published_on_hard_stale is always live
    # (not flag-gated) — this flag only controls the pre-DB rejection layer.
    # See docs/superpowers/plans/2026-05-19-rob-269-phase-3-report-generator.md §5.
    ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED: bool = False
    # ROB-269 Phase 4 — scaffold only. NOT wired in Phase 4: there is no
    # HTTP endpoint exposing this flag to the SPA and the frontend chip
    # does NOT read it. The /invest SnapshotBundleFreshnessChip renders
    # on data-presence (``snapshotFreshnessSummary != null`` on the
    # InvestmentReport response) instead. The default-off semantic is
    # achieved upstream: Phase 3's
    # ``ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED`` is also default
    # off, so reports do not carry snapshot metadata, which keeps the
    # chip absent. This flag is reserved for future bundle-aware UI
    # surfaces that legitimately need a runtime per-user toggle (e.g. an
    # A/B) — wiring an endpoint + frontend hook is a follow-up.
    # See docs/superpowers/plans/2026-05-19-rob-269-phase-4-ui-and-scheduler.md §4.
    ACTION_REPORT_BUNDLE_UI_ENABLED: bool = False
    # ROB-273 — gates the snapshot-backed advisory report generator surface
    # (MCP tool + HTTP POST endpoint). The generator can still be constructed
    # and called directly from tests / scripts; this flag only controls the
    # user-facing entrypoints. Decoupled from
    # ``ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED`` because that flag
    # gates pre-DB rejection of *any* report carrying snapshot metadata —
    # turning on the generator surface independently lets us validate the
    # automated path against draft reports before activating the
    # stale-gate enforcement for the legacy create path.
    SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED: bool = False
    # ORDER PROPOSALS (ROB-816) — default-off SOT ledger + read/create MCP tools.
    # Gates MCP tool registration; the Telegram approval surface has its own gate (PR 2).
    ORDER_PROPOSALS_ENABLED: bool = False
    # ROB-871 — master gate for resting-class automatic submission. Telegram
    # approvals remain the fallback whenever this is off or eligibility fails.
    ORDER_PROPOSALS_AUTO_APPROVE: bool = False
    # ROB-816 PR 2 — Telegram approval flow (default off)
    ORDER_PROPOSALS_TELEGRAM_ENABLED: bool = False
    ORDER_PROPOSALS_TELEGRAM_BOT_TOKEN: str = ""
    ORDER_PROPOSALS_TELEGRAM_TOKEN: str = ""
    ORDER_PROPOSALS_TELEGRAM_TOKEN_HEADER: str = "X-Telegram-Bot-Api-Secret-Token"
    ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR: str = ""
    ORDER_PROPOSALS_SUBMIT_AGENT_ID: str = ""

    # ROB-897: gate for the scheduleless order_proposal valid_until expiry sweep
    # TaskIQ task. Default off -- the sweep runs manually first (dry_run MCP tool
    # `order_proposal_expire_sweep`); recurrence is a separate decision after
    # manual reps, mirroring `toss_manual_activity_sweep_enabled` (ROB-866).
    order_proposal_expire_sweep_enabled: bool = False

    @property
    def order_proposals_telegram_chat_allowlist(self) -> list[str]:
        return [
            chat_id.strip()
            for chat_id in self.ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR.split(",")
            if chat_id.strip()
        ]

    # ROB-214 — recurring reconciliation scheduler remains disabled unless explicitly enabled.
    execution_ledger_reconcile_scheduler_enabled: bool = False
    execution_ledger_reconcile_scheduler_cron: str = "*/30 * * * *"
    execution_ledger_reconcile_scheduler_window_hours: int = 24

    trader_agent_id: str = "6b2192cc-14fa-4335-b572-2fe1e0cb54a7"

    public_base_url: str = "https://mgh3326.duckdns.org"

    # Alpaca paper-trading broker adapter (ROB-57)
    # Only paper credentials/endpoint — no live trading support.
    alpaca_paper_api_key: str | None = None
    alpaca_paper_api_secret: SecretStr | None = None
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_paper_data_base_url: str = "https://data.alpaca.markets"

    # ROB-326 — US dual-paper premarket preview/preflight path (read-only, default off)
    us_dual_paper_preview_enabled: bool = False

    # ROB-842 — automated Alpaca paper submit boundary (preview→claim→POST). The
    # automated cohort broker mutation is fail-closed unless this gate is armed.
    alpaca_paper_automated_submit_enabled: bool = False

    @field_validator("alpaca_paper_base_url", mode="before")
    @classmethod
    def validate_alpaca_paper_base_url(cls, v: Any) -> str:
        _PAPER_URL = "https://paper-api.alpaca.markets"
        _FORBIDDEN = {"https://api.alpaca.markets", "https://data.alpaca.markets"}
        normalised = str(v).rstrip("/")
        if normalised in _FORBIDDEN:
            raise ValueError(
                f"alpaca_paper_base_url must be the paper endpoint "
                f"({_PAPER_URL}), got '{normalised}' which is a forbidden URL"
            )
        if normalised != _PAPER_URL:
            raise ValueError(
                f"alpaca_paper_base_url must be exactly '{_PAPER_URL}', "
                f"got '{normalised}'"
            )
        return normalised

    @field_validator("SECRET_KEY")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        """SECRET_KEY 보안 검증"""
        if len(v) < 32:
            raise ValueError(
                "SECRET_KEY는 최소 32자 이상이어야 합니다. "
                "openssl rand -hex 32 명령으로 생성하세요."
            )
        has_upper = any(c.isupper() for c in v)
        has_lower = any(c.islower() for c in v)
        has_digit = any(c.isdigit() for c in v)
        if not (has_upper and has_lower and has_digit):
            raise ValueError(
                "SECRET_KEY must contain uppercase, lowercase, and digits for security"
            )
        # 약한 기본값 차단
        weak_keys = [
            "your_secret_key_here",
            "changeme",
            "secret",
            "your_secret_key_here_use_openssl_rand_hex_32",
            "test",
            "password",
            "12345",
        ]
        if v.lower() in weak_keys:
            raise ValueError(
                f"보안 경고: '{v}'는 약한 SECRET_KEY입니다. "
                "프로덕션에서는 강력한 랜덤 키를 사용하세요. "
                "생성 방법: openssl rand -hex 32"
            )
        unique_chars = set(v)
        unique_ratio = len(unique_chars) / len(v)
        # 허용 가능한 강도: openssl rand -hex 32 결과(64자, ~0.25 고유도)도 통과해야 함
        if len(unique_chars) < 10 or unique_ratio < 0.2:
            raise ValueError(
                "SECRET_KEY의 엔트로피가 너무 낮습니다. "
                "openssl rand -hex 32 등으로 생성한 충분히 랜덤한 값을 사용하세요."
            )
        return v

    @field_validator("PUBLIC_API_PATHS", mode="before")
    @classmethod
    def validate_public_api_paths(cls, v: list[str] | str) -> list[str]:
        """Ensure PUBLIC_API_PATHS is parsed consistently from env strings."""
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return []
            if value.startswith("["):
                parsed = json.loads(value)
                if not isinstance(parsed, list) or not all(
                    isinstance(path, str) for path in parsed
                ):
                    raise ValueError(
                        "PUBLIC_API_PATHS JSON value must be a string list"
                    )
                return [path.strip() for path in parsed if path.strip()]
            return [path.strip() for path in value.split(",") if path.strip()]
        return v or []

    @field_validator("INVESTMENT_ADVISORY_DRAFT_PROFILES", mode="before")
    @classmethod
    def _parse_advisory_draft_profiles(cls, v: list[str] | str) -> list[str]:
        """Parse comma-separated or JSON-list env into a clean profile list.

        Mirrors ``validate_public_api_paths`` so operators can set
        ``INVESTMENT_ADVISORY_DRAFT_PROFILES=A_ADVISOR,B_ADVISOR`` (or a JSON
        list) in env. ``NoDecode`` on the field keeps pydantic-settings from
        JSON-decoding the raw string before this runs.
        """
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return []
            if value.startswith("["):
                parsed = json.loads(value)
                if not isinstance(parsed, list) or not all(
                    isinstance(p, str) for p in parsed
                ):
                    raise ValueError(
                        "INVESTMENT_ADVISORY_DRAFT_PROFILES JSON value must be a "
                        "string list"
                    )
                return [p.strip() for p in parsed if p.strip()]
            return [p.strip() for p in value.split(",") if p.strip()]
        return v or []

    @field_validator("LOSS_CUT_ALLOWED_AGENT_IDS", mode="before")
    @classmethod
    def _parse_loss_cut_allowlist(cls, v: list[str] | str) -> list[str]:
        """Parse comma-separated or JSON-list env into a clean agent-id list.

        ROB-800 — allowlist of MCP caller agent ids permitted to place a
        sanctioned loss_cut. Defaults to the single Trader agent (backcompat).
        """
        if isinstance(v, list):
            return [str(p).strip() for p in v if str(p).strip()]
        value = (v or "").strip()
        if not value:
            return []
        if value.startswith("["):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = []
            if isinstance(parsed, list):
                return [str(p).strip() for p in parsed if str(p).strip()]
        return [p.strip() for p in value.split(",") if p.strip()]

    @property
    def loss_cut_allowed_agent_ids(self) -> list[str]:
        return self.LOSS_CUT_ALLOWED_AGENT_IDS

    @field_validator("SENTRY_TRACES_SAMPLE_RATE", "SENTRY_PROFILES_SAMPLE_RATE")
    @classmethod
    def validate_sentry_sample_rate(cls, value: float) -> float:
        """Ensure Sentry sample rate is between 0.0 and 1.0."""
        if not 0.0 <= value <= 1.0:
            raise ValueError("Sentry sample rates must be between 0.0 and 1.0")
        return value

    # Environment setting for cookie security
    ENVIRONMENT: str = "development"  # development, production

    # Logging
    LOG_LEVEL: str = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL

    # API Documentation
    DOCS_ENABLED: bool = True  # 개발 환경: True, 프로덕션: False

    # TradingAgents advisory runner (ROB-9)
    tradingagents_repo_path: str | None = None
    tradingagents_python: str | None = None
    tradingagents_runner_path: str | None = None
    tradingagents_base_url: str = "http://127.0.0.1:8796/v1"
    tradingagents_model: str = "gpt-5.5"
    tradingagents_default_analysts: str = "market"
    tradingagents_subprocess_timeout_sec: int = 300
    tradingagents_max_debate_rounds: int = 1
    tradingagents_max_risk_discuss_rounds: int = 1
    tradingagents_max_recur_limit: int = 30
    tradingagents_output_language: str = "English"
    tradingagents_checkpoint_enabled: bool = False
    tradingagents_memory_log_path: str | None = None

    # Research Pipeline (ROB-112)
    RESEARCH_PIPELINE_ENABLED: bool = False
    RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED: bool = False
    RESEARCH_PIPELINE_DUAL_WRITE_ENABLED: bool = False

    # Naver Remote-Debug Audit (ROB-323)
    remote_debug_audit_enabled: bool = False

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,  # 대소문자 구분 안 함
        env_parse_none_str="None",  # None 문자열 파싱
        # JSON 자동 파싱 비활성화
        env_parse_enums=True,
        extra="ignore",  # 추가 필드 무시
    )


settings = _load_settings()  # import 하면 전역 singleton


def _has_nonempty_value(value: Any) -> bool:
    return bool(str(value or "").strip())


def validate_kis_mock_config(settings_obj: Any = settings) -> list[str]:
    """Return missing KIS mock env names without exposing configured values."""

    missing: list[str] = []
    if not bool(getattr(settings_obj, "kis_mock_enabled", False)):
        missing.append("KIS_MOCK_ENABLED")
    if not _has_nonempty_value(getattr(settings_obj, "kis_mock_app_key", None)):
        missing.append("KIS_MOCK_APP_KEY")
    if not _has_nonempty_value(getattr(settings_obj, "kis_mock_app_secret", None)):
        missing.append("KIS_MOCK_APP_SECRET")
    if not _has_nonempty_value(getattr(settings_obj, "kis_mock_account_no", None)):
        missing.append("KIS_MOCK_ACCOUNT_NO")
    return missing


def validate_kiwoom_mock_config(settings_obj: Any = settings) -> list[str]:
    """Return missing Kiwoom mock env names without exposing configured values."""

    missing: list[str] = []
    if not bool(getattr(settings_obj, "kiwoom_mock_enabled", False)):
        missing.append("KIWOOM_MOCK_ENABLED")
    if not _has_nonempty_value(getattr(settings_obj, "kiwoom_mock_app_key", None)):
        missing.append("KIWOOM_MOCK_APP_KEY")
    if not _has_nonempty_value(getattr(settings_obj, "kiwoom_mock_app_secret", None)):
        missing.append("KIWOOM_MOCK_APP_SECRET")
    if not _has_nonempty_value(getattr(settings_obj, "kiwoom_mock_account_no", None)):
        missing.append("KIWOOM_MOCK_ACCOUNT_NO")
    return missing


def validate_kiwoom_mock_us_config(settings_obj: Any = settings) -> list[str]:
    """Return missing Kiwoom mock US env names without exposing configured values.

    ROB-867: US namespace is completely independent from KR. It never reads or
    falls back to ``kiwoom_mock_*`` credentials.
    """

    missing: list[str] = []
    if not bool(getattr(settings_obj, "kiwoom_mock_us_enabled", False)):
        missing.append("KIWOOM_MOCK_US_ENABLED")
    if not _has_nonempty_value(getattr(settings_obj, "kiwoom_mock_us_app_key", None)):
        missing.append("KIWOOM_MOCK_US_APP_KEY")
    if not _has_nonempty_value(
        getattr(settings_obj, "kiwoom_mock_us_app_secret", None)
    ):
        missing.append("KIWOOM_MOCK_US_APP_SECRET")
    if not _has_nonempty_value(
        getattr(settings_obj, "kiwoom_mock_us_account_no", None)
    ):
        missing.append("KIWOOM_MOCK_US_ACCOUNT_NO")
    return missing


def validate_toss_api_config(settings_obj: Any = settings) -> list[str]:
    """Return missing Toss Open API env names without exposing configured values."""

    missing: list[str] = []
    if not bool(getattr(settings_obj, "toss_api_enabled", False)):
        missing.append("TOSS_API_ENABLED")
    if not _has_nonempty_value(getattr(settings_obj, "toss_api_client_id", None)):
        missing.append("TOSS_API_CLIENT_ID")
    if not _has_nonempty_value(getattr(settings_obj, "toss_api_client_secret", None)):
        missing.append("TOSS_API_CLIENT_SECRET")
    return missing


def validate_remote_debug_audit_config(settings_obj: Any = settings) -> list[str]:
    """Return missing env names for the remote-debug audit CLI (names only).

    Default-disabled: only ``REMOTE_DEBUG_AUDIT_ENABLED=true`` is required. The
    Chrome endpoint is fixed (127.0.0.1:9222) and carries no secret, so nothing
    else is gated here.
    """
    missing: list[str] = []
    if not bool(getattr(settings_obj, "remote_debug_audit_enabled", False)):
        missing.append("REMOTE_DEBUG_AUDIT_ENABLED")
    return missing
