import json
import os
import random
from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # KIS
    kis_app_key: str
    kis_app_secret: str
    kis_access_token: str | None = None  # 최초엔 비워두고 자동 발급
    kis_account_no: str | None = None  # 계좌번호 (예: "12345678-01")

    # KIS WebSocket
    kis_ws_is_mock: bool = False  # Mock 모드 (테스트용)
    kis_ws_hts_id: str = ""  # HTS ID (WebSocket 인증용)
    kis_ws_reconnect_delay_seconds: int = 5  # 재연결 대기 시간 (초)
    kis_ws_max_reconnect_attempts: int = 10  # 최대 재연결 시도 횟수
    kis_ws_ping_interval: int = 30  # Ping 전송 간격 (초)
    kis_ws_ping_timeout: int = 10  # Ping 응답 대기 시간 (초)

    # KIS Rate Limiting (HTTP API)
    kis_rate_limit_rate: int = 19  # 초당 최대 요청 수 (안전 마진으로 20-1)
    kis_rate_limit_period: float = 1.0  # 윈도우 기간 (초)

    # KIS Per-API Rate Limits (JSON map: "TR_ID|/path" -> {"rate": int, "period": float})
    kis_api_rate_limits: dict[str, dict[str, int | float]] = {}

    # Upbit Rate Limiting (HTTP API)
    upbit_rate_limit_rate: int = 10  # 초당 최대 요청 수
    upbit_rate_limit_period: float = 1.0  # 윈도우 기간 (초)

    # Upbit Per-API Rate Limits (JSON map: "METHOD /path" -> {"rate": int, "period": float})
    upbit_api_rate_limits: dict[str, dict[str, int | float]] = {}

    # API Rate Limit Retry Settings (429 handling)
    api_rate_limit_retry_429_max: int = 2  # 429 에러 시 최대 재시도 횟수
    api_rate_limit_retry_429_base_delay: float = 0.2  # 지수 백오프 기본 대기 시간 (초)
    # Telegram
    telegram_token: str
    telegram_chat_id: str = ""
    # Strategy
    top_n: int = 30
    drop_pct: float = -3.0  # '-3'은 -3 %
    # Scheduler
    cron: str = "0 * * * *"  # 매시 정각
    google_api_key: str
    google_api_keys: list[str] | None = None

    @property
    def telegram_chat_ids(self) -> list[str]:
        """단일 chat_id를 리스트로 변환 (하위 호환성 유지)"""
        if not self.telegram_chat_id:
            return []
        return [self.telegram_chat_id.strip()]

    @field_validator("kis_api_rate_limits", "upbit_api_rate_limits", mode="before")
    @classmethod
    def parse_api_rate_limits(cls, v: Any) -> dict[str, dict[str, int | float]]:
        """Parse JSON string or dict for API rate limits."""
        if isinstance(v, str):
            if not v:
                return {}
            try:
                parsed = json.loads(v)
                if not isinstance(parsed, dict):
                    raise ValueError("API rate limits must be a JSON object")
                return parsed
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON for API rate limits: {e}") from e
        if isinstance(v, dict):
            return v
        return {}

    @field_validator("google_api_keys", mode="before")
    @classmethod
    def split_google_api_keys(cls, v: any) -> list[str]:
        if isinstance(v, str):
            if not v:  # 빈 문자열 처리
                return []
            return [key.strip() for key in v.split(",") if key.strip()]
        return v

    def _ensure_key_index(self):
        """API 키 인덱스 초기화 (필요시에만)"""
        if not hasattr(self, "_current_key_index"):
            keys = self.google_api_keys or [self.google_api_key]
            self._current_key_index = random.randint(0, len(keys) - 1)

    def get_random_key(self) -> str:
        """랜덤 Google API 키 반환"""
        keys = self.google_api_keys or [self.google_api_key]
        self._ensure_key_index()
        random_index = random.randint(0, len(keys) - 1)
        self._current_key_index = random_index
        return keys[random_index]

    def get_next_key(self) -> str:
        """순환 방식으로 다음 Google API 키 반환"""
        keys = self.google_api_keys or [self.google_api_key]
        self._ensure_key_index()
        self._current_key_index = (self._current_key_index + 1) % len(keys)
        key = keys[self._current_key_index]
        return key

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
    DATABASE_URL: str
    upbit_access_key: str
    upbit_secret_key: str

    # Finnhub API (optional - for news and fundamentals)
    finnhub_api_key: str | None = None

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
    redis_max_connections: int = 10
    redis_socket_timeout: int = 5
    redis_socket_connect_timeout: int = 5

    # Sentry
    SENTRY_DSN: str = ""
    SENTRY_ENVIRONMENT: str | None = None
    SENTRY_RELEASE: str | None = None
    SENTRY_TRACES_SAMPLE_RATE: float = 1.0
    SENTRY_PROFILES_SAMPLE_RATE: float = 1.0
    SENTRY_SEND_DEFAULT_PII: bool = True
    SENTRY_ENABLE_LOG_EVENTS: bool = True

    # Monitoring test route exposure
    EXPOSE_MONITORING_TEST_ROUTES: bool = False

    # OpenClaw integration
    OPENCLAW_WEBHOOK_URL: str = "http://localhost:18789/hooks/agent"
    OPENCLAW_TOKEN: str = ""
    OPENCLAW_CALLBACK_TOKEN: str = ""  # shared secret for inbound callback auth
    OPENCLAW_CALLBACK_URL: str = "http://localhost:8000/api/v1/openclaw/callback"
    OPENCLAW_ENABLED: bool = False

    # JWT Authentication settings
    SECRET_KEY: str
    ALGORITHM: Literal["HS256", "HS384", "HS512"] = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    SESSION_BLACKLIST_FAIL_SAFE: bool = True
    SESSION_BLACKLIST_DB_FALLBACK: bool = True
    PUBLIC_API_PATHS: list[str] = []

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
            return [path.strip() for path in v.split(",") if path.strip()]
        return v or []

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

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,  # 대소문자 구분 안 함
        env_parse_none_str="None",  # None 문자열 파싱
        # JSON 자동 파싱 비활성화
        env_parse_enums=True,
        extra="ignore",  # 추가 필드 무시
    )


settings = Settings()  # import 하면 전역 singleton
