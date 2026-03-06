#!/usr/bin/env python3
"""
Quick cache warm-up test - Simple version for fast verification

This is a simplified version of the cache warm-up test that focuses on
the core verification: second query should hit DB, not API.
"""

import asyncio
import time
from datetime import datetime
from app.services.kr_hourly_candles_read_service import read_kr_hourly_candles_1h


async def quick_test():
    """Quick test: Call twice and compare."""
    symbol = "005930"
    count = 5

    print("Quick Cache Warm-up Test")
    print("=" * 50)

    # First call
    print("\n1️⃣  First call (cold)...")
    start = time.time()
    df1 = await read_kr_hourly_candles_1h(
        symbol=symbol,
        count=count,
        end_date=None,
        now_kst=datetime.now()
    )
    t1 = time.time() - start
    print(f"   ✓ Returned {len(df1)} candles in {t1:.3f}s")

    # Wait for background storage
    print("\n⏳ Waiting 3s for background storage...")
    await asyncio.sleep(3)

    # Second call
    print("\n2️⃣  Second call (warm)...")
    start = time.time()
    df2 = await read_kr_hourly_candles_1h(
        symbol=symbol,
        count=count,
        end_date=None,
        now_kst=datetime.now()
    )
    t2 = time.time() - start
    print(f"   ✓ Returned {len(df2)} candles in {t2:.3f}s")

    # Results
    print("\n" + "=" * 50)
    print("Results:")
    print(f"  First call:  {t1:.3f}s (includes API)")
    print(f"  Second call: {t2:.3f}s (DB only)")
    print(f"  Speedup:     {t1/t2:.1f}x")
    print("=" * 50)

    # Check logs manually for:
    # - First call: should see "Fallback to KIS API"
    # - Second call: should NOT see "Fallback to KIS API"


if __name__ == "__main__":
    asyncio.run(quick_test())
