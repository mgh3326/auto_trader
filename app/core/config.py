import random
from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # KIS
    kis_app_key: str
    kis_app_secret: str
    kis_access_token: str | None = None  # 최초엔 비워두고 자동 발급
    # Telegram
    telegram_token: str
    telegram_chat_ids_str: str = ""
    # Strategy
    top_n: int = 30
    drop_pct: float = -3.0  # '-3'은 -3 %
    # Scheduler
    cron: str = "0 * * * *"  # 매시 정각
    google_api_key: str
    google_api_keys: Optional[List[str]] = None

    @property
    def telegram_chat_ids(self) -> List[str]:
        """콤마로 구분된 문자열을 리스트로 변환"""
        if not self.telegram_chat_ids_str:
            return []
        return [chat_id.strip() for chat_id in self.telegram_chat_ids_str.split(',') if chat_id.strip()]

    @field_validator("google_api_keys", mode='before')
    @classmethod
    def split_google_api_keys(cls, v: any) -> List[str]:
        # --- 디버깅 코드 추가 ---
        print(f"Validator for google_api_keys received value: {v!r}")
        print(f"Type of received value: {type(v)}")
        # ---------------------

        if isinstance(v, str):
            if not v:  # 빈 문자열 처리
                return []
            return [key.strip() for key in v.split(',') if key.strip()]
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

        return (
            f"{protocol}{auth_part}{self.redis_host}:{self.redis_port}/{self.redis_db}"
        )

    opendart_api_key: str
    DATABASE_URL: str
    upbit_access_key: str
    upbit_secret_key: str

    # Upbit 매수 설정
    upbit_buy_amount: int = 10000  # 분할 매수 금액 (기본 10만원)
    upbit_min_krw_balance: int = upbit_buy_amount + 5000  # 최소 KRW 잔고 (기본 10만원)

    # Redis 설정
    redis_url: str | None = None  # .env에서 설정하거나 None으로 두면 개별 설정 사용
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None
    redis_ssl: bool = False

    # Redis 연결 풀 설정
    redis_max_connections: int = 10
    redis_socket_timeout: int = 5
    redis_socket_connect_timeout: int = 5
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,  # 대소문자 구분 안 함
        env_parse_none_str="None",  # None 문자열 파싱
        # JSON 자동 파싱 비활성화
        env_parse_enums=True,
        extra='ignore',  # 추가 필드 무시
    )


settings = Settings()  # import 하면 전역 singleton
