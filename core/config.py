from pydantic_settings import BaseSettings, SettingsConfigDict


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
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()  # import 하면 전역 singleton
