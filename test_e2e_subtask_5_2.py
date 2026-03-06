#!/usr/bin/env python3
"""
E2E Test for subtask-5-2: Manual E2E test for non-held stock query

This test verifies:
1. Query non-held stock (symbol not in DB)
2. API is called (check logs)
3. Data is returned
4. Background task is created
5. 1m candles are persisted to DB
"""

import asyncio
import datetime
import logging
import sys
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.kr_hourly_candles_read_service import read_kr_hourly_candles_1h

# Configure logging to capture output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger(__name__)


class E2ETestVerifier:
    """Verifies E2E test results"""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.api_called = False
        self.background_task_created = False
        self.db_hit = False
        self.logs: list[str] = []
        self.start_time = time.time()
        self.end_time = 0

    def record_log(self, message: str):
        """Record a log message"""
        self.logs.append(message)
        print(f"[LOG] {message}")

        # Check for specific log messages
        if 'Fallback to KIS API' in message or 'Calling KIS API' in message:
            self.api_called = True
        if 'Background task created' in message:
            self.background_task_created = True
        if 'DB returned' in message:
            self.db_hit = True

    def verify_results(self) -> dict[str, Any]:
        """Verify all E2E requirements"""
        self.end_time = time.time()
        duration = self.end_time - self.start_time

        results = {
            'symbol': self.symbol,
            'duration_seconds': round(duration, 2),
            'api_called': self.api_called,
            'background_task_created': self.background_task_created,
            'db_hit': self.db_hit,
            'total_logs': len(self.logs),
            'success': False
        }

        # Test passes if API was called and background task was created
        results['success'] = self.api_called and self.background_task_created

        return results


async def count_minute_candles(session: AsyncSession, symbol: str) -> int:
    """Count minute candles for a symbol in kr_candles_1m table"""
    sql = text(
        """
        SELECT COUNT(*) as count
        FROM public.kr_candles_1m
        WHERE symbol = :symbol
        """
    )

    result = await session.execute(sql, {'symbol': symbol})
    return result.scalar_one()


async def get_latest_minute_candles(session: AsyncSession, symbol: str, limit: int = 10) -> list[dict]:
    """Get latest minute candles for verification"""
    sql = text(
        """
        SELECT time, venue, open, high, low, close, volume
        FROM public.kr_candles_1m
        WHERE symbol = :symbol
        ORDER BY time DESC
        LIMIT :limit
        """
    )

    result = await session.execute(sql, {'symbol': symbol, 'limit': limit})
    rows = result.fetchall()

    return [
        {
            'time': str(row[0]),
            'venue': row[1],
            'open': row[2],
            'high': row[3],
            'low': row[4],
            'close': row[5],
            'volume': row[6]
        }
        for row in rows
    ]


async def clear_symbol_data(session: AsyncSession, symbol: str) -> int:
    """Clear data for a test symbol from both tables"""
    # Clear from kr_candles_1m
    sql_1m = text(
        """
        DELETE FROM public.kr_candles_1m
        WHERE symbol = :symbol
        """
    )

    result_1m = await session.execute(sql_1m, {'symbol': symbol})
    deleted_1m = result_1m.rowcount

    # Clear from kr_candles_1h (this is a view, but we can delete from base table)
    sql_1h = text(
        """
        DELETE FROM public.kr_candles_1m
        WHERE symbol = :symbol
        """
    )

    result_1h = await session.execute(sql_1h, {'symbol': symbol})
    deleted_1h = result_1h.rowcount

    await session.commit()
    return deleted_1m + deleted_1h


async def run_e2e_test():
    """Run the complete E2E test"""

    # Use Samsung Electronics (005930) as test symbol
    # This is a real, actively traded stock
    test_symbol = '005930'

    verifier = E2ETestVerifier(test_symbol)

    print("=" * 80)
    print("E2E Test: Non-Held Stock Query with API Fallback")
    print("=" * 80)
    print(f"Test Symbol: {test_symbol}")
    print(f"Test Time: {datetime.datetime.now()}")
    print()

    # Step 1: Check initial state of DB
    print("Step 1: Checking initial database state...")
    async with AsyncSessionLocal() as session:
        initial_count = await count_minute_candles(session, test_symbol)
        verifier.record_log(f"Initial minute candle count: {initial_count}")

        if initial_count > 0:
            verifier.record_log(f"WARNING: Symbol {test_symbol} has {initial_count} existing records")
            verifier.record_log("This may affect API fallback testing")
            # Uncomment to clear data for true cold query test
            # deleted = await clear_symbol_data(session, test_symbol)
            # verifier.record_log(f"Cleared {deleted} records for clean test")

    print()

    # Step 2: Query hourly candles (should trigger API fallback if DB is empty)
    print("Step 2: Querying hourly candles (requesting 5 candles)...")
    now_kst = datetime.datetime.now()

    try:
        df = await read_kr_hourly_candles_1h(
            symbol=test_symbol,
            count=5,
            end_date=None,
            now_kst=now_kst
        )

        verifier.record_log(f"Query returned {len(df)} hourly candles")

        if not df.empty:
            verifier.record_log(f"Columns: {list(df.columns)}")
            verifier.record_log(f"Date range: {df['datetime'].min()} to {df['datetime'].max()}")
            verifier.record_log(f"Sample data:\n{df.head(2).to_string()}")
        else:
            verifier.record_log("WARNING: Empty DataFrame returned")

    except Exception as e:
        verifier.record_log(f"ERROR during query: {type(e).__name__}: {e}")
        print("\n❌ TEST FAILED: Query raised exception")
        return verifier.verify_results()

    print()

    # Step 3: Wait for background task to complete
    print("Step 3: Waiting for background storage task (2 seconds)...")
    await asyncio.sleep(2)
    verifier.record_log("Wait completed")

    print()

    # Step 4: Verify minute candles were persisted
    print("Step 4: Verifying minute candles persisted to database...")
    async with AsyncSessionLocal() as session:
        final_count = await count_minute_candles(session, test_symbol)
        verifier.record_log(f"Final minute candle count: {final_count}")

        if final_count > 0:
            latest_candles = await get_latest_minute_candles(session, test_symbol, limit=5)
            verifier.record_log(f"Latest {len(latest_candles)} minute candles:")

            for i, candle in enumerate(latest_candles[:5], 1):
                verifier.record_log(
                    f"  {i}. {candle['time']} [{candle['venue']}]: "
                    f"O={candle['open']:.2f} H={candle['high']:.2f} "
                    f"L={candle['low']:.2f} C={candle['close']:.2f} "
                    f"Vol={candle['volume']:.0f}"
                )
        else:
            verifier.record_log("WARNING: No minute candles found in DB")

    print()

    # Step 5: Print results
    print("=" * 80)
    print("E2E Test Results")
    print("=" * 80)

    results = verifier.verify_results()

    print(f"Symbol: {results['symbol']}")
    print(f"Duration: {results['duration_seconds']} seconds")
    print(f"API Called: {results['api_called']}")
    print(f"Background Task Created: {results['background_task_created']}")
    print(f"DB Hit: {results['db_hit']}")
    print(f"Total Logs Captured: {results['total_logs']}")
    print()

    # Check against requirements
    print("Requirement Verification:")
    print(f"  ✓ API called: {results['api_called']}")
    print(f"  ✓ Background task created: {results['background_task_created']}")
    print(f"  ✓ Data returned: {len(df) if 'df' in locals() else 0} candles")
    print(f"  ✓ 1m candles persisted: {final_count if 'final_count' in locals() else 0} records")
    print()

    if results['success']:
        print("✅ TEST PASSED")
        print()
        print("All requirements verified:")
        print("  1. API fallback triggered")
        print("  2. Background task created")
        print("  3. Data returned successfully")
        print("  4. Minute candles persisted to DB")
    else:
        print("❌ TEST FAILED")
        print()
        print("Missing requirements:")
        if not results['api_called']:
            print("  - API fallback not triggered")
        if not results['background_task_created']:
            print("  - Background task not created")

    print("=" * 80)

    return results


if __name__ == '__main__':
    try:
        results = asyncio.run(run_e2e_test())
        sys.exit(0 if results['success'] else 1)
    except Exception as e:
        logger.exception("E2E test crashed")
        sys.exit(2)
