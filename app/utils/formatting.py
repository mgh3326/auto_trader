"""Formatting utilities for numbers and quantities."""


def format_decimal(value: float, currency: str = "₩") -> str:
    """
    값의 크기에 따라 적절한 소수점 자릿수를 결정하여 포맷팅

    Args:
        value: 포맷팅할 값
        currency: 통화 단위 (₩, $ 등)

    Returns:
        포맷팅된 문자열
    """
    if value == 0:
        return "0"

    abs_value = abs(value)

    # 한국 원화 (₩) 기준
    if currency == "₩":
        if abs_value >= 1000000:  # 100만원 이상
            return f"{value:,.0f}"
        if abs_value >= 10000:  # 1만원 이상
            return f"{value:,.1f}"
        return f"{value:,.2f}"

    # 미국 달러 ($) 기준
    elif currency == "$":
        if abs_value >= 10:  # $10 이상
            return f"{value:,.2f}"
        return f"{value:,.3f}"

    # 암호화폐 등 기타 통화 (기본값)
    else:
        if abs_value >= 1000:  # 1000 이상
            return f"{value:,.2f}"
        elif abs_value >= 100:  # 100 이상
            return f"{value:,.3f}"
        elif abs_value >= 10:  # 10 이상
            return f"{value:,.4f}"
        elif abs_value >= 1:  # 1 이상
            return f"{value:,.5f}"
        elif abs_value >= 0.1:  # 0.1 이상
            return f"{value:,.6f}"
        elif abs_value >= 0.01:  # 0.01 이상
            return f"{value:,.7f}"
        else:  # 0.01 미만
            return f"{value:,.8f}"


def format_quantity(quantity: float, unit_shares: str = "개") -> str:
    """
    수량을 적절한 소수점 자릿수로 포맷팅

    Args:
        quantity: 수량
        unit_shares: 단위 (개, 주 등)

    Returns:
        포맷팅된 문자열
    """
    if quantity == 0:
        return "0"

    abs_quantity = abs(quantity)

    # 주식의 경우 (보통 정수 단위)
    if unit_shares == "주":
        return f"{quantity:,.0f}"

    # 암호화폐의 경우 (소수점 포함)
    elif unit_shares == "개":
        if abs_quantity >= 1000:  # 1000개 이상
            return f"{quantity:,.2f}"
        elif abs_quantity >= 100:  # 100개 이상
            return f"{quantity:,.3f}"
        elif abs_quantity >= 10:  # 10개 이상
            return f"{quantity:,.4f}"
        elif abs_quantity >= 1:  # 1개 이상
            return f"{quantity:,.5f}"
        elif abs_quantity >= 0.1:  # 0.1개 이상
            return f"{quantity:,.6f}"
        elif abs_quantity >= 0.01:  # 0.01개 이상
            return f"{quantity:,.7f}"
        else:  # 0.01개 미만
            return f"{quantity:,.8f}"

    # 기타 단위
    else:
        if abs_quantity >= 1000:
            return f"{quantity:,.2f}"
        elif abs_quantity >= 100:
            return f"{quantity:,.3f}"
        elif abs_quantity >= 10:
            return f"{quantity:,.4f}"
        elif abs_quantity >= 1:
            return f"{quantity:,.5f}"
        else:
            return f"{quantity:,.6f}"
