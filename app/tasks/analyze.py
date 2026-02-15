import asyncio

from app.analysis.service_analyzers import KISAnalyzer, UpbitAnalyzer, YahooAnalyzer
from app.monitoring.trade_notifier import get_trade_notifier
from app.services import upbit
from app.services.order_service import (
    cancel_existing_buy_orders,
    cancel_existing_sell_orders,
    get_sell_prices_for_coin,
    place_multiple_sell_orders,
)
from data.coins_info import upbit_pairs


async def _fetch_tradable_coins() -> tuple[list[dict], list[dict]]:
    """보유 중인 코인과 거래 가능한 코인을 동시에 조회."""
    await upbit_pairs.prime_upbit_constants()

    my_coins = await upbit.fetch_my_coins()

    analyzer = UpbitAnalyzer()
    try:
        tradable_coins = [
            coin
            for coin in my_coins
            if coin.get("currency") != "KRW"
            and analyzer.is_tradable(coin)
            and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS
        ]
    finally:
        await analyzer.close()

    return my_coins, tradable_coins


async def _analyze_coin_async(currency: str) -> dict[str, object]:
    """단일 코인 분석을 수행하는 비동기 헬퍼."""
    if not currency:
        return {"status": "failed", "error": "코인 코드가 필요합니다."}

    await upbit_pairs.prime_upbit_constants()
    currency_code = currency.upper()

    if currency_code not in upbit_pairs.KRW_TRADABLE_COINS:
        return {
            "status": "failed",
            "currency": currency_code,
            "message": f"{currency_code}는 KRW 마켓 거래 대상이 아닙니다.",
        }

    korean_name = upbit_pairs.COIN_TO_NAME_KR.get(currency_code, currency_code)

    analyzer = UpbitAnalyzer()
    try:
        result, model = await analyzer.analyze_coin_json(korean_name)

        # Check if analysis failed (result is None)
        if result is None:
            return {
                "status": "failed",
                "currency": currency_code,
                "korean_name": korean_name,
                "error": "분석 결과를 가져올 수 없습니다.",
            }

        # Send Telegram notification if analysis completed successfully
        if hasattr(result, "decision"):
            try:
                notifier = get_trade_notifier()
                await notifier.notify_analysis_complete(
                    symbol=currency_code,
                    korean_name=korean_name,
                    decision=result.decision,
                    confidence=float(result.confidence) if result.confidence else 0.0,
                    reasons=result.reasons
                    if hasattr(result, "reasons") and result.reasons
                    else [],
                    market_type="암호화폐",
                )
            except Exception as notify_error:  # pragma: no cover
                print(f"⚠️ 텔레그램 알림 전송 실패: {notify_error}")

        return {
            "status": "completed",
            "currency": currency_code,
            "korean_name": korean_name,
            "message": f"{korean_name} 분석이 완료되었습니다.",
        }
    except Exception as exc:  # pragma: no cover - defensive logging
        return {
            "status": "failed",
            "currency": currency_code,
            "korean_name": korean_name,
            "error": str(exc),
        }
    finally:
        await analyzer.close()


async def _execute_buy_order_for_coin_async(currency: str) -> dict[str, object]:
    """단일 코인 분할 매수 실행 헬퍼."""
    if not currency:
        return {"status": "failed", "error": "코인 코드가 필요합니다."}

    from app.services.stock_info_service import process_buy_orders_with_analysis

    currency_code = currency.upper()

    await upbit_pairs.prime_upbit_constants()

    if currency_code not in upbit_pairs.KRW_TRADABLE_COINS:
        return {
            "status": "failed",
            "currency": currency_code,
            "message": f"{currency_code}는 KRW 마켓에서 거래할 수 없습니다.",
        }

    market = f"KRW-{currency_code}"

    try:
        my_coins = await upbit.fetch_my_coins()
        target_coin = next(
            (coin for coin in my_coins if coin.get("currency") == currency_code), None
        )

        if not target_coin:
            return {
                "status": "failed",
                "currency": currency_code,
                "message": f"{currency_code} 보유 내역을 찾을 수 없습니다.",
            }

        avg_buy_price = float(target_coin.get("avg_buy_price", 0))

        current_price_df = await upbit.fetch_price(market)
        current_price = float(current_price_df.iloc[0]["close"])

        await cancel_existing_buy_orders(market)
        await asyncio.sleep(1)

        result = await process_buy_orders_with_analysis(
            market, current_price, avg_buy_price
        )
        message = result.get("message") or ""
        failure_reasons = result.get("failure_reasons") or []
        combined_reason = " ".join([message, *failure_reasons]).lower()
        insufficient_keywords = [
            "krw 잔고 부족",
            "잔고 부족",
            "금액 부족",
            "주문 가능 금액 부족",
            "insufficient balance",
            "insufficient funds",
        ]
        has_insufficient_balance = bool(
            result.get("insufficient_balance")
            or any(keyword in combined_reason for keyword in insufficient_keywords)
        )

        # Send Telegram notification if orders were placed
        if result.get("success") and result.get("orders_placed", 0) > 0:
            try:
                notifier = get_trade_notifier()
                korean_name = upbit_pairs.COIN_TO_NAME_KR.get(
                    currency_code, currency_code
                )

                # Extract order details from result if available
                orders_placed = result.get("orders_placed", 0)
                total_amount = result.get("total_amount", 0.0)

                # Note: We don't have individual prices/volumes from process_buy_orders_with_analysis
                # For now, send summary notification
                await notifier.notify_buy_order(
                    symbol=currency_code,
                    korean_name=korean_name,
                    order_count=orders_placed,
                    total_amount=total_amount,
                    prices=[],  # Individual prices not available from result
                    volumes=[],  # Individual volumes not available from result
                    market_type="암호화폐",
                )
            except Exception as notify_error:  # pragma: no cover
                print(f"⚠️ 텔레그램 알림 전송 실패: {notify_error}")

        # Send failure notification for insufficient balance
        elif not result.get("success") and has_insufficient_balance:
            try:
                notifier = get_trade_notifier()
                korean_name = upbit_pairs.COIN_TO_NAME_KR.get(
                    currency_code, currency_code
                )
                reason = message or (
                    failure_reasons[0] if failure_reasons else "잔고 부족으로 매수 실패"
                )

                await notifier.notify_trade_failure(
                    symbol=currency_code,
                    korean_name=korean_name,
                    reason=reason,
                    market_type="암호화폐",
                )
            except Exception as notify_error:  # pragma: no cover
                print(f"⚠️ 텔레그램 알림 전송 실패: {notify_error}")

        return {
            "status": "completed" if result.get("success") else "failed",
            "currency": currency_code,
            "message": result.get("message"),
            "result": result,
        }
    except Exception as exc:  # pragma: no cover - defensive logging
        return {
            "status": "failed",
            "currency": currency_code,
            "error": str(exc),
        }


async def _execute_sell_order_for_coin_async(currency: str) -> dict[str, object]:
    """단일 코인 분할 매도 실행 헬퍼."""
    if not currency:
        return {"status": "failed", "error": "코인 코드가 필요합니다."}

    currency_code = currency.upper()

    await upbit_pairs.prime_upbit_constants()

    if currency_code not in upbit_pairs.KRW_TRADABLE_COINS:
        return {
            "status": "failed",
            "currency": currency_code,
            "message": f"{currency_code}는 KRW 마켓에서 거래할 수 없습니다.",
        }

    market = f"KRW-{currency_code}"

    try:
        my_coins = await upbit.fetch_my_coins()
        target_coin = next(
            (coin for coin in my_coins if coin.get("currency") == currency_code), None
        )

        if not target_coin:
            return {
                "status": "failed",
                "currency": currency_code,
                "message": f"{currency_code} 보유 내역을 찾을 수 없습니다.",
            }

        balance = float(target_coin.get("balance", 0))
        avg_buy_price = float(target_coin.get("avg_buy_price", 0))

        await cancel_existing_sell_orders(market)
        await asyncio.sleep(1)

        refreshed = await upbit.fetch_my_coins()
        for coin in refreshed:
            if coin.get("currency") == currency_code:
                balance = float(coin.get("balance", 0))
                break

        if balance < 0.00000001:
            return {
                "status": "failed",
                "currency": currency_code,
                "message": "보유 수량이 너무 적습니다.",
            }

        current_price_df = await upbit.fetch_price(market)
        current_price = float(current_price_df.iloc[0]["close"])

        sell_prices = await get_sell_prices_for_coin(
            currency_code, avg_buy_price, current_price
        )
        if not sell_prices:
            return {
                "status": "failed",
                "currency": currency_code,
                "message": "매도 조건에 맞는 가격이 없습니다.",
            }

        def format_price(value: float) -> str:
            return f"{value:,.0f}"

        print(f"📊 {market} 분석 기반 분할 매도 주문 처리")
        print(f"현재가: {format_price(current_price)}원")
        print(f"평균 매수가: {format_price(avg_buy_price)}원")
        print(f"보유 수량: {balance:.8f}")
        prices_preview = ", ".join(f"{format_price(price)}원" for price in sell_prices)
        print(f"매도 후보 가격: {prices_preview}")

        result = await place_multiple_sell_orders(
            market, balance, sell_prices, currency_code
        )

        if result.get("success"):
            print(f"📈 매도 주문 완료: {result.get('orders_placed', 0)}건 성공")

            # Send Telegram notification if orders were placed
            if result.get("orders_placed", 0) > 0:
                try:
                    notifier = get_trade_notifier()
                    korean_name = upbit_pairs.COIN_TO_NAME_KR.get(
                        currency_code, currency_code
                    )

                    orders_placed = result.get("orders_placed", 0)
                    # Estimate expected amount from sell_prices and balance
                    expected_amount = (
                        sum(sell_prices) * balance / len(sell_prices)
                        if sell_prices
                        else 0
                    )

                    await notifier.notify_sell_order(
                        symbol=currency_code,
                        korean_name=korean_name,
                        order_count=orders_placed,
                        total_volume=balance,
                        prices=sell_prices,  # Use the sell_prices we calculated
                        volumes=[],  # Don't have exact volumes per order
                        expected_amount=expected_amount,
                        market_type="암호화폐",
                    )
                except Exception as notify_error:  # pragma: no cover
                    print(f"⚠️ 텔레그램 알림 전송 실패: {notify_error}")
        else:
            print(f"⚠️ 매도 주문 실패: {result.get('message')}")

        return {
            "status": "completed" if result.get("success") else "failed",
            "currency": currency_code,
            "message": result.get("message"),
            "result": result,
        }
    except Exception as exc:  # pragma: no cover - defensive logging
        return {
            "status": "failed",
            "currency": currency_code,
            "error": str(exc),
        }


async def run_analysis_for_stock(symbol: str, name: str, instrument_type: str) -> dict:
    analyzer = None
    try:
        if instrument_type == "equity_kr":
            analyzer = KISAnalyzer()
            result, _ = await analyzer.analyze_stock_json(name)
        elif instrument_type == "equity_us":
            analyzer = YahooAnalyzer()
            result, _ = await analyzer.analyze_stock_json(symbol)
        elif instrument_type == "crypto":
            analyzer = UpbitAnalyzer()
            result, _ = await analyzer.analyze_coin_json(name)
        else:
            return {
                "status": "ignored",
                "reason": f"unsupported type: {instrument_type}",
            }

        if result is None:
            return {
                "status": "failed",
                "symbol": symbol,
                "name": name,
                "reason": "analysis returned None",
            }

        return {
            "status": "ok",
            "symbol": symbol,
            "name": name,
            "instrument_type": instrument_type,
        }
    finally:
        if analyzer and hasattr(analyzer, "close"):
            await analyzer.close()


async def run_analysis_for_my_coins() -> dict:
    await upbit_pairs.prime_upbit_constants()
    analyzer = UpbitAnalyzer()

    try:
        my_coins = await upbit.fetch_my_coins()
        tradable_coins = [
            coin
            for coin in my_coins
            if coin.get("currency") != "KRW"
            and analyzer.is_tradable(coin)
            and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS
        ]

        if not tradable_coins:
            return {
                "status": "completed",
                "analyzed_count": 0,
                "total_count": 0,
                "message": "거래 가능한 코인이 없습니다.",
                "results": [],
            }

        coin_names = []
        for coin in tradable_coins:
            currency = coin.get("currency")
            korean_name = upbit_pairs.COIN_TO_NAME_KR.get(currency)
            if korean_name:
                coin_names.append(korean_name)

        total_count = len(coin_names)
        results = []

        for coin_name in coin_names:
            try:
                _, model = await analyzer.analyze_coins_json([coin_name])
                results.append(
                    {"coin_name": coin_name, "success": True, "model": model}
                )
            except Exception as exc:
                results.append(
                    {"coin_name": coin_name, "success": False, "error": str(exc)}
                )

        success_count = sum(1 for result in results if result["success"])
        return {
            "status": "completed",
            "analyzed_count": success_count,
            "total_count": total_count,
            "message": f"{success_count}/{total_count}개 코인 분석 완료",
            "results": results,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "error": str(exc),
            "analyzed_count": 0,
            "total_count": 0,
            "results": [],
        }
    finally:
        await analyzer.close()


async def execute_buy_orders_task() -> dict:
    from app.services.stock_info_service import process_buy_orders_with_analysis

    await upbit_pairs.prime_upbit_constants()
    analyzer = UpbitAnalyzer()

    try:
        my_coins = await upbit.fetch_my_coins()
        tradable_coins = [
            coin
            for coin in my_coins
            if coin.get("currency") != "KRW"
            and analyzer.is_tradable(coin)
            and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS
        ]

        if not tradable_coins:
            return {
                "status": "completed",
                "success_count": 0,
                "total_count": 0,
                "message": "거래 가능한 코인이 없습니다.",
                "results": [],
            }

        market_codes = [f"KRW-{coin['currency']}" for coin in tradable_coins]
        current_prices = await upbit.fetch_multiple_current_prices(market_codes)

        for coin in tradable_coins:
            currency = coin["currency"]
            market = f"KRW-{currency}"
            avg_buy_price = float(coin.get("avg_buy_price", 0))

            if avg_buy_price > 0 and market in current_prices:
                current_price = current_prices[market]
                profit_rate = (current_price - avg_buy_price) / avg_buy_price
                coin["profit_rate"] = profit_rate
            else:
                coin["profit_rate"] = float("inf")

        tradable_coins.sort(key=lambda coin: coin.get("profit_rate", float("inf")))

        total_count = len(tradable_coins)
        order_results = []

        for coin in tradable_coins:
            currency = coin["currency"]
            market = f"KRW-{currency}"
            avg_buy_price = float(coin["avg_buy_price"])

            try:
                current_price_df = await upbit.fetch_price(market)
                current_price = float(current_price_df.iloc[0]["close"])

                await cancel_existing_buy_orders(market)
                await asyncio.sleep(1)

                result = await process_buy_orders_with_analysis(
                    market, current_price, avg_buy_price
                )

                if result["success"]:
                    order_results.append(
                        {
                            "currency": currency,
                            "success": True,
                            "message": result["message"],
                            "orders_placed": result.get("orders_placed", 0),
                        }
                    )
                else:
                    order_results.append(
                        {
                            "currency": currency,
                            "success": False,
                            "message": result["message"],
                        }
                    )
            except Exception as exc:
                order_results.append(
                    {"currency": currency, "success": False, "error": str(exc)}
                )

        success_count = sum(1 for result in order_results if result["success"])
        return {
            "status": "completed",
            "success_count": success_count,
            "total_count": total_count,
            "message": f"{success_count}/{total_count}개 코인 매수 주문 완료",
            "results": order_results,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "error": str(exc),
            "success_count": 0,
            "total_count": 0,
            "results": [],
        }
    finally:
        await analyzer.close()


async def execute_sell_orders_task() -> dict:
    await upbit_pairs.prime_upbit_constants()
    analyzer = UpbitAnalyzer()

    try:
        my_coins = await upbit.fetch_my_coins()
        tradable_coins = [
            coin
            for coin in my_coins
            if coin.get("currency") != "KRW"
            and analyzer.is_tradable(coin)
            and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS
        ]

        if not tradable_coins:
            return {
                "status": "completed",
                "success_count": 0,
                "total_count": 0,
                "message": "거래 가능한 코인이 없습니다.",
                "results": [],
            }

        total_count = len(tradable_coins)
        order_results = []

        for coin in tradable_coins:
            currency = coin["currency"]
            market = f"KRW-{currency}"
            balance = float(coin["balance"])
            avg_buy_price = float(coin["avg_buy_price"])

            try:
                await cancel_existing_sell_orders(market)
                await asyncio.sleep(1)

                updated_coins = await upbit.fetch_my_coins()
                balance = 0.0
                for updated_coin in updated_coins:
                    if updated_coin.get("currency") == currency:
                        balance = float(updated_coin["balance"])
                        break

                if balance < 0.00000001:
                    order_results.append(
                        {
                            "currency": currency,
                            "success": False,
                            "message": "보유 수량이 너무 적음",
                        }
                    )
                    continue

                current_price_df = await upbit.fetch_price(market)
                current_price = float(current_price_df.iloc[0]["close"])

                sell_prices = await get_sell_prices_for_coin(
                    currency, avg_buy_price, current_price
                )

                if sell_prices:
                    result = await place_multiple_sell_orders(
                        market, balance, sell_prices, currency
                    )
                    if result["success"]:
                        order_results.append(
                            {
                                "currency": currency,
                                "success": True,
                                "message": result["message"],
                                "orders_placed": result.get("orders_placed", 0),
                            }
                        )
                    else:
                        order_results.append(
                            {
                                "currency": currency,
                                "success": False,
                                "message": result["message"],
                            }
                        )
                else:
                    order_results.append(
                        {
                            "currency": currency,
                            "success": False,
                            "message": "매도 조건에 맞는 가격 없음",
                        }
                    )
            except Exception as exc:
                order_results.append(
                    {"currency": currency, "success": False, "error": str(exc)}
                )

        success_count = sum(1 for result in order_results if result["success"])
        return {
            "status": "completed",
            "success_count": success_count,
            "total_count": total_count,
            "message": f"{success_count}/{total_count}개 코인 매도 주문 완료",
            "results": order_results,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "error": str(exc),
            "success_count": 0,
            "total_count": 0,
            "results": [],
        }
    finally:
        await analyzer.close()


async def execute_buy_order_for_coin_task(currency: str) -> dict:
    """특정 코인에 대한 분할 매수 주문 실행."""

    return await _execute_buy_order_for_coin_async(currency)


async def execute_sell_order_for_coin_task(currency: str) -> dict:
    """특정 코인에 대한 분할 매도 주문 실행."""

    return await _execute_sell_order_for_coin_async(currency)


async def run_analysis_for_coin_task(currency: str) -> dict:
    """단일 코인에 대한 AI 분석 실행."""

    return await _analyze_coin_async(currency)


async def run_per_coin_automation_task() -> dict:
    """보유 코인 각각에 대해 분석 → 분할 매수 → 분할 매도를 순차 실행."""

    _, tradable_coins = await _fetch_tradable_coins()

    if not tradable_coins:
        return {
            "status": "completed",
            "total_coins": 0,
            "success_coins": 0,
            "results": [],
            "message": "거래 가능한 코인이 없습니다.",
        }

    results: list[dict[str, object]] = []

    for coin in tradable_coins:
        currency = (coin.get("currency") or "").upper()
        korean_name = coin.get("korean_name") or upbit_pairs.COIN_TO_NAME_KR.get(
            currency, currency
        )
        coin_summary = {
            "currency": currency,
            "korean_name": korean_name,
            "steps": [],
        }

        step_definitions = [
            ("analysis", lambda c=currency: _analyze_coin_async(c)),
            ("buy", lambda c=currency: _execute_buy_order_for_coin_async(c)),
            ("sell", lambda c=currency: _execute_sell_order_for_coin_async(c)),
        ]

        continue_steps = True

        for step_name, step_fn in step_definitions:
            if not continue_steps:
                break

            result = await step_fn()
            coin_summary["steps"].append(
                {
                    "step": step_name,
                    "result": result,
                }
            )

            if result.get("status") != "completed":
                continue_steps = False

            await asyncio.sleep(0.5)

        results.append(coin_summary)

    success_coins = sum(
        1
        for item in results
        if all(step["result"].get("status") == "completed" for step in item["steps"])
    )

    return {
        "status": "completed",
        "total_coins": len(tradable_coins),
        "success_coins": success_coins,
        "results": results,
    }
