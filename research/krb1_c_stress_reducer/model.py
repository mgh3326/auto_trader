"""Fail-closed input parsing for the sealed KR-B1c reducer contract."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

RATE_SCALE = 1_000_000_000_000
PARENT_CANONICAL_SHA256 = (
    "d5e1246b2072ad227d924e059091e27fa49719e42a5bfba651ae8f2bced9d6f1"
)
AMENDMENT_CANONICAL_SHA256 = (
    "d5da5edd6b49fb759b781c13f627e21a84667ced2be7cf03624a40bb813be389"
)
MARKETS = ("KOSPI", "KOSDAQ")
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
KST = timezone(timedelta(hours=9))


class ContractError(ValueError):
    """The sealed reducer contract cannot be satisfied."""


@dataclass(frozen=True)
class CostRecord:
    market: str
    broker_id: str
    account_product_id: str
    order_channel_id: str
    cost_basis: str
    rate_upper_bound_scope: str
    effective_from: date
    effective_to: date | None
    buy_commission_rate_e12: int
    sell_commission_rate_e12: int
    sell_tax_components: tuple[tuple[str, int], ...]
    source_locator: str
    source_snapshot_sha256: str
    probe_reconciliation_status: str
    mock_cost_relation: str

    @property
    def sell_tax_rate_e12(self) -> int:
        return sum(rate for _, rate in self.sell_tax_components)


@dataclass(frozen=True)
class MarketRates:
    buy_commission_rate_e12: int
    sell_commission_rate_e12: int
    sell_tax_rate_e12: int

    @property
    def buy_commission(self) -> Fraction:
        return Fraction(self.buy_commission_rate_e12, RATE_SCALE)

    @property
    def sell_commission(self) -> Fraction:
        return Fraction(self.sell_commission_rate_e12, RATE_SCALE)

    @property
    def sell_tax(self) -> Fraction:
        return Fraction(self.sell_tax_rate_e12, RATE_SCALE)


@dataclass(frozen=True)
class CostInputs:
    raw: dict[str, Any]
    completed_at_kst: datetime
    coverage_start: date
    coverage_end: date
    records: tuple[CostRecord, ...]
    rates: dict[str, MarketRates]


@dataclass(frozen=True)
class TickBand:
    lower: int
    upper_exclusive: int | None
    tick: int


@dataclass(frozen=True)
class MarketTickTable:
    market: str
    table_id: str
    instrument_scope: str
    source_locator: str
    source_snapshot_sha256: str
    bands: tuple[TickBand, ...]

    def tick(self, price: int) -> int:
        if isinstance(price, bool) or not isinstance(price, int) or price < 0:
            raise ContractError("tick lookup price must be a nonnegative integer")
        for band in self.bands:
            if price >= band.lower and (
                band.upper_exclusive is None or price < band.upper_exclusive
            ):
                return band.tick
        raise ContractError(f"{self.market} tick table has no band for {price}")

    def tick_ceil(self, value: Fraction) -> int:
        if not isinstance(value, Fraction):
            raise ContractError("tick_ceil requires fractions.Fraction")
        if value < 0:
            raise ContractError("tick_ceil value must be nonnegative")
        integer_ceiling = (value.numerator + value.denominator - 1) // value.denominator
        for band in self.bands:
            candidate = max(integer_ceiling, band.lower)
            aligned = ((candidate + band.tick - 1) // band.tick) * band.tick
            if band.upper_exclusive is None or aligned < band.upper_exclusive:
                return aligned
        raise ContractError(f"{self.market} tick table is not open-ended")


@dataclass(frozen=True)
class TickTables:
    raw: dict[str, Any]
    markets: dict[str, MarketTickTable]


def _reject_float(_: str) -> None:
    raise ContractError("JSON floating-point numbers are prohibited")


def load_json_exact(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read exact JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return value


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _require_exact_keys(
    value: dict[str, Any], required: set[str], context: str
) -> None:
    actual = set(value)
    if actual != required:
        missing = sorted(required - actual)
        extra = sorted(actual - required)
        raise ContractError(
            f"{context} keys mismatch: missing={missing}, extra={extra}"
        )


def _require_nonempty_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{context} must be a nonempty string")
    return value


def _require_hash(value: object, context: str) -> str:
    text = _require_nonempty_string(value, context)
    if HEX_64.fullmatch(text) is None:
        raise ContractError(f"{context} must be 64 lowercase hexadecimal characters")
    return text


def _require_rate(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{context} must be a nonnegative integer rate_e12")
    return value


def _parse_date(value: object, context: str) -> date:
    text = _require_nonempty_string(value, context)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ContractError(f"{context} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ContractError(f"{context} must use canonical YYYY-MM-DD")
    return parsed


def _parse_completed_at(value: object) -> datetime:
    text = _require_nonempty_string(value, "p0_2_completed_at_kst")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractError("p0_2_completed_at_kst must be ISO-8601") from exc
    if parsed.utcoffset() != KST.utcoffset(None):
        raise ContractError("p0_2_completed_at_kst must have the +09:00 KST offset")
    return parsed


def _parse_cost_record(raw: object, index: int) -> CostRecord:
    context = f"market_cost_records[{index}]"
    if not isinstance(raw, dict):
        raise ContractError(f"{context} must be an object")
    required = {
        "market",
        "broker_id",
        "account_product_id",
        "order_channel_id",
        "cost_basis",
        "rate_upper_bound_scope",
        "effective_from",
        "effective_to",
        "buy_commission_rate_e12",
        "sell_commission_rate_e12",
        "sell_tax_components",
        "source_locator",
        "source_snapshot_sha256",
        "probe_reconciliation_status",
        "mock_cost_relation",
    }
    _require_exact_keys(raw, required, context)
    market = _require_nonempty_string(raw["market"], f"{context}.market")
    if market not in MARKETS:
        raise ContractError(f"{context}.market must be KOSPI or KOSDAQ")
    if raw["cost_basis"] != "REAL_TRADING_TARIFF":
        raise ContractError(f"{context}.cost_basis must be REAL_TRADING_TARIFF")
    if raw["probe_reconciliation_status"] != "PASS":
        raise ContractError(f"{context}.probe_reconciliation_status must be PASS")
    if raw["mock_cost_relation"] not in {"EQUAL", "DIFFERENT"}:
        raise ContractError(f"{context}.mock_cost_relation is invalid")
    if raw["broker_id"] != "kiwoom":
        raise ContractError(f"{context}.broker_id must be kiwoom")
    if raw["account_product_id"] != "KIWOOM_DOMESTIC_CASH_STOCK":
        raise ContractError(
            f"{context}.account_product_id does not match the amendment"
        )
    if raw["order_channel_id"] != "KIWOOM_OPENAPI_KRX":
        raise ContractError(f"{context}.order_channel_id does not match the amendment")

    component_values = raw["sell_tax_components"]
    if not isinstance(component_values, list) or not component_values:
        raise ContractError(f"{context}.sell_tax_components must be a nonempty list")
    components: list[tuple[str, int]] = []
    component_codes: set[str] = set()
    for component_index, component in enumerate(component_values):
        component_context = f"{context}.sell_tax_components[{component_index}]"
        if not isinstance(component, dict):
            raise ContractError(f"{component_context} must be an object")
        _require_exact_keys(
            component, {"component_code", "rate_e12"}, component_context
        )
        code = _require_nonempty_string(
            component["component_code"], f"{component_context}.component_code"
        )
        if code in component_codes:
            raise ContractError(f"{context} has duplicate tax component {code}")
        component_codes.add(code)
        components.append(
            (
                code,
                _require_rate(component["rate_e12"], f"{component_context}.rate_e12"),
            )
        )

    effective_to_raw = raw["effective_to"]
    effective_to = (
        None
        if effective_to_raw is None
        else _parse_date(effective_to_raw, f"{context}.effective_to")
    )
    effective_from = _parse_date(raw["effective_from"], f"{context}.effective_from")
    if effective_to is not None and effective_to <= effective_from:
        raise ContractError(f"{context} effective interval must be positive")

    return CostRecord(
        market=market,
        broker_id="kiwoom",
        account_product_id="KIWOOM_DOMESTIC_CASH_STOCK",
        order_channel_id="KIWOOM_OPENAPI_KRX",
        cost_basis="REAL_TRADING_TARIFF",
        rate_upper_bound_scope=_require_nonempty_string(
            raw["rate_upper_bound_scope"], f"{context}.rate_upper_bound_scope"
        ),
        effective_from=effective_from,
        effective_to=effective_to,
        buy_commission_rate_e12=_require_rate(
            raw["buy_commission_rate_e12"],
            f"{context}.buy_commission_rate_e12",
        ),
        sell_commission_rate_e12=_require_rate(
            raw["sell_commission_rate_e12"],
            f"{context}.sell_commission_rate_e12",
        ),
        sell_tax_components=tuple(components),
        source_locator=_require_nonempty_string(
            raw["source_locator"], f"{context}.source_locator"
        ),
        source_snapshot_sha256=_require_hash(
            raw["source_snapshot_sha256"],
            f"{context}.source_snapshot_sha256",
        ),
        probe_reconciliation_status="PASS",
        mock_cost_relation=raw["mock_cost_relation"],
    )


def _validate_record_coverage(
    market: str,
    records: list[CostRecord],
    coverage_start: date,
    coverage_end: date,
) -> None:
    ordered = sorted(records, key=lambda item: item.effective_from)
    if not ordered:
        raise ContractError(f"{market} has no cost record")
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if previous.effective_to is None:
            raise ContractError(
                f"{market} only the final current record may be open-ended"
            )
        if previous.effective_to < current.effective_from:
            raise ContractError(f"{market} authoritative cost records have a date gap")
        if previous.effective_to > current.effective_from:
            raise ContractError(f"{market} authoritative cost records overlap")
    if ordered[-1].effective_to is not None:
        raise ContractError(f"{market} current cost record must be open-ended")
    if ordered[0].effective_from > coverage_start:
        raise ContractError(f"{market} records do not cover coverage_start_session")
    covering_end = next(
        (
            record
            for record in ordered
            if record.effective_from <= coverage_end
            and (record.effective_to is None or coverage_end < record.effective_to)
        ),
        None,
    )
    if covering_end is None:
        raise ContractError(f"{market} records do not cover coverage_end_session")


def load_cost_inputs(path: Path) -> CostInputs:
    raw = load_json_exact(path)
    required = {
        "schema_version",
        "p0_2_completed_at_kst",
        "parent_canonical_sha256",
        "coverage_start_session",
        "coverage_end_session",
        "market_cost_records",
    }
    _require_exact_keys(raw, required, "P0-2 cost input")
    if raw["schema_version"] != "krb1.p0_2_cost_inputs.v1":
        raise ContractError("unexpected P0-2 schema_version")
    parent_hash = _require_hash(
        raw["parent_canonical_sha256"], "parent_canonical_sha256"
    )
    if parent_hash != PARENT_CANONICAL_SHA256:
        raise ContractError("P0-2 parent_canonical_sha256 is not the sealed parent")
    completed_at = _parse_completed_at(raw["p0_2_completed_at_kst"])
    coverage_start = _parse_date(
        raw["coverage_start_session"], "coverage_start_session"
    )
    coverage_end = _parse_date(raw["coverage_end_session"], "coverage_end_session")
    if coverage_end < coverage_start:
        raise ContractError("coverage_end_session precedes coverage_start_session")
    raw_records = raw["market_cost_records"]
    if not isinstance(raw_records, list):
        raise ContractError("market_cost_records must be a list")
    records = tuple(
        _parse_cost_record(raw_record, index)
        for index, raw_record in enumerate(raw_records)
    )

    grouped: dict[str, list[CostRecord]] = {market: [] for market in MARKETS}
    for record in records:
        grouped[record.market].append(record)
    rates: dict[str, MarketRates] = {}
    for market in MARKETS:
        _validate_record_coverage(market, grouped[market], coverage_start, coverage_end)
        rates[market] = MarketRates(
            buy_commission_rate_e12=max(
                record.buy_commission_rate_e12 for record in grouped[market]
            ),
            sell_commission_rate_e12=max(
                record.sell_commission_rate_e12 for record in grouped[market]
            ),
            sell_tax_rate_e12=max(
                record.sell_tax_rate_e12 for record in grouped[market]
            ),
        )
    return CostInputs(
        raw=raw,
        completed_at_kst=completed_at,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        records=records,
        rates=rates,
    )


def _parse_tick_table(market: str, raw: object) -> MarketTickTable:
    context = f"markets.{market}"
    if not isinstance(raw, dict):
        raise ContractError(f"{context} must be an object")
    required = {
        "table_id",
        "instrument_scope",
        "source_locator",
        "source_snapshot_sha256",
        "bands",
    }
    _require_exact_keys(raw, required, context)
    raw_bands = raw["bands"]
    if not isinstance(raw_bands, list) or not raw_bands:
        raise ContractError(f"{context}.bands must be a nonempty list")
    bands: list[TickBand] = []
    for index, raw_band in enumerate(raw_bands):
        band_context = f"{context}.bands[{index}]"
        if not isinstance(raw_band, dict):
            raise ContractError(f"{band_context} must be an object")
        _require_exact_keys(
            raw_band, {"lower", "upper_exclusive", "tick"}, band_context
        )
        lower = raw_band["lower"]
        upper = raw_band["upper_exclusive"]
        tick = raw_band["tick"]
        if isinstance(lower, bool) or not isinstance(lower, int) or lower < 0:
            raise ContractError(f"{band_context}.lower must be nonnegative integer")
        if upper is not None and (
            isinstance(upper, bool) or not isinstance(upper, int) or upper <= lower
        ):
            raise ContractError(
                f"{band_context}.upper_exclusive must exceed lower or be null"
            )
        if isinstance(tick, bool) or not isinstance(tick, int) or tick <= 0:
            raise ContractError(f"{band_context}.tick must be positive integer")
        bands.append(TickBand(lower=lower, upper_exclusive=upper, tick=tick))
    if bands[0].lower != 0:
        raise ContractError(f"{context}.bands must start at zero")
    for previous, current in zip(bands, bands[1:], strict=False):
        if previous.upper_exclusive != current.lower:
            raise ContractError(f"{context}.bands must have no gap or overlap")
    if bands[-1].upper_exclusive is not None:
        raise ContractError(f"{context}.bands must end with an open-ended band")
    if any(band.upper_exclusive is None for band in bands[:-1]):
        raise ContractError(f"{context} only the final band may be open-ended")
    table = MarketTickTable(
        market=market,
        table_id=_require_nonempty_string(raw["table_id"], f"{context}.table_id"),
        instrument_scope=_require_nonempty_string(
            raw["instrument_scope"], f"{context}.instrument_scope"
        ),
        source_locator=_require_nonempty_string(
            raw["source_locator"], f"{context}.source_locator"
        ),
        source_snapshot_sha256=_require_hash(
            raw["source_snapshot_sha256"],
            f"{context}.source_snapshot_sha256",
        ),
        bands=tuple(bands),
    )
    first = table.tick_ceil(Fraction(5_000))
    after_cap = table.tick_ceil(Fraction(400_001))
    if first < 5_000 or after_cap <= 400_000:
        raise ContractError(f"{context} tick_ceil boundary checks failed")
    return table


def load_tick_tables(path: Path) -> TickTables:
    raw = load_json_exact(path)
    required = {
        "schema_version",
        "parent_canonical_sha256",
        "symbol_table_mapping_status",
        "markets",
    }
    _require_exact_keys(raw, required, "P0-1 tick input")
    if raw["schema_version"] != "krb1.reference.tick_tables.v1":
        raise ContractError("unexpected reference tick-table schema_version")
    if raw["parent_canonical_sha256"] != PARENT_CANONICAL_SHA256:
        raise ContractError("tick table input is not bound to the sealed parent")
    if raw["symbol_table_mapping_status"] != "COMPLETE":
        raise ContractError("P0-1 symbol-to-table mapping is not COMPLETE")
    raw_markets = raw["markets"]
    if not isinstance(raw_markets, dict) or set(raw_markets) != set(MARKETS):
        raise ContractError("tick table input must contain exactly KOSPI and KOSDAQ")
    markets = {
        market: _parse_tick_table(market, raw_markets[market]) for market in MARKETS
    }
    return TickTables(raw=raw, markets=markets)
