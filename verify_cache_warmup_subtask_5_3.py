#!/usr/bin/env python3
"""
Verification script for Subtask 5-3: Cache Warm-up Test

This script verifies that:
1. First query (cold) triggers KIS API fallback
2. Second query (warm) hits DB instead of API
3. Query returns faster on second call
4. No duplicate data in kr_candles_1m table
"""

import asyncio
import sys
import time
import logging
from datetime import datetime
from typing import Any
import pandas as pd

# Setup logging to capture log messages
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Import after logging setup
from app.services.kr_hourly_candles_read_service import read_kr_hourly_candles_1h
from app.core.db import AsyncSessionLocal
from sqlalchemy import text


class LogCapture(logging.Handler):
    """Custom logging handler to capture log messages."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(self.format(record))


async def check_db_for_duplicates(symbol: str) -> dict[str, Any]:
    """Check for duplicate records in kr_candles_1m table."""
    async with AsyncSessionLocal() as session:
        # Check for duplicates
        result = await session.execute(
            text("""
                SELECT
                    time, symbol, venue, COUNT(*) as count
                FROM public.kr_candles_1m
                WHERE symbol = :symbol
                GROUP BY time, symbol, venue
                HAVING COUNT(*) > 1
            """),
            {"symbol": symbol}
        )
        duplicates = result.fetchall()

        # Get total count
        count_result = await session.execute(
            text("""
                SELECT COUNT(*) as total_count
                FROM public.kr_candles_1m
                WHERE symbol = :symbol
            """),
            {"symbol": symbol}
        )
        total_count = count_result.scalar()

        # Get venue distribution
        venue_result = await session.execute(
            text("""
                SELECT venue, COUNT(*) as count
                FROM public.kr_candles_1m
                WHERE symbol = :symbol
                GROUP BY venue
                ORDER BY venue
            """),
            {"symbol": symbol}
        )
        venue_dist = {row[0]: row[1] for row in venue_result.fetchall()}

        return {
            "duplicates": len(duplicates),
            "total_count": total_count,
            "venue_distribution": venue_dist,
            "duplicate_details": duplicates
        }


async def run_cache_warmup_test(
    symbol: str = "005930",
    count: int = 5
) -> dict[str, Any]:
    """
    Run cache warm-up test.

    Returns:
        Dictionary with test results including:
        - first_call: dict with timing and log analysis
        - second_call: dict with timing and log analysis
        - duplicate_check: dict with duplicate analysis
        - passed: bool indicating if test passed
    """
    print("\n" + "="*80)
    print("CACHE WARM-UP VERIFICATION TEST - Subtask 5-3")
    print("="*80)
    print(f"Symbol: {symbol}")
    print(f"Requested candles: {count}")
    print("="*80 + "\n")

    # Setup log capture
    log_capture = LogCapture()
    log_capture.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
    logger = logging.getLogger("app.services.kr_hourly_candles_read_service")
    logger.addHandler(log_capture)

    results = {
        "first_call": {},
        "second_call": {},
        "duplicate_check": {},
        "passed": False
    }

    try:
        # ===== FIRST CALL (Cold Query) =====
        print("🔵 FIRST CALL (Cold Query) - Expecting API fallback")
        print("-" * 80)

        log_capture.messages = []
        start_time = time.time()

        try:
            df1 = await read_kr_hourly_candles_1h(
                symbol=symbol,
                count=count,
                end_date=None,
                now_kst=datetime.now()
            )
            first_duration = time.time() - start_time
            first_candles = len(df1)

            print(f"✓ First call completed in {first_duration:.3f}s")
            print(f"✓ Returned {first_candles} candles")

            # Analyze logs
            log_messages = " ".join(log_capture.messages)
            has_db_log = "DB returned" in log_messages
            has_api_fallback = "Fallback to KIS API" in log_messages
            has_background_task = "Background task created" in log_messages

            print(f"✓ Log analysis:")
            print(f"  - DB query logged: {has_db_log}")
            print(f"  - API fallback triggered: {has_api_fallback}")
            print(f"  - Background task created: {has_background_task}")

            results["first_call"] = {
                "duration": first_duration,
                "candles_returned": first_candles,
                "db_query_logged": has_db_log,
                "api_fallback_triggered": has_api_fallback,
                "background_task_created": has_background_task,
                "success": True
            }

        except Exception as e:
            first_duration = time.time() - start_time
            print(f"✗ First call failed after {first_duration:.3f}s: {e}")
            results["first_call"] = {
                "duration": first_duration,
                "success": False,
                "error": str(e)
            }

        print()

        # ===== WAIT for background task =====
        print("⏳ Waiting 3 seconds for background storage to complete...")
        print("-" * 80)
        await asyncio.sleep(3)
        print("✓ Wait complete\n")

        # ===== SECOND CALL (Warm Query) =====
        print("🟢 SECOND CALL (Warm Query) - Expecting DB cache hit")
        print("-" * 80)

        log_capture.messages = []
        start_time = time.time()

        try:
            df2 = await read_kr_hourly_candles_1h(
                symbol=symbol,
                count=count,
                end_date=None,
                now_kst=datetime.now()
            )
            second_duration = time.time() - start_time
            second_candles = len(df2)

            print(f"✓ Second call completed in {second_duration:.3f}s")
            print(f"✓ Returned {second_candles} candles")

            # Analyze logs
            log_messages = " ".join(log_capture.messages)
            has_db_log = "DB returned" in log_messages
            has_api_fallback = "Fallback to KIS API" in log_messages
            has_background_task = "Background task created" in log_messages

            print(f"✓ Log analysis:")
            print(f"  - DB query logged: {has_db_log}")
            print(f"  - API fallback triggered: {has_api_fallback} (should be False)")
            print(f"  - Background task created: {has_background_task} (should be False)")

            results["second_call"] = {
                "duration": second_duration,
                "candles_returned": second_candles,
                "db_query_logged": has_db_log,
                "api_fallback_triggered": has_api_fallback,
                "background_task_created": has_background_task,
                "success": True
            }

        except Exception as e:
            second_duration = time.time() - start_time
            print(f"✗ Second call failed after {second_duration:.3f}s: {e}")
            results["second_call"] = {
                "duration": second_duration,
                "success": False,
                "error": str(e)
            }

        print()

        # ===== DUPLICATE CHECK =====
        print("🔍 CHECKING FOR DUPLICATES in kr_candles_1m table")
        print("-" * 80)

        dup_check = await check_db_for_duplicates(symbol)

        print(f"✓ Total records in DB: {dup_check['total_count']}")
        print(f"✓ Duplicate records found: {dup_check['duplicates']}")
        print(f"✓ Venue distribution: {dup_check['venue_distribution']}")

        results["duplicate_check"] = {
            "total_records": dup_check["total_count"],
            "duplicate_count": dup_check["duplicates"],
            "venue_distribution": dup_check["venue_distribution"],
            "passed": dup_check["duplicates"] == 0
        }

        print()

        # ===== PERFORMANCE ANALYSIS =====
        print("📊 PERFORMANCE ANALYSIS")
        print("-" * 80)

        if results["first_call"].get("success") and results["second_call"].get("success"):
            speedup = results["first_call"]["duration"] / results["second_call"]["duration"]
            print(f"✓ First call (cold): {results['first_call']['duration']:.3f}s")
            print(f"✓ Second call (warm): {results['second_call']['duration']:.3f}s")
            print(f"✓ Speedup: {speedup:.1f}x")

            results["performance"] = {
                "first_call_duration": results["first_call"]["duration"],
                "second_call_duration": results["second_call"]["duration"],
                "speedup_factor": speedup
            }
        else:
            print("✗ Performance analysis skipped due to call failures")
            results["performance"] = None

        print()

        # ===== FINAL VERDICT =====
        print("="*80)
        print("FINAL VERDICT")
        print("="*80)

        # Check conditions
        checks = {
            "First call succeeded": results["first_call"].get("success", False),
            "First call triggered API fallback": results["first_call"].get("api_fallback_triggered", False),
            "Second call succeeded": results["second_call"].get("success", False),
            "Second call did NOT trigger API fallback": not results["second_call"].get("api_fallback_triggered", True),
            "Second call hit DB": results["second_call"].get("db_query_logged", False),
            "No duplicate records": dup_check["duplicates"] == 0,
            "Warm query faster": (
                results["second_call"].get("duration", float('inf')) <
                results["first_call"].get("duration", 0)
            )
        }

        all_passed = True
        for check_name, passed in checks.items():
            status = "✓ PASS" if passed else "✗ FAIL"
            print(f"{status}: {check_name}")
            if not passed:
                all_passed = False

        print()
        if all_passed:
            print("🎉 ALL CHECKS PASSED - Cache warm-up is working correctly!")
        else:
            print("⚠️  SOME CHECKS FAILED - Review results above")

        results["passed"] = all_passed

        print("="*80 + "\n")

    finally:
        # Cleanup
        logger.removeHandler(log_capture)

    return results


async def main():
    """Main entry point."""
    if len(sys.argv) > 1:
        symbol = sys.argv[1]
    else:
        symbol = "005930"  # Samsung Electronics

    if len(sys.argv) > 2:
        count = int(sys.argv[2])
    else:
        count = 5

    results = await run_cache_warmup_test(symbol, count)

    # Exit with appropriate code
    sys.exit(0 if results["passed"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
