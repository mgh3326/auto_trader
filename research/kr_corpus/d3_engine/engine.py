"""Pure event-driven D3 portfolio engine for B0/C1/C2/C3."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext

from research.kr_corpus.d3_engine.cash import CashLedger
from research.kr_corpus.d3_engine.constants import (
    CONTRACT_SHA256,
    DECIMAL_PRECISION,
    DECIMAL_ROUNDING,
    TICK_TABLE_SHA256,
    runtime_pins,
)
from research.kr_corpus.d3_engine.guards import SealedAccessGuard
from research.kr_corpus.d3_engine.indicators import (
    OhlcPoint,
    bollinger_bands,
    fib_levels,
    fib_resistance_above_close,
    rsi_wilder,
    scan_fib_window,
)
from research.kr_corpus.d3_engine.metrics import (
    deployment_mean,
    locked_share_time_weighted_mean,
    nearest_rank,
    twr_returns,
)
from research.kr_corpus.d3_engine.models import (
    Arm,
    Bar,
    CashflowView,
    EngineResult,
    Fill,
    Order,
    OrderClass,
    OrderSide,
    OrderStatus,
    PortfolioRunInput,
    Position,
    RunState,
)
from research.kr_corpus.d3_engine.policies import (
    C1Cycle,
    c2_allows,
    c3_buy_suppressed,
    c3_trim_quantity,
    mark_c3_trim_filled,
    mark_c3_trim_skipped,
    update_c3_close,
)
from research.kr_corpus.d3_engine.signals import (
    PriceLevel,
    SignalCandidate,
    build_buy_rungs,
    choose_l2,
    cluster_levels,
    rank_candidates,
    support_distance,
)
from research.kr_corpus.d3_engine.tick import TickTable


class RunInvalid(ValueError):
    code = "RUN_INVALID"


class PortfolioEngine:
    """Execute deterministic session events without broker, DB, or scheduler access."""

    def __init__(
        self,
        tick_table: TickTable,
        *,
        access_guard: SealedAccessGuard | None = None,
    ) -> None:
        tick_table.validate()
        self._ticks = tick_table
        self._guard = access_guard or SealedAccessGuard()

    def run(self, run_input: PortfolioRunInput) -> EngineResult:
        """Run one arm under the contract's fixed Decimal arithmetic context."""

        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            context.rounding = DECIMAL_ROUNDING
            if run_input.arm is Arm.B0:
                return self._run(run_input)
            b0_result = self._run(replace(run_input, arm=Arm.B0))
            b0_demand = frozenset(
                (item["session"], item["symbol"])
                for item in b0_result.evidence["counterfactual_demand_pairs"]
            )
            return self._run(run_input, b0_demand_pairs=b0_demand)

    def _run(
        self,
        run_input: PortfolioRunInput,
        *,
        b0_demand_pairs: frozenset[tuple[date, str]] | None = None,
    ) -> EngineResult:
        bars = sorted(run_input.bars, key=lambda item: (item.session, item.symbol))
        if not bars:
            raise RunInvalid("empty bar input")
        if len({(bar.session, bar.symbol) for bar in bars}) != len(bars):
            raise RunInvalid("duplicate symbol/session bar")
        for bar in bars:
            self._guard.assert_exploration_date(bar.session)

        if run_input.market_sessions:
            sessions = list(run_input.market_sessions)
            if sessions != sorted(set(sessions)):
                raise RunInvalid("market_sessions must be strict ascending unique")
        else:
            sessions = sorted(
                {bar.session for bar in bars}
                | {action.session for action in run_input.corporate_actions}
            )
        session_positions = {session: index for index, session in enumerate(sessions)}
        for session in sessions:
            self._guard.assert_exploration_date(session)
        if any(bar.session not in session_positions for bar in bars):
            raise RunInvalid("bar session is outside market_sessions")
        if any(
            action.session not in session_positions
            for action in run_input.corporate_actions
        ):
            raise RunInvalid("corporate action is outside market_sessions")
        if run_input.decision_start is not None and not any(
            session >= run_input.decision_start for session in sessions
        ):
            raise RunInvalid("decision_start is after all input sessions")
        processed_sessions = [
            session
            for session in sessions
            if run_input.decision_start is None or session >= run_input.decision_start
        ]
        terminal_session = processed_sessions[-1]
        by_session = {(bar.session, bar.symbol): bar for bar in bars}
        histories: dict[str, list[Bar]] = defaultdict(list)
        for bar in bars:
            histories[bar.symbol].append(bar)
        history_index = {
            (bar.session, bar.symbol): index
            for symbol_bars in histories.values()
            for index, bar in enumerate(symbol_bars)
        }
        contiguous_start: dict[tuple[date, str], int] = {}
        for symbol, symbol_bars in histories.items():
            segment_start = 0
            for index, bar in enumerate(symbol_bars):
                if index and session_positions[bar.session] != (
                    session_positions[symbol_bars[index - 1].session] + 1
                ):
                    segment_start = index
                contiguous_start[(bar.session, symbol)] = segment_start

        state = RunState()
        cash = CashLedger(run_input.config.initial_cash)
        c1_cycles: dict[str, C1Cycle] = defaultdict(C1Cycle)
        cumulative_contribution = Decimal(0)
        cumulative_contribution_series: list[Decimal] = []
        daily_invested: list[Decimal] = []
        daily_locked_ratios: list[Decimal] = []
        nav_series: list[Decimal] = []
        unit_price_series: list[Decimal] = [Decimal(1)]
        units = run_input.config.initial_cash
        last_unit_price = Decimal(1)
        last_closes: dict[str, Decimal] = {}
        previous_month: tuple[int, int] | None = None
        status = "OK"
        unserved_sessions: set[date] = set()
        unserved_notional = Decimal(0)
        demand_pairs: set[tuple[date, str]] = set()
        order_counter = 0

        actions_by_session: dict[date, list[object]] = defaultdict(list)
        for action in sorted(
            run_input.corporate_actions,
            key=lambda item: (item.session, item.symbol, item.kind),
        ):
            self._guard.assert_exploration_date(action.session)
            actions_by_session[action.session].append(action)

        index_values = dict(run_input.index_closes)
        if len(index_values) != len(run_input.index_closes):
            raise RunInvalid("duplicate KOSPI index session")
        index_dates = sorted(index_values)
        for index_date in index_dates:
            self._guard.assert_exploration_date(index_date)
            index_value = index_values[index_date]
            if not index_value.is_finite() or index_value <= 0:
                raise RunInvalid("KOSPI index closes must be finite positive")

        for session_index, session in enumerate(sessions):
            if (
                run_input.decision_start is not None
                and session < run_input.decision_start
            ):
                continue
            for symbol in sorted(histories):
                current_bar = by_session.get((session, symbol))
                if current_bar is not None:
                    last_closes[symbol] = current_bar.close
            settlement = cash.settle_pre_open(session_index)
            if settlement["payable_cleared"] or settlement["receivable_credited"]:
                state.events.append(
                    {
                        "event": "t2_settle_pre_open",
                        "session": session,
                        **settlement,
                    }
                )

            current_month = (session.year, session.month)
            if current_month != previous_month:
                previous_month = current_month
                if run_input.cashflow_view is CashflowView.WITH_CONTRIBUTION:
                    units += run_input.config.monthly_contribution / last_unit_price
                    cash.contribute(run_input.config.monthly_contribution)
                    cumulative_contribution += run_input.config.monthly_contribution
                    state.events.append(
                        {
                            "event": "monthly_contribution_pre_open",
                            "session": session,
                            "amount": run_input.config.monthly_contribution,
                        }
                    )

            for action in actions_by_session.get(session, []):
                position = state.positions.get(action.symbol)
                if action.kind == "split" and action.split_factor is None:
                    state.events.append(
                        {
                            "event": "corporate_action",
                            "session": session,
                            "symbol": action.symbol,
                            "label": "ADJUSTED_PRICE_SIMULATION",
                            "quantity_unchanged": position.quantity if position else 0,
                        }
                    )
                if (
                    action.data_ends_before_exploration_end
                    and position is not None
                    and position.quantity > 0
                ):
                    status = "INCONCLUSIVE_UNRESOLVED_TERMINAL"
                    state.events.append(
                        {
                            "event": "unresolved_terminal",
                            "session": session,
                            "symbol": action.symbol,
                            "quantity": position.quantity,
                        }
                    )

            self._expire_orders(
                state=state,
                cash=cash,
                c1_cycles=c1_cycles,
                arm=run_input.arm,
                session=session,
            )

            c3_time_trim_symbols: set[str] = set()
            if run_input.arm is Arm.C3:
                c3_time_trim_symbols = self._execute_armed_time_trims(
                    state=state,
                    cash=cash,
                    by_session=by_session,
                    session=session,
                    session_index=session_index,
                    fee_rate=run_input.config.fee_rate,
                )

            candidates: list[SignalCandidate] = []
            candidate_clusters: dict[str, Decimal] = {}
            for symbol in sorted(histories):
                bar = by_session.get((session, symbol))
                if bar is None:
                    continue
                index = history_index[(session, symbol)]
                segment_start = contiguous_start[(session, symbol)]
                history = histories[symbol][segment_start : index + 1]
                decision_index = len(history) - 1
                signal = self._signal_for_session(history, decision_index)
                if signal is None:
                    continue
                rsi, l2_price, _, _ = signal
                previous_close = history[decision_index - 1].close
                position = state.positions.get(symbol)
                is_add = position is not None and position.quantity > 0
                if is_add and previous_close >= position.average_price:
                    continue
                candidate = SignalCandidate(
                    symbol=symbol,
                    rsi=rsi,
                    support_distance=abs(support_distance(l2_price, previous_close)),
                    support_price=l2_price,
                    is_add=is_add,
                )
                candidates.append(candidate)
                candidate_clusters[symbol] = l2_price

            ranked = rank_candidates(
                candidates,
                max_new=(
                    run_input.config.max_new_entries_per_session
                    if b0_demand_pairs is None
                    else len(candidates)
                ),
            )
            if b0_demand_pairs is None:
                demand_pairs.update((session, item.symbol) for item in ranked)
            else:
                ranked = tuple(
                    item for item in ranked if (session, item.symbol) in b0_demand_pairs
                )
                actual_pairs = {(session, item.symbol) for item in ranked}
                expected_pairs = {
                    pair for pair in b0_demand_pairs if pair[0] == session
                }
                for _, symbol in sorted(expected_pairs - actual_pairs):
                    state.events.append(
                        {
                            "event": "policy_rejected",
                            "reason": "not_arm_eligible_against_b0_demand",
                            "session": session,
                            "symbol": symbol,
                        }
                    )
                    unserved_sessions.add(session)
                    unserved_notional += run_input.config.order_notional
            buy_orders: list[Order] = []
            for rank, candidate in enumerate(ranked, start=1):
                position = state.positions.get(candidate.symbol)
                if (
                    run_input.arm is Arm.C3
                    and position
                    and (
                        c3_buy_suppressed(position)
                        or candidate.symbol in c3_time_trim_symbols
                    )
                ):
                    state.events.append(
                        {
                            "event": "policy_rejected",
                            "reason": "c3_time_trim_armed",
                            "session": session,
                            "symbol": candidate.symbol,
                        }
                    )
                    unserved_sessions.add(session)
                    unserved_notional += run_input.config.order_notional
                    continue
                if run_input.arm is Arm.C2 and not self._c2_session_allows(
                    session=session,
                    market_sessions=sessions,
                    session_positions=session_positions,
                    index_values=index_values,
                ):
                    state.events.append(
                        {
                            "event": "policy_rejected",
                            "reason": "c2_below_sma200_or_missing",
                            "session": session,
                            "symbol": candidate.symbol,
                        }
                    )
                    unserved_sessions.add(session)
                    unserved_notional += run_input.config.order_notional
                    continue
                candidate_index = history_index[(session, candidate.symbol)]
                previous_close = histories[candidate.symbol][candidate_index - 1].close
                for rung, limit in build_buy_rungs(
                    close=previous_close,
                    l2_price=candidate_clusters[candidate.symbol],
                    tick_table=self._ticks,
                ):
                    quantity = int(run_input.config.order_notional // limit)
                    if quantity < 1:
                        continue
                    order_counter += 1
                    order = Order(
                        order_id=f"D3-{session.isoformat()}-{order_counter:08d}",
                        session=session,
                        symbol=candidate.symbol,
                        side=OrderSide.BUY,
                        order_class=(
                            OrderClass.ADD if candidate.is_add else OrderClass.NEW
                        ),
                        limit=limit,
                        quantity=quantity,
                        rung=rung,
                        rank=rank,
                    )
                    if run_input.arm is Arm.C1:
                        admitted, reason = c1_cycles[candidate.symbol].reserve(
                            notional=order.gross_limit_notional,
                            is_add=candidate.is_add,
                        )
                        if not admitted:
                            order.status = OrderStatus.POLICY_REJECTED
                            state.events.append(
                                {
                                    "event": "policy_rejected",
                                    "reason": reason,
                                    "session": session,
                                    "symbol": candidate.symbol,
                                    "rung": rung,
                                }
                            )
                            unserved_sessions.add(session)
                            unserved_notional += order.gross_limit_notional
                            continue
                    reservation = order.gross_limit_notional * (
                        Decimal(1) + run_input.config.fee_rate
                    )
                    if not cash.reserve_order(order.order_id, reservation):
                        if run_input.arm is Arm.C1:
                            c1_cycles[candidate.symbol].expire(
                                order.gross_limit_notional,
                                is_add=candidate.is_add,
                            )
                        order.status = OrderStatus.CASH_REJECTED
                        state.events.append(
                            {
                                "event": "cash_rejected",
                                "session": session,
                                "symbol": candidate.symbol,
                                "rung": rung,
                            }
                        )
                        unserved_sessions.add(session)
                        unserved_notional += order.gross_limit_notional
                        continue
                    buy_orders.append(order)
                    state.events.append(self._order_event(order))

            sell_orders: list[Order] = []
            for symbol in sorted(state.positions):
                position = state.positions[symbol]
                if position.quantity < 1:
                    continue
                bar = by_session.get((session, symbol))
                if bar is None:
                    continue
                index = history_index[(session, symbol)]
                segment_start = contiguous_start[(session, symbol)]
                history = histories[symbol][segment_start : index + 1]
                decision_index = len(history) - 1
                if decision_index < 120:
                    continue
                if run_input.arm is Arm.C3 and c3_buy_suppressed(position):
                    continue
                new_sell_orders = self._resistance_orders(
                    symbol=symbol,
                    session=session,
                    history=history,
                    index=decision_index,
                    position=position,
                    first_order_number=order_counter,
                )
                sell_orders.extend(new_sell_orders)
                order_counter += len(new_sell_orders)
            for order in sell_orders:
                state.events.append(self._order_event(order))

            bought_symbols: set[str] = set()
            for order in buy_orders:
                bar = by_session[(session, order.symbol)]
                fill_price = self._buy_fill_price(order.limit, bar)
                if fill_price is None:
                    state.pending_orders.append(order)
                    continue
                gross = fill_price * order.quantity
                fee = gross * run_input.config.fee_rate
                cash.fill_buy(
                    order_id=order.order_id,
                    actual_amount=gross + fee,
                    trade_session_index=session_index,
                )
                position = state.positions.setdefault(
                    order.symbol, Position(symbol=order.symbol)
                )
                position.apply_buy(
                    quantity=order.quantity,
                    price=fill_price,
                    fee=fee,
                    session_index=session_index,
                )
                if run_input.arm is Arm.C1:
                    c1_cycles[order.symbol].fill(
                        notional=gross,
                        is_add=order.order_class is OrderClass.ADD,
                        reserved_notional=order.gross_limit_notional,
                    )
                order.status = OrderStatus.FILLED
                order.fill_price = fill_price
                fill = Fill(
                    order.order_id,
                    session,
                    order.symbol,
                    OrderSide.BUY,
                    order.order_class,
                    order.quantity,
                    fill_price,
                    gross,
                    fee,
                )
                state.fills.append(fill)
                bought_symbols.add(order.symbol)
                state.events.append(self._fill_event(fill))

            for order in sell_orders:
                if order.symbol in bought_symbols:
                    state.pending_orders.append(order)
                    continue
                bar = by_session[(session, order.symbol)]
                fill_price = self._sell_fill_price(order.limit, bar)
                if fill_price is None:
                    state.pending_orders.append(order)
                    continue
                position = state.positions[order.symbol]
                quantity = min(order.quantity, position.quantity)
                if quantity < 1:
                    continue
                gross = fill_price * quantity
                fee = gross * run_input.config.fee_rate
                cash.fill_sell(
                    net_amount=gross - fee,
                    trade_session_index=session_index,
                )
                position.apply_sell(quantity=quantity)
                order.status = OrderStatus.FILLED
                order.fill_price = fill_price
                fill = Fill(
                    order.order_id,
                    session,
                    order.symbol,
                    OrderSide.SELL,
                    order.order_class,
                    quantity,
                    fill_price,
                    gross,
                    fee,
                )
                state.fills.append(fill)
                state.events.append(self._fill_event(fill))
                if position.quantity == 0:
                    c1_cycles.pop(order.symbol, None)

            if run_input.arm is Arm.C3:
                for symbol in sorted(state.positions):
                    position = state.positions[symbol]
                    bar = by_session.get((session, symbol))
                    if bar is None:
                        if position.quantity > 0:
                            position.underwater_streak = 0
                            state.events.append(
                                {
                                    "event": "c3_close_clock",
                                    "session": session,
                                    "symbol": symbol,
                                    "underwater": False,
                                    "streak": 0,
                                    "reset_reason": "missing_session_close",
                                    "armed_90": False,
                                    "armed_180": False,
                                }
                            )
                        continue
                    outcome = update_c3_close(position, close=bar.close)
                    state.events.append(
                        {
                            "event": "c3_close_clock",
                            "session": session,
                            "symbol": symbol,
                            "underwater": outcome.underwater,
                            "streak": outcome.streak,
                            "armed_90": outcome.armed_90,
                            "armed_180": outcome.armed_180,
                        }
                    )

            self._finish_day(
                state=state,
                cash=cash,
                by_session=by_session,
                session=session,
                cumulative_contribution=cumulative_contribution,
                cumulative_contribution_series=cumulative_contribution_series,
                daily_invested=daily_invested,
                daily_locked_ratios=daily_locked_ratios,
                nav_series=nav_series,
                last_closes=last_closes,
                fee_rate=run_input.config.fee_rate,
                terminal=session == terminal_session,
            )
            last_unit_price = nav_series[-1] / units
            unit_price_series.append(last_unit_price)

        terminal_positions = tuple(
            {
                "symbol": symbol,
                "quantity": position.quantity,
                "average_price": position.average_price,
                "invested_cost_basis": position.invested_cost_basis,
                "underwater_streak": position.underwater_streak,
            }
            for symbol, position in sorted(state.positions.items())
            if position.quantity > 0
        )
        deployment, _ = deployment_mean(
            daily_invested_cost=daily_invested,
            cumulative_contribution=cumulative_contribution_series,
            initial_cash=run_input.config.initial_cash,
        )
        drawdown = self._max_drawdown(unit_price_series)
        locked_p95 = nearest_rank(daily_locked_ratios, Decimal("0.95"))
        calendar_days = (processed_sessions[-1] - processed_sessions[0]).days
        twr_annualized: Decimal | None = None
        if calendar_days > 0:
            _, twr_annualized = twr_returns(
                start_unit_price=Decimal(1),
                end_unit_price=unit_price_series[-1],
                calendar_days=Decimal(calendar_days),
            )
        metrics: dict[str, object] = {
            "terminal_nav": nav_series[-1],
            "unitized_mdd": drawdown,
            "twr_cumulative": unit_price_series[-1] - Decimal(1),
            "twr_annualized": twr_annualized,
            "twr_calendar_days": calendar_days,
            "locked_share_tw_mean": locked_share_time_weighted_mean(
                daily_locked_ratios
            ),
            "locked_share_p95": locked_p95,
            "locked_share_max": max(daily_locked_ratios),
            "deployment_mean": deployment,
            "unserved_counterfactual_demand_sessions": len(unserved_sessions),
            "unserved_notional_diagnostic": unserved_notional,
            "policy_rejected": sum(
                event["event"] == "policy_rejected" for event in state.events
            ),
            "cash_rejected": sum(
                event["event"] == "cash_rejected" for event in state.events
            ),
            "signals_submitted": sum(
                event["event"] == "order_submitted" for event in state.events
            ),
            "fills": len(state.fills),
        }
        evidence: dict[str, object] = {
            "contract_sha256": dict(sorted(CONTRACT_SHA256.items())),
            "tick_source": "krx_tick_table_frozen.yaml",
            "tick_sha256": TICK_TABLE_SHA256,
            "tick_python_imports": 0,
            "fib_window_excludes_t": True,
            "counterfactual_demand_basis": (
                "B0_self" if b0_demand_pairs is None else "B0_shadow"
            ),
            "counterfactual_demand_pairs": [
                {"session": session, "symbol": symbol}
                for session, symbol in sorted(
                    demand_pairs if b0_demand_pairs is None else b0_demand_pairs
                )
            ],
            "session_order": [
                "contribution_pre_open",
                "corporate_action",
                "prior_order_expiry",
                "c3_time_trim_next_valid_open_before_buy",
                "indicator_signal_rung",
                "order_issue_reserve",
                "fill_buy_before_sell",
            ],
            "primary_run_executed": False,
            "runtime_pins": runtime_pins(),
            **self._guard.spy.evidence(),
        }
        return EngineResult(
            arm=run_input.arm,
            cashflow_view=run_input.cashflow_view,
            data_view=run_input.data_view,
            events=tuple(state.events),
            fills=tuple(state.fills),
            terminal_positions=terminal_positions,
            metrics=metrics,
            evidence=evidence,
            status=status,
        )

    def _signal_for_session(
        self, history: list[Bar], decision_index: int
    ) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
        if decision_index < 120:
            return None
        points = tuple(OhlcPoint(bar.high, bar.low, bar.close) for bar in history)
        window = scan_fib_window(points, decision_index=decision_index)
        previous_closes = [bar.close for bar in history[:decision_index]]
        rsi_series = rsi_wilder(previous_closes)
        rsi = rsi_series[-1]
        if rsi is None:
            return None
        bands = bollinger_bands(previous_closes)
        previous_close = previous_closes[-1]
        levels = [
            PriceLevel(price, "fib_family", f"fib_{ratio}")
            for ratio, price in fib_levels(window.low, window.high).items()
        ]
        levels.append(PriceLevel(bands.lower, "bb_lower", "bb_lower"))
        clusters = cluster_levels(levels, close=previous_close)
        l2 = choose_l2(clusters, close=previous_close)
        rounded_rsi = rsi.quantize(Decimal("0.0001"), rounding=DECIMAL_ROUNDING)
        if rounded_rsi >= Decimal("45") or l2 is None:
            return None
        return rounded_rsi, l2.representative, window.high, window.low

    @staticmethod
    def _c2_session_allows(
        *,
        session: date,
        market_sessions: list[date],
        session_positions: dict[date, int],
        index_values: dict[date, Decimal],
    ) -> bool:
        if session not in index_values or session not in session_positions:
            return False
        session_index = session_positions[session]
        if session_index < 200:
            return False
        prior = market_sessions[session_index - 200 : session_index]
        if len(prior) != 200 or any(day not in index_values for day in prior):
            return False
        close = index_values[prior[-1]]
        sma = sum((index_values[day] for day in prior), Decimal(0)) / Decimal(200)
        return c2_allows(t_minus_1_close=close, sma200=sma)

    def _resistance_orders(
        self,
        *,
        symbol: str,
        session: date,
        history: list[Bar],
        index: int,
        position: Position,
        first_order_number: int,
    ) -> list[Order]:
        points = tuple(OhlcPoint(bar.high, bar.low, bar.close) for bar in history)
        window = scan_fib_window(points, decision_index=index)
        previous_closes = [bar.close for bar in history[:index]]
        previous_close = previous_closes[-1]
        bands = bollinger_bands(previous_closes)
        levels = [
            PriceLevel(price, "fib_resistance_family", f"fib_r_{ratio}")
            for ratio, price in fib_resistance_above_close(
                window.low, window.high, previous_close
            ).items()
        ]
        levels.append(PriceLevel(bands.upper, "bb_upper", "bb_upper"))
        clusters = [
            cluster
            for cluster in cluster_levels(levels, close=previous_close)
            if cluster.qualifies and cluster.representative > previous_close
        ][:2]
        if not clusters:
            return []
        first_quantity = position.quantity // 2
        quantities = [first_quantity, position.quantity - first_quantity]
        orders: list[Order] = []
        for offset, (cluster, quantity) in enumerate(
            zip(clusters, quantities, strict=False), start=1
        ):
            if quantity < 1:
                continue
            orders.append(
                Order(
                    order_id=f"D3-{session.isoformat()}-{first_order_number + offset:08d}",
                    session=session,
                    symbol=symbol,
                    side=OrderSide.SELL,
                    order_class=OrderClass.RESISTANCE_TRIM,
                    limit=self._ticks.sell_limit(cluster.representative),
                    quantity=quantity,
                    rung=f"R{offset}",
                    rank=offset,
                )
            )
        return orders

    def _execute_armed_time_trims(
        self,
        *,
        state: RunState,
        cash: CashLedger,
        by_session: dict[tuple[date, str], Bar],
        session: date,
        session_index: int,
        fee_rate: Decimal,
    ) -> set[str]:
        processed: set[str] = set()
        for symbol in sorted(state.positions):
            position = state.positions[symbol]
            stage = (
                90 if position.trim90_armed else 180 if position.trim180_armed else None
            )
            if stage is None:
                continue
            bar = by_session.get((session, symbol))
            if bar is None:
                continue
            processed.add(symbol)
            quantity = c3_trim_quantity(position)
            if quantity < 1:
                mark_c3_trim_skipped(position, stage=stage)
                state.events.append(
                    {
                        "event": "c3_time_trim_skipped",
                        "session": session,
                        "symbol": symbol,
                        "stage": stage,
                        "reason": "trim_qty_lt_1",
                    }
                )
                continue
            gross = bar.open * quantity
            fee = gross * fee_rate
            cash.fill_sell(net_amount=gross - fee, trade_session_index=session_index)
            position.apply_sell(quantity=quantity)
            mark_c3_trim_filled(position, stage=stage)
            fill = Fill(
                order_id=f"D3-{session.isoformat()}-TIME-{symbol}-{stage}",
                session=session,
                symbol=symbol,
                side=OrderSide.SELL,
                order_class=OrderClass.TIME_TRIM,
                quantity=quantity,
                price=bar.open,
                gross=gross,
                fee=fee,
            )
            state.fills.append(fill)
            state.events.append(self._fill_event(fill))
        return processed

    @staticmethod
    def _expire_orders(
        *,
        state: RunState,
        cash: CashLedger,
        c1_cycles: dict[str, C1Cycle],
        arm: Arm,
        session: date,
    ) -> None:
        for order in sorted(state.pending_orders, key=lambda item: item.order_id):
            order.status = OrderStatus.EXPIRED
            if order.side is OrderSide.BUY:
                cash.expire_order(order.order_id)
                if arm is Arm.C1:
                    c1_cycles[order.symbol].expire(
                        order.gross_limit_notional,
                        is_add=order.order_class is OrderClass.ADD,
                    )
            state.events.append(
                {
                    "event": "order_expired",
                    "session": session,
                    "order_id": order.order_id,
                    "symbol": order.symbol,
                }
            )
        state.pending_orders.clear()

    @staticmethod
    def _finish_day(
        *,
        state: RunState,
        cash: CashLedger,
        by_session: dict[tuple[date, str], Bar],
        session: date,
        cumulative_contribution: Decimal,
        cumulative_contribution_series: list[Decimal],
        daily_invested: list[Decimal],
        daily_locked_ratios: list[Decimal],
        nav_series: list[Decimal],
        last_closes: dict[str, Decimal],
        fee_rate: Decimal,
        terminal: bool,
    ) -> None:
        invested = sum(
            (
                position.invested_cost_basis
                for position in state.positions.values()
                if position.quantity > 0
            ),
            Decimal(0),
        )
        locked = sum(
            (
                position.invested_cost_basis
                for position in state.positions.values()
                if position.quantity > 0 and position.underwater_streak >= 180
            ),
            Decimal(0),
        )
        daily_invested.append(invested)
        daily_locked_ratios.append(locked / invested if invested else Decimal(0))
        cumulative_contribution_series.append(cumulative_contribution)
        reserved_cash = sum(cash.reserved_orders.values(), Decimal(0))
        receivable_cash = sum(
            (settlement.amount for settlement in cash.receivables), Decimal(0)
        )
        positions_value = Decimal(0)
        for symbol, position in state.positions.items():
            if position.quantity < 1:
                continue
            close = (
                by_session[(session, symbol)].close
                if (session, symbol) in by_session
                else last_closes.get(symbol)
            )
            if close is None:
                continue
            gross = close * position.quantity
            positions_value += gross * (Decimal(1) - fee_rate) if terminal else gross
        nav_series.append(
            cash.orderable_cash + reserved_cash + receivable_cash + positions_value
        )

    @staticmethod
    def _max_drawdown(values: Iterable[Decimal]) -> Decimal:
        peak: Decimal | None = None
        worst = Decimal(0)
        for value in values:
            if peak is None or value > peak:
                peak = value
            if peak and peak > 0:
                worst = min(worst, value / peak - Decimal(1))
        return worst

    @staticmethod
    def _buy_fill_price(limit: Decimal, bar: Bar) -> Decimal | None:
        if bar.open <= limit:
            return bar.open
        if bar.low <= limit:
            return limit
        return None

    @staticmethod
    def _sell_fill_price(limit: Decimal, bar: Bar) -> Decimal | None:
        if bar.open >= limit:
            return bar.open
        if bar.high >= limit:
            return limit
        return None

    @staticmethod
    def _order_event(order: Order) -> dict[str, object]:
        return {
            "event": "order_submitted",
            "session": order.session,
            "order_id": order.order_id,
            "symbol": order.symbol,
            "side": order.side,
            "class": order.order_class,
            "rung": order.rung,
            "limit": order.limit,
            "quantity": order.quantity,
        }

    @staticmethod
    def _fill_event(fill: Fill) -> dict[str, object]:
        return {
            "event": "fill",
            "session": fill.session,
            "order_id": fill.order_id,
            "symbol": fill.symbol,
            "side": fill.side,
            "class": fill.order_class,
            "quantity": fill.quantity,
            "price": fill.price,
            "gross": fill.gross,
            "fee": fill.fee,
        }
