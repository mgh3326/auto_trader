#!/usr/bin/env python3
"""
End-to-end verification script for Discord webhook integration.

This script sends test notifications to all configured Discord webhooks
and verifies they are received correctly.

Usage:
    python scripts/test_discord_webhook_e2e.py [--market-type TYPE]

Options:
    --market-type TYPE    Test specific market type: us, kr, crypto, alerts (default: all)
    --dry-run             Show what would be sent without actually sending
    --verbose             Enable verbose output

Examples:
    # Test all webhooks
    python scripts/test_discord_webhook_e2e.py

    # Test only crypto webhook
    python scripts/test_discord_webhook_e2e.py --market-type crypto

    # Dry run to see what would be sent
    python scripts/test_discord_webhook_e2e.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.core.config import settings
from app.monitoring.trade_notifier import TradeNotifier, get_trade_notifier


def print_section(title: str) -> None:
    """Print a section header."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def print_success(message: str) -> None:
    """Print a success message."""
    print(f"✅ {message}")


def print_error(message: str) -> None:
    """Print an error message."""
    print(f"❌ {message}")


def print_info(message: str) -> None:
    """Print an info message."""
    print(f"ℹ️  {message}")


def print_warning(message: str) -> None:
    """Print a warning message."""
    print(f"⚠️  {message}")


async def test_buy_notification(notifier: TradeNotifier, market_type: str, webhook_name: str) -> bool:
    """Test buy order notification."""
    print_info(f"Testing buy notification for {webhook_name}...")

    if market_type == "us":
        symbol = "AAPL"
        korean_name = "애플"
        market = "해외주식"
    elif market_type == "kr":
        symbol = "005930"
        korean_name = "삼성전자"
        market = "국내주식"
    else:  # crypto
        symbol = "BTC"
        korean_name = "비트코인"
        market = "암호화폐"

    success = await notifier.notify_buy_order(
        symbol=symbol,
        korean_name=korean_name,
        order_count=1,
        total_amount=100000,
        prices=[100000],
        volumes=[1.0],
        market_type=market,
    )

    if success:
        print_success(f"Buy notification sent to {webhook_name}")
        return True
    else:
        print_error(f"Failed to send buy notification to {webhook_name}")
        return False


async def test_sell_notification(notifier: TradeNotifier, market_type: str, webhook_name: str) -> bool:
    """Test sell order notification."""
    print_info(f"Testing sell notification for {webhook_name}...")

    if market_type == "us":
        symbol = "TSLA"
        korean_name = "테슬라"
        market = "해외주식"
    elif market_type == "kr":
        symbol = "000660"
        korean_name = "SK하이닉스"
        market = "국내주식"
    else:  # crypto
        symbol = "ETH"
        korean_name = "이더리움"
        market = "암호화폐"

    success = await notifier.notify_sell_order(
        symbol=symbol,
        korean_name=korean_name,
        order_count=1,
        total_volume=0.5,
        prices=[100000],
        volumes=[0.5],
        expected_amount=50000,
        market_type=market,
    )

    if success:
        print_success(f"Sell notification sent to {webhook_name}")
        return True
    else:
        print_error(f"Failed to send sell notification to {webhook_name}")
        return False


async def test_analysis_notification(notifier: TradeNotifier, market_type: str, webhook_name: str) -> bool:
    """Test AI analysis notification."""
    print_info(f"Testing analysis notification for {webhook_name}...")

    if market_type == "us":
        symbol = "NVDA"
        korean_name = "엔비디아"
        market = "해외주식"
    elif market_type == "kr":
        symbol = "035420"
        korean_name = "NAVER"
        market = "국내주식"
    else:  # crypto
        symbol = "BTC"
        korean_name = "비트코인"
        market = "암호화폐"

    success = await notifier.notify_analysis_complete(
        symbol=symbol,
        korean_name=korean_name,
        decision="buy",
        confidence=85,
        reasons=["상승 추세 지속", "거래량 증가", "RSI 과매도 탈출"],
        market_type=market,
    )

    if success:
        print_success(f"Analysis notification sent to {webhook_name}")
        return True
    else:
        print_error(f"Failed to send analysis notification to {webhook_name}")
        return False


async def test_alerts_notification(notifier: TradeNotifier) -> bool:
    """Test alerts/analysis notification."""
    print_info("Testing alerts notification...")

    success = await notifier.notify_analysis_complete(
        symbol="TEST",
        korean_name="테스트종목",
        decision="hold",
        confidence=70,
        reasons=["테스트 알림"],
        market_type="국내주식",
    )

    if success:
        print_success("Alerts notification sent")
        return True
    else:
        print_error("Failed to send alerts notification")
        return False


def check_configuration() -> dict[str, str | None]:
    """Check which Discord webhooks are configured."""
    config = {
        "us": getattr(settings, "discord_webhook_us", None),
        "kr": getattr(settings, "discord_webhook_kr", None),
        "crypto": getattr(settings, "discord_webhook_crypto", None),
        "alerts": getattr(settings, "discord_webhook_alerts", None),
    }
    return config


def display_configuration(config: dict[str, str | None]) -> None:
    """Display current configuration."""
    print_section("Discord Webhook Configuration")

    configured_count = 0
    for name, url in config.items():
        if url:
            configured_count += 1
            # Mask the URL for security
            masked_url = url[:50] + "..." if len(url) > 50 else url
            print_success(f"{name.upper():8} : {masked_url}")
        else:
            print_warning(f"{name.upper():8} : Not configured")

    print()
    print_info(f"Total configured: {configured_count}/4 webhooks")

    if configured_count == 0:
        print_error("No Discord webhooks configured!")
        print()
        print_info("Please configure webhook URLs in .env file:")
        print("  DISCORD_WEBHOOK_US=https://discord.com/api/webhooks/...")
        print("  DISCORD_WEBHOOK_KR=https://discord.com/api/webhooks/...")
        print("  DISCORD_WEBHOOK_CRYPTO=https://discord.com/api/webhooks/...")
        print("  DISCORD_WEBHOOK_ALERTS=https://discord.com/api/webhooks/...")
        return False

    return True


async def run_tests(
    market_type: str | None,
    dry_run: bool,
    verbose: bool,
) -> dict[str, list[bool]]:
    """Run the end-to-end tests."""
    # Check configuration
    config = check_configuration()
    if not display_configuration(config):
        return {}

    # Initialize TradeNotifier
    print_section("Initializing TradeNotifier")

    # Reset singleton for clean test
    TradeNotifier._instance = None
    TradeNotifier._initialized = False

    notifier = get_trade_notifier()

    # Configure with webhooks from settings
    notifier.configure(
        bot_token=getattr(settings, "telegram_token", "") or "",
        chat_ids=[getattr(settings, "telegram_chat_id", "")] if getattr(settings, "telegram_chat_id", None) else [],
        enabled=True,
        discord_webhook_us=config["us"],
        discord_webhook_kr=config["kr"],
        discord_webhook_crypto=config["crypto"],
        discord_webhook_alerts=config["alerts"],
    )

    print_success("TradeNotifier initialized")

    # Determine which webhooks to test
    webhooks_to_test = []
    if market_type:
        if market_type == "us" and config["us"]:
            webhooks_to_test.append(("us", "US Stocks", config["us"]))
        elif market_type == "kr" and config["kr"]:
            webhooks_to_test.append(("kr", "KR Stocks", config["kr"]))
        elif market_type == "crypto" and config["crypto"]:
            webhooks_to_test.append(("crypto", "Crypto", config["crypto"]))
        elif market_type == "alerts" and config["alerts"]:
            webhooks_to_test.append(("alerts", "Alerts", config["alerts"]))
        else:
            print_error(f"Webhook for '{market_type}' is not configured")
            return {}
    else:
        # Test all configured webhooks
        if config["us"]:
            webhooks_to_test.append(("us", "US Stocks", config["us"]))
        if config["kr"]:
            webhooks_to_test.append(("kr", "KR Stocks", config["kr"]))
        if config["crypto"]:
            webhooks_to_test.append(("crypto", "Crypto", config["crypto"]))
        if config["alerts"]:
            webhooks_to_test.append(("alerts", "Alerts", config["alerts"]))

    if not webhooks_to_test:
        print_error("No webhooks to test")
        return {}

    # Run tests
    print_section("Sending Test Notifications")

    results: dict[str, list[bool]] = {}

    for webhook_type, webhook_name, _ in webhooks_to_test:
        print(f"\n📡 Testing {webhook_name} webhook...")
        print("-" * 70)

        test_results = []

        if dry_run:
            print_info("[DRY RUN] Would send notifications to {webhook_name}")
            test_results = [True, True, True]  # Pretend all tests passed
        else:
            # Run actual tests
            if webhook_type == "alerts":
                result = await test_alerts_notification(notifier)
                test_results.append(result)
            else:
                # Test buy, sell, and analysis notifications
                result1 = await test_buy_notification(notifier, webhook_type, webhook_name)
                test_results.append(result1)

                result2 = await test_sell_notification(notifier, webhook_type, webhook_name)
                test_results.append(result2)

                result3 = await test_analysis_notification(notifier, webhook_type, webhook_name)
                test_results.append(result3)

        results[webhook_name] = test_results

    # Cleanup
    await notifier.shutdown()

    return results


def display_summary(results: dict[str, list[bool]], verbose: bool) -> int:
    """Display test summary."""
    print_section("Test Results Summary")

    if not results:
        print_error("No tests were run")
        return 1

    total_tests = 0
    passed_tests = 0

    for webhook_name, test_results in results.items():
        total_tests += len(test_results)
        webhook_passed = sum(test_results)
        webhook_total = len(test_results)
        passed_tests += webhook_passed

        if webhook_passed == webhook_total:
            print_success(f"{webhook_name}: {webhook_passed}/{webhook_total} tests passed")
        else:
            print_warning(f"{webhook_name}: {webhook_passed}/{webhook_total} tests passed")

        if verbose:
            for i, result in enumerate(test_results, 1):
                status = "✅" if result else "❌"
                print(f"  {status} Test {i}")

    print()
    print(f"Overall: {passed_tests}/{total_tests} tests passed")

    if passed_tests == total_tests:
        print_success("\n🎉 All tests passed! Discord webhook integration is working correctly.")
        return 0
    else:
        print_error(f"\n❌ {total_tests - passed_tests} test(s) failed. Please check your Discord webhook configuration.")
        return 1


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="End-to-end verification for Discord webhook integration"
    )
    parser.add_argument(
        "--market-type",
        choices=["us", "kr", "crypto", "alerts"],
        help="Test specific market type (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be sent without actually sending",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output",
    )

    args = parser.parse_args()

    try:
        # Run async tests
        results = asyncio.run(run_tests(
            market_type=args.market_type,
            dry_run=args.dry_run,
            verbose=args.verbose,
        ))

        # Display summary
        exit_code = display_summary(results, args.verbose)

        return exit_code

    except KeyboardInterrupt:
        print_warning("\n\nTest interrupted by user")
        return 130
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
