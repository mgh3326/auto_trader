"""KIS 선물옵션 모의 계좌 전용 자격증명 (hk 6022).

선물옵션 모의 계좌(상품코드 ``03``)는 KIS 가 주식 모의 계좌와 별도로 발급한 앱키·
시크릿을 쓴다. 이 모듈은 그 env 이름과 누락 거부만 정한다 — 설정값을 로그나
예외에 담지 않고, 누락 시 **env 이름만** 보고한다.
"""

from __future__ import annotations

FUTURES_MOCK_CREDENTIAL_ENV_NAMES: tuple[str, ...] = (
    "KIS_FUTURES_MOCK_APP_KEY",
    "KIS_FUTURES_MOCK_APP_SECRET",
)

#: env 이름 ↔ settings 필드 (값은 절대 출력하지 않는다).
_ENV_TO_SETTING: tuple[tuple[str, str], ...] = (
    ("KIS_FUTURES_MOCK_APP_KEY", "kis_futures_mock_app_key"),
    ("KIS_FUTURES_MOCK_APP_SECRET", "kis_futures_mock_app_secret"),
)


class FuturesMockCredentialsMissing(ValueError):
    """선물옵션 모의 앱키·시크릿이 없다 — 네트워크·토큰 조회 전에 거부한다.

    ``missing`` 은 env 이름만 담는다. 설정된 값의 일부도 포함하지 않는다.
    """

    def __init__(self, missing: list[str] | tuple[str, ...]) -> None:
        self.missing: tuple[str, ...] = tuple(missing)
        super().__init__(
            "KIS 선물옵션 모의 자격증명이 설정되지 않았습니다: "
            + ", ".join(self.missing)
        )


def missing_futures_mock_credentials(settings_obj: object) -> list[str]:
    """Return missing env names only — never configured values."""
    return [
        env_name
        for env_name, attr in _ENV_TO_SETTING
        if not str(getattr(settings_obj, attr, None) or "").strip()
    ]


def require_futures_mock_credentials(settings_obj: object) -> None:
    """Raise :class:`FuturesMockCredentialsMissing` when either key is absent."""
    missing = missing_futures_mock_credentials(settings_obj)
    if missing:
        raise FuturesMockCredentialsMissing(missing)


__all__ = [
    "FUTURES_MOCK_CREDENTIAL_ENV_NAMES",
    "FuturesMockCredentialsMissing",
    "missing_futures_mock_credentials",
    "require_futures_mock_credentials",
]
