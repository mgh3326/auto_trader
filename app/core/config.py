import os
from typing import List
import random

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class GoogleApiKeyManager:
    def __init__(self, api_keys: List[str]):
        if not api_keys:
            raise ValueError("최소 하나의 API 키가 필요합니다.")
        self.api_keys = api_keys
        self.current_index = 0
        self.usage_count = {key: 0 for key in api_keys}

    def get_next_key(self) -> str:
        """순환 방식으로 다음 키 반환"""
        key = self.api_keys[self.current_index]
        self.usage_count[key] += 1
        self.current_index = (self.current_index + 1) % len(self.api_keys)
        return key

    def get_random_key(self) -> str:
        """랜덤하게 키 선택"""
        key = random.choice(self.api_keys)
        self.usage_count[key] += 1
        return key

    def get_least_used_key(self) -> str:
        """가장 적게 사용된 키 반환"""
        key = min(self.usage_count, key=self.usage_count.get)
        self.usage_count[key] += 1
        return key

    def reset_usage(self):
        """사용량 카운터 초기화"""
        self.usage_count = {key: 0 for key in self.api_keys}

class Settings(BaseSettings):
    # KIS
    kis_app_key: str
    kis_app_secret: str
    kis_access_token: str | None = None  # 최초엔 비워두고 자동 발급
    # Telegram
    telegram_token: str
    telegram_chat_ids: list[int] = []
    # Strategy
    top_n: int = 30
    drop_pct: float = -3.0  # '-3'은 -3 %
    # Scheduler
    cron: str = "0 * * * *"  # 매시 정각
    google_api_key: str
    google_api_keys: List[str]  # 소문자로 변경

    def get_random_key(self) -> str:
        """랜덤 Google API 키 반환"""
        keys = self.google_api_keys or [self.google_api_key]
        return random.choice(keys)


    opendart_api_key: str
    DATABASE_URL: str
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False,  # 대소문자 구분 안 함
                                      )


settings = Settings()  # import 하면 전역 singleton
