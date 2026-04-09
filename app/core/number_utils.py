"""Korean number format parsing utilities.

Shared parser for Korean number formats used across KRX, Naver Finance,
and other Korean financial data sources.
"""

from __future__ import annotations

import re


def parse_korean_number(value_str: str | None) -> int | float | None:
    """Parse Korean number formats.

    Handles formats like:
    - "1,234" → 1234
    - "5.67%" → 0.0567
    - "1조 2,345억" → 1,234,500,000,000
    - "▼1,234" or "-1,234" → -1234
    - "-" → None

    Args:
        value_str: Number string in Korean format

    Returns:
        Parsed number (int for whole numbers, float for decimals) or None
    """
    if not value_str:
        return None

    # Remove whitespace
    cleaned = value_str.strip()
    if not cleaned or cleaned == "-":
        return None

    # Handle percentage
    is_percent = "%" in cleaned
    cleaned = cleaned.replace("%", "")

    # Handle negative indicators
    is_negative = (
        cleaned.startswith("-")
        or "▼" in cleaned
        or "하락" in cleaned
        or cleaned.startswith("−")  # Unicode minus
    )
    cleaned = re.sub(r"[▲▼하락상승\-+−]", "", cleaned)

    # Remove commas and spaces
    cleaned = cleaned.replace(",", "").replace(" ", "")

    # Handle Korean units (조, 억, 만)
    # Process from largest to smallest
    total = 0.0
    remaining = cleaned

    # 조 (trillion in Korean, 10^12)
    if "조" in remaining:
        parts = remaining.split("조")
        try:
            jo_value = float(parts[0]) if parts[0] else 0
            total += jo_value * 1_0000_0000_0000
            remaining = parts[1] if len(parts) > 1 else ""
        except ValueError:
            pass

    # 억 (hundred million, 10^8)
    if "억" in remaining:
        parts = remaining.split("억")
        try:
            eok_value = float(parts[0]) if parts[0] else 0
            total += eok_value * 1_0000_0000
            remaining = parts[1] if len(parts) > 1 else ""
        except ValueError:
            pass

    # 만 (ten thousand, 10^4)
    if "만" in remaining:
        parts = remaining.split("만")
        try:
            man_value = float(parts[0]) if parts[0] else 0
            total += man_value * 1_0000
            remaining = parts[1] if len(parts) > 1 else ""
        except ValueError:
            pass

    # Add any remaining number
    if remaining:
        try:
            total += float(remaining)
        except ValueError:
            if total == 0:
                return None

    # If no Korean units were found, try parsing as plain number
    if total == 0 and not any(unit in value_str for unit in ["조", "억", "만"]):
        try:
            total = float(cleaned)
        except ValueError:
            return None

    # Apply percentage
    if is_percent:
        total = total / 100

    # Apply negative
    if is_negative:
        total = -abs(total)

    # Return int if whole number, float otherwise
    if total == int(total) and not is_percent:
        return int(total)
    return total
