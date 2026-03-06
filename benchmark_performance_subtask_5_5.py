#!/usr/bin/env python3
"""
Performance Benchmark Script for KR Hourly Candles Read Service
Subtask 5-5: Verify cold query vs warm query latency

This script measures and compares:
- Cold query: First call when DB is empty (requires API fetch)
- Warm query: Second call when data is cached in DB
- Speedup factor: How much faster warm queries are

Performance Targets:
- Cold query: < 3 seconds (includes API latency)
- Warm query: < 100ms (DB hit only)
- Speedup: ~30x faster on warm queries
"""

import asyncio
import time
import statistics
from typing import Dict, List
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.kr_hourly_candles_read_service import read_kr_hourly_candles_1h
from sqlalchemy import text
from app.core.db import AsyncSessionLocal
import logging

# Configure logging to capture performance details
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ANSI color codes for terminal output
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def print_header(text: str):
    """Print a formatted header."""
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'=' * 80}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{text.center(80)}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{'=' * 80}{Colors.ENDC}\n")


def print_section(text: str):
    """Print a section header."""
    print(f"\n{Colors.OKBLUE}{Colors.BOLD}{text}{Colors.ENDC}")
    print(f"{Colors.OKBLUE}{'-' * len(text)}{Colors.ENDC}\n")


def print_success(text: str):
    """Print success message."""
    print(f"{Colors.OKGREEN}✓ {text}{Colors.ENDC}")


def print_error(text: str):
    """Print error message."""
    print(f"{Colors.FAIL}✗ {text}{Colors.ENDC}")


def print_warning(text: str):
    """Print warning message."""
    print(f"{Colors.WARNING}⚠ {text}{Colors.ENDC}")


def format_time(ms: float) -> str:
    """Format time in milliseconds with appropriate unit."""
    if ms < 1:
        return f"{ms * 1000:.3f}μs"
    elif ms < 1000:
        return f"{ms:.2f}ms"
    else:
        return f"{ms / 1000:.2f}s"


def format_speedup(factor: float) -> str:
    """Format speedup factor."""
    return f"{factor:.1f}x"


class PerformanceBenchmark:
    """Performance benchmark for KR hourly candles read service."""

    def __init__(self, symbol: str = "005930", count: int = 5, runs: int = 3):
        """
        Initialize benchmark.

        Args:
            symbol: Stock symbol to query (default: 005930 - Samsung Electronics)
            count: Number of hourly candles to request
            runs: Number of benchmark runs for statistical significance
        """
        self.symbol = symbol
        self.count = count
        self.runs = runs
        self.cold_times: List[float] = []
        self.warm_times: List[float] = []

        # Performance targets
        self.TARGET_COLD_MAX_MS = 3000  # 3 seconds
        self.TARGET_WARM_MAX_MS = 100   # 100ms
        self.TARGET_SPEEDUP_MIN = 20    # At least 20x speedup (spec says ~30x)

    async def clear_db_cache(self):
        """Clear all cached data for the test symbol from the database."""
        print_section("Clearing Database Cache")

        try:
            async with AsyncSessionLocal() as session:
                # Delete from kr_candles_1m table
                await session.execute(
                    text("DELETE FROM public.kr_candles_1m WHERE symbol = :symbol"),
                    {"symbol": self.symbol}
                )
                await session.commit()
                print_success(f"Cleared kr_candles_1m for symbol {self.symbol}")

                # Note: kr_candles_1h is a continuous aggregate, it will auto-refresh
                print_success("Database cache cleared successfully")

        except Exception as e:
            print_error(f"Failed to clear database cache: {e}")
            raise

    async def measure_cold_query(self) -> float:
        """
        Measure cold query performance (DB empty, requires API fetch).

        Returns:
            Query time in milliseconds
        """
        import datetime

        # Ensure we're querying for a time that requires API fetch
        now_kst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))

        start_time = time.perf_counter()
        try:
            result = await read_kr_hourly_candles_1h(
                symbol=self.symbol,
                count=self.count,
                end_date=None,
                now_kst=now_kst
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000

            if len(result) > 0:
                print_success(f"  Cold query returned {len(result)} candles")
            else:
                print_warning(f"  Cold query returned 0 candles (API may have failed)")

            return elapsed_ms
        except Exception as e:
            print_error(f"  Cold query failed: {e}")
            raise

    async def measure_warm_query(self) -> float:
        """
        Measure warm query performance (data cached in DB).

        Returns:
            Query time in milliseconds
        """
        import datetime

        # Query for same time/params - should hit DB cache
        now_kst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))

        start_time = time.perf_counter()
        try:
            result = await read_kr_hourly_candles_1h(
                symbol=self.symbol,
                count=self.count,
                end_date=None,
                now_kst=now_kst
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000

            if len(result) > 0:
                print_success(f"  Warm query returned {len(result)} candles")
            else:
                print_warning(f"  Warm query returned 0 candles")

            return elapsed_ms
        except Exception as e:
            print_error(f"  Warm query failed: {e}")
            raise

    async def run_single_benchmark(self, run_num: int) -> Dict[str, float]:
        """
        Run a single benchmark iteration (cold + warm).

        Args:
            run_num: Run number for display purposes

        Returns:
            Dictionary with cold_ms, warm_ms, speedup
        """
        print(f"\n{Colors.OKCYAN}Benchmark Run {run_num + 1}/{self.runs}{Colors.ENDC}")

        # Clear cache before cold query
        await self.clear_db_cache()

        # Small delay to ensure DB transaction is committed
        await asyncio.sleep(0.5)

        # Measure cold query
        print(f"  Measuring cold query (DB empty, API fetch required)...")
        cold_ms = await self.measure_cold_query()
        print(f"    → Cold query time: {format_time(cold_ms)}")

        # Small delay to ensure background task completes and data is persisted
        print(f"  Waiting for background storage to complete...")
        await asyncio.sleep(2.0)

        # Measure warm query
        print(f"  Measuring warm query (data cached in DB)...")
        warm_ms = await self.measure_warm_query()
        print(f"    → Warm query time: {format_time(warm_ms)}")

        # Calculate speedup
        speedup = cold_ms / warm_ms if warm_ms > 0 else float('inf')
        print(f"    → Speedup: {format_speedup(speedup)}")

        return {
            'cold_ms': cold_ms,
            'warm_ms': warm_ms,
            'speedup': speedup
        }

    async def run_benchmark_suite(self) -> Dict:
        """
        Run complete benchmark suite with multiple iterations.

        Returns:
            Dictionary with benchmark results and statistics
        """
        print_header("KR Hourly Candles Performance Benchmark")
        print(f"Symbol: {self.symbol}")
        print(f"Count: {self.count} candles")
        print(f"Runs: {self.runs} iterations")
        print(f"\nPerformance Targets:")
        print(f"  • Cold query: < {format_time(self.TARGET_COLD_MAX_MS)} (includes API)")
        print(f"  • Warm query: < {format_time(self.TARGET_WARM_MAX_MS)} (DB hit only)")
        print(f"  • Speedup: > {format_speedup(self.TARGET_SPEEDUP_MIN)}")

        results = []

        # Run benchmark iterations
        for i in range(self.runs):
            try:
                result = await self.run_single_benchmark(i)
                results.append(result)
                self.cold_times.append(result['cold_ms'])
                self.warm_times.append(result['warm_ms'])
            except Exception as e:
                print_error(f"Benchmark run {i + 1} failed: {e}")
                print_warning("Continuing with remaining runs...")
                continue

        # Calculate statistics
        if not results:
            print_error("No successful benchmark runs!")
            return {'success': False}

        avg_cold = statistics.mean(self.cold_times)
        avg_warm = statistics.mean(self.warm_times)
        avg_speedup = avg_cold / avg_warm if avg_warm > 0 else float('inf')

        if len(self.cold_times) > 1:
            std_cold = statistics.stdev(self.cold_times)
            std_warm = statistics.stdev(self.warm_times)
        else:
            std_cold = 0
            std_warm = 0

        # Determine pass/fail for each metric
        cold_pass = avg_cold < self.TARGET_COLD_MAX_MS
        warm_pass = avg_warm < self.TARGET_WARM_MAX_MS
        speedup_pass = avg_speedup > self.TARGET_SPEEDUP_MIN

        all_pass = cold_pass and warm_pass and speedup_pass

        return {
            'success': True,
            'avg_cold_ms': avg_cold,
            'avg_warm_ms': avg_warm,
            'avg_speedup': avg_speedup,
            'std_cold_ms': std_cold,
            'std_warm_ms': std_warm,
            'cold_pass': cold_pass,
            'warm_pass': warm_pass,
            'speedup_pass': speedup_pass,
            'all_pass': all_pass,
            'runs': len(results)
        }

    def print_results(self, results: Dict):
        """Print benchmark results with pass/fail indicators."""
        print_section("Benchmark Results")

        if not results.get('success'):
            print_error("Benchmark failed to complete")
            return

        print(f"Successful runs: {results['runs']}/{self.runs}\n")

        # Cold query results
        print(f"Cold Query (DB empty, API fetch):")
        print(f"  Average: {format_time(results['avg_cold_ms'])} "
              f"(±{format_time(results['std_cold_ms'])})")
        if results['cold_pass']:
            print_success(f"Target met (< {format_time(self.TARGET_COLD_MAX_MS)})")
        else:
            print_error(f"Target missed (> {format_time(self.TARGET_COLD_MAX_MS)})")

        # Warm query results
        print(f"\nWarm Query (data cached in DB):")
        print(f"  Average: {format_time(results['avg_warm_ms'])} "
              f"(±{format_time(results['std_warm_ms'])})")
        if results['warm_pass']:
            print_success(f"Target met (< {format_time(self.TARGET_WARM_MAX_MS)})")
        else:
            print_error(f"Target missed (> {format_time(self.TARGET_WARM_MAX_MS)})")

        # Speedup results
        print(f"\nSpeedup Factor:")
        print(f"  Average: {format_speedup(results['avg_speedup'])}")
        if results['speedup_pass']:
            print_success(f"Target met (> {format_speedup(self.TARGET_SPEEDUP_MIN)})")
        else:
            print_error(f"Target missed (< {format_speedup(self.TARGET_SPEEDUP_MIN)})")

        # Overall result
        print_section("Overall Result")
        if results['all_pass']:
            print_success(f"{Colors.BOLD}ALL PERFORMANCE TARGETS MET{Colors.ENDC}")
            print(f"\nThe KR hourly candles read service meets all performance requirements:")
            print(f"  • Cold queries complete within {format_time(self.TARGET_COLD_MAX_MS)}")
            print(f"  • Warm queries complete within {format_time(self.TARGET_WARM_MAX_MS)}")
            print(f"  • Cache provides >{format_speedup(self.TARGET_SPEEDUP_MIN)} speedup")
            return 0
        else:
            print_error(f"{Colors.BOLD}SOME PERFORMANCE TARGETS NOT MET{Colors.ENDC}")
            print(f"\nPlease review the results above and consider:")
            print(f"  • Check KIS API latency (affects cold query time)")
            print(f"  • Check database query performance (affects warm query time)")
            print(f"  • Review network connectivity")
            return 1


async def main():
    """Main entry point for performance benchmark."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Performance benchmark for KR hourly candles read service"
    )
    parser.add_argument(
        'symbol',
        nargs='?',
        default='005930',
        help='Stock symbol to query (default: 005930 - Samsung Electronics)'
    )
    parser.add_argument(
        'count',
        nargs='?',
        type=int,
        default=5,
        help='Number of hourly candles to request (default: 5)'
    )
    parser.add_argument(
        '--runs',
        type=int,
        default=3,
        help='Number of benchmark runs for statistical significance (default: 3)'
    )

    args = parser.parse_args()

    # Create and run benchmark
    benchmark = PerformanceBenchmark(
        symbol=args.symbol,
        count=args.count,
        runs=args.runs
    )

    try:
        results = await benchmark.run_benchmark_suite()
        exit_code = benchmark.print_results(results)
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print_warning("\nBenchmark interrupted by user")
        sys.exit(130)
    except Exception as e:
        print_error(f"Benchmark failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
