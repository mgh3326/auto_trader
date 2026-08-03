#!/usr/bin/env python
"""Stage 1b — compare Kiwoom mock vs live chart data (READ-ONLY).

Default-disabled: without ``--confirm-live-read`` this script makes no network
call at all. It reads charts and nothing else — the live side goes through
``KiwoomLiveReadOnlyClient``, whose api-id/path allowlists, pinned host, and
env gate make an order call unreachable from here.

🔴 Never invoke with ``ENV_FILE=.env.prod``. Use the scoped credentials file
that contains only the four Kiwoom app-key values and no account number.

Usage:
    uv run python -m scripts.kiwoom_live_readonly_compare                    # dry run
    uv run python -m scripts.kiwoom_live_readonly_compare --confirm-live-read
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_ENV_FILE = Path(
    "/Users/mgh3326/services/auto_trader/shared/.env.kiwoom-readonly.native"
)

#: Exactly the keys this script may take from the env file. Anything else in
#: the file is ignored; nothing is ever printed.
REQUIRED_ENV_KEYS = (
    "KIWOOM_APP_KEY",
    "KIWOOM_APP_SECRET",
    "KIWOOM_MOCK_APP_KEY",
    "KIWOOM_MOCK_APP_SECRET",
)

#: Fixed comparison sample: 10 KOSPI + 10 KOSDAQ, sorted for determinism.
#: ⚠️ Hand-assembled liquid names, NOT a measured top-turnover ranking — the
#: only turnover sources available are the production DB, which this job may
#: not touch. Realised 거래대금 is reported per symbol so the reader can see
#: what was actually sampled. All four crosscheckable symbols are included.
KOSPI_SYMBOLS = (
    "000270",  # 기아
    "000660",  # SK하이닉스        (crosscheckable)
    "005380",  # 현대차
    "005930",  # 삼성전자          (crosscheckable)
    "006400",  # 삼성SDI
    "035420",  # NAVER             (crosscheckable)
    "035720",  # 카카오
    "051910",  # LG화학
    "068270",  # 셀트리온          (crosscheckable)
    "105560",  # KB금융
)
KOSDAQ_SYMBOLS = (
    "028300",  # HLB
    "041510",  # 에스엠
    "060310",  # 3S               (fable-probed)
    "145020",  # 휴젤
    "196170",  # 알테오젠
    "247540",  # 에코프로비엠
    "263750",  # 펄어비스
    "265520",  # AP시스템          (fable-probed)
    "293490",  # 카카오게임즈
    "086520",  # 에코프로
)

MINUTE_TIC_SCOPE = "5"


def _load_scoped_env(path: Path) -> dict[str, str]:
    """Parse only the four required keys. Values are never logged or returned
    anywhere they could be printed."""

    if not path.exists():
        raise SystemExit(f"env file not found: {path}")
    if "prod" in path.name:
        raise SystemExit(f"refusing to read a production env file: {path}")

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in REQUIRED_ENV_KEYS:
            values[key] = value.strip().strip('"').strip("'")

    missing = [k for k in REQUIRED_ENV_KEYS if not values.get(k)]
    if missing:
        raise SystemExit(f"env file missing required keys: {', '.join(missing)}")

    forbidden = [k for k in ("KIWOOM_ACCOUNT_NO", "DATABASE_URL") if k in os.environ]
    if forbidden:
        print(f"⚠️  WARNING: {forbidden} present in process env (not used here)")
    return values


class Pacer:
    """Serial rate limiter + hard call budget."""

    def __init__(self, *, min_interval: float, max_calls: int) -> None:
        self.min_interval = min_interval
        self.max_calls = max_calls
        self.calls = 0
        self.live_calls = 0
        self.mock_calls = 0
        self._last = 0.0
        self.observations: list[dict[str, Any]] = []

    async def wait(self, interval: float | None = None) -> None:
        gap = self.min_interval if interval is None else interval
        if self._last:
            remaining = gap - (time.monotonic() - self._last)
            if remaining > 0:
                await asyncio.sleep(remaining)

    def spend(self, side: str) -> None:
        if self.calls >= self.max_calls:
            raise RuntimeError(f"call budget {self.max_calls} exhausted")
        self.calls += 1
        if side == "live":
            self.live_calls += 1
        else:
            self.mock_calls += 1
        self._last = time.monotonic()


async def _timed(pacer: Pacer, side: str, label: str, coro_fn, interval=None):
    """Run one call, record timing/outcome, never raise past the caller."""

    await pacer.wait(interval)
    started = time.monotonic()
    pacer.spend(side)
    try:
        payload = await coro_fn()
        elapsed = time.monotonic() - started
        pacer.observations.append(
            {
                "side": side,
                "label": label,
                "interval": interval if interval is not None else pacer.min_interval,
                "ok": True,
                "elapsed_s": round(elapsed, 3),
                "return_code": payload.get("return_code"),
            }
        )
        return payload, None
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        pacer.observations.append(
            {
                "side": side,
                "label": label,
                "interval": interval if interval is not None else pacer.min_interval,
                "ok": False,
                "elapsed_s": round(elapsed, 3),
                "error": type(exc).__name__,
            }
        )
        return None, exc


async def run(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file)
    creds = _load_scoped_env(env_file)

    # Arm the gate and map the scoped env names onto the live settings names
    # BEFORE app.core.config is imported.
    os.environ["KIWOOM_LIVE_MARKETDATA_ENABLED"] = "true"
    os.environ["KIWOOM_LIVE_APP_KEY"] = creds["KIWOOM_APP_KEY"]
    os.environ["KIWOOM_LIVE_APP_SECRET"] = creds["KIWOOM_APP_SECRET"]

    # ``Settings`` requires several unrelated fields that the scoped credentials
    # file deliberately does not carry. Fill them with obviously non-functional
    # placeholders so the singleton constructs — rather than sourcing a
    # production env file, which is forbidden here and would drag in the real
    # DB URL and other brokers' live credentials. Nothing in this script opens a
    # database, and the placeholder DSN points at a closed port.
    for key, placeholder in (
        ("KIS_APP_KEY", "unused-placeholder-not-a-credential"),
        ("KIS_APP_SECRET", "unused-placeholder-not-a-credential"),
        ("OPENDART_API_KEY", "unused-placeholder-not-a-credential"),
        ("UPBIT_ACCESS_KEY", "unused-placeholder-not-a-credential"),
        ("UPBIT_SECRET_KEY", "unused-placeholder-not-a-credential"),
        # SECRET_KEY has a strength validator (length + character classes +
        # entropy). This value is throwaway, used only to let Settings build in
        # this read-only process; nothing signs or encrypts anything here.
        ("SECRET_KEY", "Kiwoom7ReadOnly3Compare9Placeholder2Value8Xy"),
        ("DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"),
    ):
        os.environ.setdefault(key, placeholder)
    if "prod" in os.environ.get("ENV_FILE", ""):
        raise SystemExit("refusing to run with a production ENV_FILE")

    # OAuth tokens are cached in Redis. Point that at a throwaway instance so
    # this read-only comparison never writes keys into the deployment's shared
    # cache (which holds ~24k live OHLCV entries).
    if args.redis_url:
        os.environ["REDIS_URL"] = args.redis_url

    from app.services.brokers.kiwoom import constants
    from app.services.brokers.kiwoom.chart_compare import (
        ChartKind,
        adjudicate_mismatches,
        compare_chart_payloads,
        extract_rows,
        load_frozen_kis_sample,
    )
    from app.services.brokers.kiwoom.client import KiwoomMockClient
    from app.services.brokers.kiwoom.live_market_data import (
        ALLOWED_API_IDS,
        CHART_PATH,
        KiwoomLiveReadOnlyClient,
    )

    symbols = sorted(KOSPI_SYMBOLS) + sorted(KOSDAQ_SYMBOLS)
    base_dt = args.base_dt

    print(f"env_file       : {env_file}  (4 keys, values never printed)")
    print(f"symbols        : {len(symbols)} (10 KOSPI + 10 KOSDAQ, sorted)")
    print(f"base_dt        : {base_dt}")
    print(f"interval       : {args.interval}s   budget: {args.max_calls}")
    print(f"live_confirmed : {args.confirm_live_read}")

    if not args.confirm_live_read:
        print("\nDRY RUN — no network call made. Pass --confirm-live-read to execute.")
        return 0

    live = KiwoomLiveReadOnlyClient.from_app_settings()
    # Chart TRs ignore the account number; the scoped env has none, so an empty
    # string is passed rather than sourcing one from anywhere.
    mock = KiwoomMockClient(
        base_url=constants.MOCK_BASE_URL,
        app_key=creds["KIWOOM_MOCK_APP_KEY"],
        app_secret=creds["KIWOOM_MOCK_APP_SECRET"],
        account_no="",
    )

    def mock_chart(api_id: str, body: dict[str, Any]):
        # Belt and braces: this script may only ever ask mock for chart TRs.
        assert api_id in ALLOWED_API_IDS, f"non-chart api_id blocked: {api_id}"

        async def call():
            return await mock.post_api(api_id=api_id, path=CHART_PATH, body=body)

        return call

    pacer = Pacer(min_interval=args.interval, max_calls=args.max_calls)
    frozen = load_frozen_kis_sample()
    results: list[dict[str, Any]] = []

    for index, symbol in enumerate(symbols, start=1):
        market = "kospi" if symbol in KOSPI_SYMBOLS else "kosdaq"
        print(f"[{index:2d}/{len(symbols)}] {symbol} ({market})", flush=True)
        row: dict[str, Any] = {"symbol": symbol, "market": market}

        for kind, live_fn, mock_body in (
            (
                ChartKind.DAILY,
                lambda s=symbol: live.fetch_daily_chart(symbol=s, base_dt=base_dt),
                {"stk_cd": symbol, "base_dt": base_dt, "upd_stkpc_tp": "1"},
            ),
            (
                ChartKind.MINUTE,
                lambda s=symbol: live.fetch_minute_chart(
                    symbol=s, tic_scope=MINUTE_TIC_SCOPE
                ),
                {
                    "stk_cd": symbol,
                    "tic_scope": MINUTE_TIC_SCOPE,
                    "upd_stkpc_tp": "1",
                },
            ),
        ):
            api_id = (
                constants.CHART_DAILY_API_ID
                if kind is ChartKind.DAILY
                else constants.CHART_MINUTE_API_ID
            )
            mock_payload, mock_err = await _timed(
                pacer, "mock", f"{symbol}:{kind.name}", mock_chart(api_id, mock_body)
            )
            live_payload, live_err = await _timed(
                pacer, "live", f"{symbol}:{kind.name}", live_fn
            )

            entry: dict[str, Any] = {}
            if mock_err or live_err:
                entry["error"] = {
                    "mock": type(mock_err).__name__ if mock_err else None,
                    "live": type(live_err).__name__ if live_err else None,
                }
                row[kind.name.lower()] = entry
                continue

            comparison = compare_chart_payloads(
                symbol=symbol,
                kind=kind,
                mock_payload=mock_payload,
                live_payload=live_payload,
            )
            mock_rows = extract_rows(mock_payload, kind)
            live_rows = extract_rows(live_payload, kind)

            # The session is open while this runs, so the newest bar is still
            # forming and legitimately differs between two fetches ~2s apart.
            # Report both the raw rate and the rate excluding that bar.
            newest = max(
                (str(r.get(kind.key_field, "")) for r in mock_rows + live_rows),
                default="",
            )
            stale_mismatches = [m for m in comparison.mismatches if m.row_key != newest]
            compared_cells = len(comparison.common_row_keys) * max(
                len(comparison.compared_fields), 1
            )

            entry = {
                "mock_rows": comparison.mock_row_count,
                "live_rows": comparison.live_row_count,
                "common_rows": len(comparison.common_row_keys),
                "only_mock": len(comparison.keys_only_in_mock),
                "only_live": len(comparison.keys_only_in_live),
                "compared_fields": list(comparison.compared_fields),
                "compared_cells": compared_cells,
                "mismatch_cells": len(comparison.mismatches),
                "mismatch_cells_excl_newest_bar": len(stale_mismatches),
                "newest_bar_key": newest,
                "mismatched_rows": list(comparison.mismatched_row_keys),
                "mock_return_code": comparison.mock_return_code,
                "live_return_code": comparison.live_return_code,
                "sample_mismatches": [
                    {
                        "row": m.row_key,
                        "field": m.field_name,
                        "mock": m.mock_raw,
                        "live": m.live_raw,
                    }
                    for m in comparison.mismatches[:5]
                ],
            }
            if comparison.mismatches:
                entry["adjudication"] = [
                    {k: (v.value if hasattr(v, "value") else v) for k, v in a.items()}
                    for a in adjudicate_mismatches(
                        comparison=comparison,
                        mock_payload=mock_payload,
                        live_payload=live_payload,
                        frozen=frozen,
                    )
                ]
            if kind is ChartKind.DAILY and live_rows:
                entry["live_latest"] = {
                    "dt": live_rows[0].get("dt"),
                    "trde_prica_mn_krw": live_rows[0].get("trde_prica"),
                }
            row[kind.name.lower()] = entry

        results.append(row)

    # Delisted control — live only, 1 call.
    delisted_payload, delisted_err = await _timed(
        pacer,
        "live",
        "051170:DELISTED",
        lambda: live.fetch_daily_chart(symbol="051170", base_dt=base_dt),
    )
    if delisted_err:
        delisted = {"status": "ERROR", "error": type(delisted_err).__name__}
    else:
        rows = extract_rows(delisted_payload, ChartKind.DAILY)
        delisted = {
            "status": "FOUND" if rows else "EMPTY",
            "rows": len(rows),
            "return_code": delisted_payload.get("return_code"),
        }

    # Bounded rate-limit probe on the MOCK host only (the safer of the two).
    probe: list[dict[str, Any]] = []
    for interval in (2.0, 1.0, 0.5, 0.2, 0.05):
        payload, err = await _timed(
            pacer,
            "mock",
            f"probe@{interval}",
            mock_chart(
                constants.CHART_DAILY_API_ID,
                {"stk_cd": "005930", "base_dt": base_dt, "upd_stkpc_tp": "1"},
            ),
            interval=interval,
        )
        probe.append(
            {
                "interval_s": interval,
                "ok": err is None,
                "error": type(err).__name__ if err else None,
                "return_code": None if err else payload.get("return_code"),
            }
        )

    report = {
        "base_dt": base_dt,
        "symbols": symbols,
        "results": results,
        "delisted_051170": delisted,
        "rate_limit_probe_mock_only": probe,
        "observations": pacer.observations,
        "calls": {
            "total": pacer.calls,
            "live": pacer.live_calls,
            "mock": pacer.mock_calls,
            "budget": pacer.max_calls,
        },
    }
    out = Path(args.out)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"\ncalls: total={pacer.calls} live={pacer.live_calls} mock={pacer.mock_calls}"
    )
    print(f"delisted 051170: {delisted}")
    print(f"written: {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--base-dt", default=None)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--max-calls", type=int, default=200)
    parser.add_argument("--out", default="/tmp/kiwoom_live_compare.json")
    parser.add_argument(
        "--redis-url",
        default=None,
        help=(
            "Redis for the OAuth token cache. Point at a throwaway instance to "
            "keep the deployment's shared cache untouched."
        ),
    )
    parser.add_argument(
        "--confirm-live-read",
        action="store_true",
        help="required for any network call; without it this is a dry run",
    )
    args = parser.parse_args()
    if args.base_dt is None:
        import datetime as dt

        args.base_dt = (
            dt.datetime.now(dt.UTC)
            .astimezone(dt.timezone(dt.timedelta(hours=9)))
            .strftime("%Y%m%d")
        )
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
