"""Lazy pykrx-only source adapter for the isolated research collector."""

from __future__ import annotations

import contextlib
import importlib.metadata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CorpusConfig
from .pacing import RequestBudgetExceeded, RequestPacer
from .redaction import (
    CredentialPreconditionError,
    Redactor,
    dedicated_credential_environment,
    discard_redacted_source_output,
    load_dedicated_credentials,
)


class SourceCallFailure(RuntimeError):
    """A pykrx call failed; the message is redacted before persistence."""


class SourceBlockedSignal(RuntimeError):
    """The source returned a rate-limit or access-block status."""


@dataclass(frozen=True)
class LifecycleMasterBound:
    """Official KRX lifecycle-master budget upper bound, not membership data."""

    tickers: frozenset[str]
    listed_count: int
    delisted_count: int


def normalize_ticker(value: object) -> str:
    text = str(value).strip()
    if text.isdecimal() and len(text) < 6:
        return text.zfill(6)
    return text


class PykrxSource:
    """A single-process source adapter with zero fallbacks and zero retries."""

    def __init__(self, config: CorpusConfig, pacer: RequestPacer) -> None:
        self.config = config
        self.pacer = pacer
        self.redactor: Redactor | None = None
        self._stock: Any | None = None
        self._stock_ticker_class: Any | None = None

    @contextlib.contextmanager
    def runtime(self) -> Iterator[PykrxSource]:
        """Load dedicated credentials and suppress pykrx's leaky output."""
        try:
            credentials, redactor = load_dedicated_credentials(
                Path(self.config.env_file)
            )
        except CredentialPreconditionError:
            raise

        try:
            installed_version = importlib.metadata.version("pykrx")
        except importlib.metadata.PackageNotFoundError as exc:
            raise CredentialPreconditionError(
                "pykrx is unavailable in VENV_DIR"
            ) from exc
        if installed_version != self.config.pykrx_version:
            raise CredentialPreconditionError(
                "pykrx version in VENV_DIR does not match the signed configuration"
            )

        self.redactor = redactor
        with (
            dedicated_credential_environment(credentials),
            self.pacer.patch_requests(),
            discard_redacted_source_output(redactor),
        ):
            # Import must happen after credentials and output capture are in
            # force because pykrx initializes/authenticates its KRX session at
            # module load time.
            from pykrx import stock
            from pykrx.website.krx.market.ticker import StockTicker

            self._stock = stock
            self._stock_ticker_class = StockTicker
            try:
                yield self
            finally:
                self._stock = None
                self._stock_ticker_class = None

    def _require_runtime(self) -> tuple[Any, Any]:
        if self._stock is None or self._stock_ticker_class is None:
            raise RuntimeError("pykrx source was used outside its runtime context")
        return self._stock, self._stock_ticker_class

    def _call(self, label: str, operation: Callable[[], Any]) -> Any:
        try:
            result = operation()
        except RequestBudgetExceeded:
            raise
        except Exception as exc:
            redactor = self.redactor or Redactor(())
            raise SourceCallFailure(f"{label}: {redactor.redact(exc)}") from None
        if self.pacer.blocked_signal_seen:
            statuses = ",".join(str(status) for status in self.pacer.blocked_statuses)
            raise SourceBlockedSignal(
                f"{label}: source access/rate-limit status observed ({statuses})"
            )
        return result

    def lifecycle_master_bound(self) -> LifecycleMasterBound:
        """Fetch the KRX listed+delisted master solely as a request upper bound.

        It is never used to infer historical membership, common-stock status,
        or inclusion in the corpus.  Historic inclusion is always determined
        later by positive ``get_market_ticker_list(session, market)`` output.
        """
        _, stock_ticker_class = self._require_runtime()

        def fetch() -> Any:
            return stock_ticker_class()

        master = self._call("lifecycle_master_bound", fetch)
        listed = master.listed
        delisted = master.delisted
        allowed_markets = {"STK", "KSQ"}
        listed_tickers = {
            normalize_ticker(ticker)
            for ticker, row in listed.iterrows()
            if str(row["시장"]) in allowed_markets
        }
        delisted_tickers = {
            normalize_ticker(ticker)
            for ticker, row in delisted.iterrows()
            if str(row["시장"]) in allowed_markets
        }
        return LifecycleMasterBound(
            tickers=frozenset(listed_tickers | delisted_tickers),
            listed_count=len(listed_tickers),
            delisted_count=len(delisted_tickers),
        )

    def session_calendar(self) -> tuple[str, ...]:
        stock, _ = self._require_runtime()
        dates = self._call(
            "session_calendar",
            lambda: stock.get_previous_business_days(
                fromdate=self.config.source_start,
                todate=self.config.source_cutoff,
            ),
        )
        return tuple(timestamp.strftime("%Y-%m-%d") for timestamp in dates)

    def membership(self, session: str, market: str) -> tuple[str, ...]:
        stock, _ = self._require_runtime()
        source_session = session.replace("-", "")
        tickers = self._call(
            f"membership:{session}:{market}",
            lambda: stock.get_market_ticker_list(source_session, market),
        )
        return tuple(normalize_ticker(ticker) for ticker in tickers)

    def adjusted_ohlcv(self, ticker: str) -> Any:
        stock, _ = self._require_runtime()
        return self._call(
            f"ohlcv:{ticker}",
            lambda: stock.get_market_ohlcv_by_date(
                self.config.source_start,
                self.config.source_cutoff,
                ticker,
                freq="d",
                adjusted=True,
            ),
        )
