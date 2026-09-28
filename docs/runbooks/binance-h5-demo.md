# H5-LS-ENV-v1 Futures Demo manual lane

This runbook documents the H5-only code path. It does not grant order
permission. The separate operator contract must authorize H5 strategy orders
before an operator may start the runner. No worker in task 847 starts it.

## Identity and activation

The lane is isolated to BTCUSDT, ETHUSDT and SOLUSDT on the exact origin
https://demo-fapi.binance.com. It uses the existing Futures Demo credential
resolver and signed client. There is no live endpoint, credential path,
host allowlist change or scheduler. BINANCE_H5_DEMO_ENABLED and
BINANCE_FUTURES_DEMO_ENABLED both default off. Every tick requires explicit
confirmation; every mutation passes confirm=True to the existing transport.

After separate authority and migration checks, an operator may start the
manual foreground CLI in tmux with --loop and --confirm-demo. The --once mode
is also manual. Do not run either mode merely because this PR has merged.
The CLI prints an event or a blocked error class without credentials.

## Signal and entry

The adapter pages exactly 5,040 one-minute rows per symbol over 11 bounded
requests to obtain 21 complete four-hour bars; the ROB-993 500-row default
cannot supply that history. Missing minutes yield no four-hour bar. Only a
completed decision bucket is evaluated. The ROB-993 StrategyPlugin.evaluate
interface is used with the exact registered long and short envelope formula.
The signal key is symbol, KST minute, side and the original decimal close
text separated by vertical bars. The entry uses a fresh executable book ask
or bid, never a historical next open.

The H5 service commits a three-symbol opportunity grid before evaluating
orders. A PostgreSQL advisory transaction lock serializes H5 reservation,
including global two-position and one-per-symbol limits, five complete-bar
re-entry ban (bars one through five after exit blocked; sixth eligible) and
daily risk checks. Full account positions and open orders
are checked before reservation and immediately before send. Any foreign
exposure or incomplete read blocks entry. NAV sizing floors quantity to
MARKET_LOT_SIZE under NAV × 0.01 / 0.05 notional; it never rounds up to pass
MIN_NOTIONAL. Isolated 1x and one-way position mode require positive broker
readback and are never changed by this adapter.

## Fills, restart and holding

An H5 intent and deterministic client order ID are durable before send.
The send fence commits immediately before the sole submit attempt. A crash
or unknown response after the fence remains blocked; restart looks up that
same client order ID and the complete position state. A not-found response
does not prove absence and never triggers resubmit. A pre-send reserved intent
also blocks for review. Entry and close fill accounting needs matching broker
order status plus position evidence. The shared Demo ledger is written only
through BinanceDemoLedgerService. Its root is released after flat position
and empty open orders are proven.

Recovery also requires the broker order creation and update timestamps.
The holding clock starts at order creation as the conservative earliest
possible fill, so a delayed cumulative-fill lookup never extends the 24h
deadline. Closed-trip time uses the broker order update, not the lookup time.
These fields are carried by the broker [Query Order response](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Query-Order).

Every tick reads one-minute lows/highs since actual entry fill, including
between four-hour closes. The exit priority is intrabar adverse 5% hard
stop, completed-bar adverse 3% stop, six complete bars or 24-hour time
exit, 50% TP at favorable 3%, then remainder TP at favorable 5%. If an
historical bar touches stop and TP with unknown sequence, stop wins. Every
close is reduceOnly and limited to broker-proven remaining quantity.
Three stop losses per KST day stop new entries. NAV loss at least 3% from
KST day start latches the entry stop until the next KST day; drawdown at least 15% from observed peak
stops the lane. A daily loss gate does not bound realized loss.

## Control and weekly review

The fixed-seed control uses the committed opportunity grid, matching actual
closed-trip week, symbol, direction count, notional risk and fees. It has no
broker client or account orders. The offline weekly scorer reports net fees,
PF, NAV MDD, round-trip counts and actual versus control PF. After eight
weeks and at least 60 trips, PASS requires PF at least 1.2, MDD at most 15%,
and PF above control. MDD above 15% or PF below 0.8 at 30 trips is early
FAIL-RISK; otherwise the label is INSUFFICIENT_SAMPLE or FAIL-EFFICACY.
The control isolates entry signal value, not envelope value. Annualized net
return below 10% raises an operating-cost note.

The scorer reads a snapshot JSON file or H5 tables read-only and writes
its output to a local file. It never starts the runner or contacts Binance.
Both ledgers use the declared 0.0005 taker fee per fill notional; exchange
commission changes and funding are not silently treated as measured fees.

## Operational limits

The one-minute read detects price crossings only as observed minute extrema;
it cannot reconstruct the order of ticks inside a minute. Market close
execution can slip beyond a stop threshold. An unresolved broker response,
foreign exposure, missing history or forecast persistence failure blocks
new action pending evidence or operator review. The old ROB-993 and DFC
paths retain their own caps, ledger identity and CLI behavior.
The computed control observes four-hour grid quotes and conservative stop
touches rather than a replay of executable minute quotes; report this
resolution limit with every score. A restart after 24h reads the declared
first 24h envelope plus a current executable quote and exits by wall clock.
