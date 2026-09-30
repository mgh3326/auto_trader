"""Shared KIS account-number parser for CANO / ACNT_PRDT_CD.

Every KIS client that builds a request body needs the configured account
number split into ``CANO`` (first 8 digits) and ``ACNT_PRDT_CD`` (the 2-digit
product code). This module is the single place that parse happens, for both
the live and the KIS mock (VTS) account scopes.

Scope rules
-----------
* Live (``is_mock=False``): unchanged legacy grammar — hyphens are stripped
  and the value must yield at least 10 characters; ``[:8]`` / ``[8:10]`` are
  returned.
* Mock (``is_mock=True``): strict grammar — exactly 8 digits, exactly 10
  digits, or the hyphenated ``8-2`` form. An 8-digit value means the product
  code was omitted and resolves to ``KIS_MOCK_DEFAULT_PRODUCT_CODE`` — the
  same implicit ``01`` the working broker-edge mock order path relies on
  (the edge envelope carries only ``account_scope`` and the edge service
  resolves the account). The default is applied inside the parser and only
  when the value is exactly 8 digits, so an already-qualified value can never
  receive it twice. Every other shape is rejected with a masked error.

Error contract
--------------
Malformed values raise ``ValueError`` whose message embeds the fixed
``[MASKED]`` placeholder (#1093) — never any part of the configured value,
its digits, or a product code. A missing/empty value raises a scope-aware
env-name message that contains no account material either.
"""

from __future__ import annotations

import re
from typing import Any, Final

__all__ = [
    "KIS_ACCOUNT_NO_ENV_LIVE",
    "KIS_ACCOUNT_NO_ENV_MOCK",
    "KIS_MOCK_DEFAULT_PRODUCT_CODE",
    "MASKED_ACCOUNT_VALUE",
    "mask_account_identifier",
    "parse_kis_account_parts",
    "resolve_kis_account_parts",
]

#: Settings key whose value feeds the parser per account scope.
KIS_ACCOUNT_NO_ENV_LIVE: Final[str] = "KIS_ACCOUNT_NO"
KIS_ACCOUNT_NO_ENV_MOCK: Final[str] = "KIS_MOCK_ACCOUNT_NO"

#: Implicit product code for a bare 8-digit mock account. The desk-verified
#: working mock order path is the broker-edge, which applies "01" edge-side;
#: this constant is the only in-repo place that supplies it.
KIS_MOCK_DEFAULT_PRODUCT_CODE: Final[str] = "01"

MASKED_ACCOUNT_VALUE: Final[str] = "[MASKED]"

# ASCII digits only — ``\d`` would also match Unicode decimal digits.
_MOCK_BARE_CANO_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{8}$")
_MOCK_FULL_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{10}$")
_MOCK_HYPHENATED_RE: Final[re.Pattern[str]] = re.compile(r"^([0-9]{8})-([0-9]{2})$")


def mask_account_identifier(value: str | None) -> str:
    """Return a fixed placeholder for an account identifier in error text."""
    if not value:
        return ""
    return MASKED_ACCOUNT_VALUE


def _invalid_account_error(value: str | None) -> ValueError:
    return ValueError(
        f"계좌번호 형식이 올바르지 않습니다: {mask_account_identifier(value)}"
    )


def parse_kis_account_parts(value: str | None, *, is_mock: bool) -> tuple[str, str]:
    """Split a configured KIS account number into ``(CANO, ACNT_PRDT_CD)``.

    ``is_mock`` describes the account BINDING (which credential namespace the
    settings view resolves), not the per-call TR flag. Mock values accept the
    bare 8-digit form with the implicit product code; live values keep the
    historical ``len >= 10`` acceptance unchanged.
    """

    env_name = KIS_ACCOUNT_NO_ENV_MOCK if is_mock else KIS_ACCOUNT_NO_ENV_LIVE
    if not value:
        raise ValueError(
            f"{env_name} 환경변수가 설정되지 않았습니다. "
            "계좌번호를 .env 파일에 추가해주세요."
        )
    if not isinstance(value, str):
        raise _invalid_account_error(value)

    if is_mock:
        text = value.strip()
        if _MOCK_BARE_CANO_RE.fullmatch(text):
            return text, KIS_MOCK_DEFAULT_PRODUCT_CODE
        if _MOCK_FULL_RE.fullmatch(text):
            return text[:8], text[8:10]
        hyphenated = _MOCK_HYPHENATED_RE.fullmatch(text)
        if hyphenated:
            return hyphenated.group(1), hyphenated.group(2)
        raise _invalid_account_error(value)

    account_no = value.replace("-", "")
    if len(account_no) < 10:
        raise _invalid_account_error(value)
    return account_no[:8], account_no[8:10]


def resolve_kis_account_parts(settings_view: Any) -> tuple[str, str]:
    """Parse the account number a KIS settings view is bound to.

    ``account_scope == "kis_mock"`` on the view marks the mock binding; any
    view without it (the live settings object, a live ``_KISSettingsView``)
    parses with the unchanged live grammar.
    """

    is_mock = getattr(settings_view, "account_scope", None) == "kis_mock"
    return parse_kis_account_parts(
        getattr(settings_view, "kis_account_no", None), is_mock=is_mock
    )
