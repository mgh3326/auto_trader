"""Frozen D3 contract literals and runtime pins."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from importlib.metadata import version
from pathlib import Path

CONTRACT_SHA256 = {
    "d3-contract-draft-v3-20260806.md": (
        "c1bdee5769030e9e26c9bd185e479c02c54614a56ed0f5fb4c75d5f4a387e8c5"
    ),
    "d3-contract-draft-v2-20260806.md": (
        "734bffb88fc2e45312f9862645cc644c7694cca25ab633a5eddb48ce32537915"
    ),
    "operator-style-baseline-v1-20260806.md": (
        "f659486a6c06a9fcf266eadf6cffd548ca623904b873f07a3d3d1da581bb233f"
    ),
    "kospi_index_daily_2014_2024.csv": (
        "7088b7c3a8e7b7c2f5f724cc3891cd4d91457ce32894ac775c543a2282d9302e"
    ),
    "krx_tick_table_frozen.yaml": (
        "e2e41f3ffa76ee589c9abb2c57940d293f37e92884e9b1d27aab5a30959cae1e"
    ),
    "krx_tick_size_frozen.py": (
        "0b99d17d7fc59a9f8ef792d38548c85d37256360c424bba053c19a597bb17f18"
    ),
    "CONTRACT.md": ("b079c78823f7ba46f3e8fc52210953247fe47f4a9fa5b74cb204f33360b339c4"),
    "provenance.json": (
        "1a4b138b0ed3f09af46fd079b33d26e359c3c58e538b05a80a897256b0470a8d"
    ),
    "checksums.sha256": (
        "f03208c6f1e6ea360a743f261ea0698055493f3cf6800caeb98cd827313f89c0"
    ),
}

TICK_TABLE_SHA256 = CONTRACT_SHA256["krx_tick_table_frozen.yaml"]
INDEX_SHA256 = CONTRACT_SHA256["kospi_index_daily_2014_2024.csv"]

INITIAL_CASH = Decimal("10000000")
MONTHLY_CONTRIBUTION = Decimal("3500000")
ORDER_NOTIONAL = Decimal("300000")
C1_FILLED_GROSS_CAP = Decimal("1200000")
C1_MAX_ADDS = 6
FEE_RATE = Decimal("0.00215")
SENSITIVITY_FEE_RATE = Decimal("0.00415")
RSI_THRESHOLD = Decimal("45")
SUPPORT_MIN_DISTANCE = Decimal("-0.08")
SUPPORT_MAX_DISTANCE = Decimal("-0.03")
CONFLUENCE_TOLERANCE = Decimal("0.01")

DECIMAL_PRECISION = 50
DECIMAL_ROUNDING = ROUND_HALF_UP
TIMEZONE_NAME = "Asia/Seoul"
CALENDAR_NAME = "XKRX"
PYTHON_PIN = "3.13"
EXCHANGE_CALENDARS_PIN = "4.13.2"
TZDATA_PIN = "2026.1"

EXPLANATION_KEYS = frozenset(
    {
        "note",
        "notes",
        "role",
        "trap",
        "traps",
        "mutant_kill",
        "mutants_targeted",
        "scenarios",
        "contract_refs",
    }
)


@dataclass(frozen=True, slots=True)
class ArtifactPaths:
    """Read-only paths supplied to the local acceptance harness."""

    contract_v3: Path
    contract_v2: Path
    baseline: Path
    index_csv: Path
    tick_yaml: Path
    tick_python_provenance: Path
    golden_root: Path

    @classmethod
    def defaults(cls) -> ArtifactPaths:
        work = Path.home() / "work"
        inbox = work / "herdr-inbox"
        inputs = work / "herdr-artifacts" / "d3-contract-inputs-v1"
        golden = work / "herdr-artifacts" / "d3-golden-v1"
        return cls(
            contract_v3=inbox / "d3-contract-draft-v3-20260806.md",
            contract_v2=inbox / "d3-contract-draft-v2-20260806.md",
            baseline=inbox / "operator-style-baseline-v1-20260806.md",
            index_csv=inputs / "kospi_index_daily_2014_2024.csv",
            tick_yaml=inputs / "krx_tick_table_frozen.yaml",
            tick_python_provenance=inputs / "krx_tick_size_frozen.py",
            golden_root=golden,
        )


def runtime_pins() -> dict[str, str]:
    """Return pins and fail closed when the deterministic runtime drifts."""

    python_version = platform.python_version()
    if tuple(map(int, python_version.split(".")[:2])) != (3, 13):
        raise RuntimeError(f"RUN_INVALID_PYTHON_VERSION:{python_version}")

    exchange_calendars_version = version("exchange-calendars")
    if exchange_calendars_version != EXCHANGE_CALENDARS_PIN:
        raise RuntimeError(
            "RUN_INVALID_CALENDAR_VERSION:"
            f"{exchange_calendars_version}!={EXCHANGE_CALENDARS_PIN}"
        )

    tzdata_version = version("tzdata")
    if tzdata_version != TZDATA_PIN:
        raise RuntimeError(f"RUN_INVALID_TZDATA_VERSION:{tzdata_version}!={TZDATA_PIN}")

    return {
        "python": python_version,
        "decimal_precision": str(DECIMAL_PRECISION),
        "decimal_rounding": str(DECIMAL_ROUNDING),
        "timezone": TIMEZONE_NAME,
        "tzdata": tzdata_version,
        "calendar": CALENDAR_NAME,
        "exchange_calendars": exchange_calendars_version,
    }
