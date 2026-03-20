#!/usr/bin/env python3
"""
Database Verification Script for Subtask 5-4
=============================================

This script verifies the kr_candles_1m table for:
- Correct data structure (OHLCV columns)
- Venue separation (KRX/NTX)
- No duplicate records
- Proper time format (KST naive)
- Valid OHLCV values

Usage:
    python verify_db_kr_candles_1m_subtask_5_4.py [symbol]

Default symbol: 005930 (Samsung Electronics)

Expected Results:
    - Returns 10 rows with venue in ('KRX', 'NTX')
    - No duplicates on (time, symbol, venue)
    - Times in KST naive format
    - Valid OHLCV values (high >= low, volume >= 0)
"""

import asyncio
import sys
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal


class Colors:
    """ANSI color codes for terminal output"""
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    END = "\033[0m"


def print_success(msg: str) -> None:
    print(f"{Colors.GREEN}✓ {msg}{Colors.END}")


def print_error(msg: str) -> None:
    print(f"{Colors.RED}✗ {msg}{Colors.END}")


def print_warning(msg: str) -> None:
    print(f"{Colors.YELLOW}⚠ {msg}{Colors.END}")


def print_info(msg: str) -> None:
    print(f"{Colors.BLUE}ℹ {msg}{Colors.END}")


def print_header(msg: str) -> None:
    print(f"\n{Colors.BOLD}{msg}{Colors.END}")
    print("=" * len(msg))


async def verify_table_structure(session: AsyncSession, symbol: str) -> bool:
    """Verify that kr_candles_1m table has the correct structure."""
    print_header("1. Verifying Table Structure")

    try:
        # Check if table exists and has data
        result = await session.execute(
            text("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'kr_candles_1m'
                AND table_schema = 'public'
                ORDER BY ordinal_position;
            """)
        )
        columns = result.fetchall()

        expected_columns = {
            "time": "timestamp without time zone",
            "symbol": "text",
            "venue": "text",
            "open": "double precision",
            "high": "double precision",
            "low": "double precision",
            "close": "double precision",
            "volume": "double precision",
            "value": "double precision",
        }

        found_columns = {row[0]: row[1] for row in columns}

        missing_columns = set(expected_columns.keys()) - set(found_columns.keys())
        if missing_columns:
            print_error(f"Missing columns: {missing_columns}")
            return False

        print_success(f"Table structure correct with {len(columns)} columns")
        return True

    except Exception as e:
        print_error(f"Failed to verify table structure: {e}")
        return False


async def verify_data_exists(session: AsyncSession, symbol: str) -> bool:
    """Verify that data exists for the given symbol."""
    print_header("2. Verifying Data Exists")

    try:
        result = await session.execute(
            text("""
                SELECT COUNT(*) as count
                FROM public.kr_candles_1m
                WHERE symbol = :symbol;
            """),
            {"symbol": symbol}
        )
        count = result.scalar()

        if count == 0:
            print_warning(f"No data found for symbol '{symbol}'")
            print_info("This is expected if E2E test has not been run yet")
            return False
        elif count < 10:
            print_warning(f"Only {count} records found for symbol '{symbol}' (expected at least 10)")
            return True
        else:
            print_success(f"Found {count} records for symbol '{symbol}'")
            return True

    except Exception as e:
        print_error(f"Failed to check data existence: {e}")
        return False


async def verify_sample_data(session: AsyncSession, symbol: str) -> bool:
    """Verify sample data structure and values."""
    print_header("3. Verifying Sample Data")

    try:
        result = await session.execute(
            text("""
                SELECT time, symbol, venue, open, high, low, close, volume
                FROM public.kr_candles_1m
                WHERE symbol = :symbol
                ORDER BY time DESC
                LIMIT 10;
            """),
            {"symbol": symbol}
        )
        rows = result.fetchall()

        if not rows:
            print_warning("No sample data to verify")
            return False

        print_success(f"Retrieved {len(rows)} sample rows")

        # Check each row for valid data
        all_valid = True
        for i, row in enumerate(rows, 1):
            time_val, symbol_val, venue, open_val, high_val, low_val, close_val, volume = row

            # Print row info
            print(f"\n  Row {i}:")
            print(f"    Time: {time_val}")
            print(f"    Symbol: {symbol_val}")
            print(f"    Venue: {venue}")
            print(f"    OHLC: {open_val:.2f} / {high_val:.2f} / {low_val:.2f} / {close_val:.2f}")
            print(f"    Volume: {volume:.0f}")

            # Validate venue
            if venue not in ("KRX", "NTX"):
                print_error(f"Invalid venue '{venue}' (expected 'KRX' or 'NTX')")
                all_valid = False
            else:
                print_success(f"  Venue '{venue}' is valid")

            # Validate OHLC values
            if high_val < low_val:
                print_error(f"Invalid OHLC: high ({high_val}) < low ({low_val})")
                all_valid = False
            else:
                print_success(f"  OHLC values are valid")

            # Validate volume
            if volume < 0:
                print_error(f"Invalid volume: {volume} (must be >= 0)")
                all_valid = False
            else:
                print_success(f"  Volume is valid")

        return all_valid

    except Exception as e:
        print_error(f"Failed to verify sample data: {e}")
        return False


async def verify_venue_separation(session: AsyncSession, symbol: str) -> bool:
    """Verify that venue separation is maintained."""
    print_header("4. Verifying Venue Separation")

    try:
        result = await session.execute(
            text("""
                SELECT DISTINCT venue
                FROM public.kr_candles_1m
                WHERE symbol = :symbol;
            """),
            {"symbol": symbol}
        )
        venues = [row[0] for row in result.fetchall()]

        if not venues:
            print_warning("No venues found")
            return False

        print_success(f"Found venues: {venues}")

        # Check that all venues are valid
        invalid_venues = [v for v in venues if v not in ("KRX", "NTX")]
        if invalid_venues:
            print_error(f"Invalid venues found: {invalid_venues}")
            return False

        print_success("All venues are valid (KRX/NTX)")
        return True

    except Exception as e:
        print_error(f"Failed to verify venue separation: {e}")
        return False


async def verify_no_duplicates(session: AsyncSession, symbol: str) -> bool:
    """Verify that there are no duplicate records."""
    print_header("5. Verifying No Duplicates")

    try:
        result = await session.execute(
            text("""
                SELECT time, symbol, venue, COUNT(*) as count
                FROM public.kr_candles_1m
                WHERE symbol = :symbol
                GROUP BY time, symbol, venue
                HAVING COUNT(*) > 1;
            """),
            {"symbol": symbol}
        )
        duplicates = result.fetchall()

        if duplicates:
            print_error(f"Found {len(duplicates)} duplicate records:")
            for dup in duplicates[:5]:  # Show first 5
                time_val, symbol_val, venue, count = dup
                print(f"  - {time_val}, {symbol_val}, {venue}: {count} records")
            return False
        else:
            print_success("No duplicate records found")
            return True

    except Exception as e:
        print_error(f"Failed to check for duplicates: {e}")
        return False


async def verify_time_format(session: AsyncSession, symbol: str) -> bool:
    """Verify that times are in KST naive format."""
    print_header("6. Verifying Time Format")

    try:
        result = await session.execute(
            text("""
                SELECT time
                FROM public.kr_candles_1m
                WHERE symbol = :symbol
                LIMIT 1;
            """),
            {"symbol": symbol}
        )
        row = result.fetchone()

        if not row:
            print_warning("No data to verify time format")
            return False

        time_val = row[0]

        # Check if time is timezone-aware or naive
        if time_val.tzinfo is not None:
            print_error(f"Time is timezone-aware: {time_val.tzinfo}")
            print_info("Expected: KST naive (no timezone info)")
            return False
        else:
            print_success(f"Time is KST naive (no timezone info)")
            print_info(f"Sample time: {time_val}")
            return True

    except Exception as e:
        print_error(f"Failed to verify time format: {e}")
        return False


async def verify_continuous_aggregate(session: AsyncSession, symbol: str) -> bool:
    """Verify that the continuous aggregate (kr_candles_1h) is accessible."""
    print_header("7. Verifying Continuous Aggregate")

    try:
        result = await session.execute(
            text("""
                SELECT COUNT(*) as count
                FROM public.kr_candles_1h
                WHERE symbol = :symbol;
            """),
            {"symbol": symbol}
        )
        count = result.scalar()

        if count == 0:
            print_warning("No data in kr_candles_1h continuous aggregate")
            print_info("This is expected if TimescaleDB has not refreshed yet")
            return True
        else:
            print_success(f"Found {count} hourly candles in continuous aggregate")
            return True

    except Exception as e:
        print_error(f"Failed to verify continuous aggregate: {e}")
        return False


async def main() -> int:
    """Main verification routine."""
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930"

    print_header(f"Database Verification for Symbol: {symbol}")
    print_info("Checking kr_candles_1m table structure and data...")

    async with AsyncSessionLocal() as session:
        results = {
            "Table Structure": await verify_table_structure(session, symbol),
            "Data Exists": await verify_data_exists(session, symbol),
            "Sample Data": await verify_sample_data(session, symbol),
            "Venue Separation": await verify_venue_separation(session, symbol),
            "No Duplicates": await verify_no_duplicates(session, symbol),
            "Time Format": await verify_time_format(session, symbol),
            "Continuous Aggregate": await verify_continuous_aggregate(session, symbol),
        }

    # Print summary
    print_header("Verification Summary")
    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for check, result in results.items():
        status = f"{Colors.GREEN}PASS{Colors.END}" if result else f"{Colors.RED}FAIL{Colors.END}"
        print(f"{check}: {status}")

    print(f"\nTotal: {passed}/{total} checks passed")

    if passed == total:
        print_success("\n✓ All verifications passed!")
        return 0
    else:
        print_warning(f"\n⚠ {total - passed} verification(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
