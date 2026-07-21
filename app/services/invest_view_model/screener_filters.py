"""ROB-439 MVP (foundation): a single adjustable-filter schema over the screener
*base snapshots*.

The /invest/screener filters were fragmented across three layers (display-only
filterChips, the `_SCREENING_FILTERS` kwargs dict, and hardcoded loader WHERE
constants), so neither a Toss-style chip editor nor the `screen_stocks` MCP tool
could adjust thresholds. This module is the shared core: one filter definition,
keyed to a base-snapshot column, that drives the UI chips, the loader predicates,
and the MCP filter input.

Model (locked decisions, see ROB-439):
- **AND-only** stacking — a filter set is a conjunction of (field, operator, value)
  conditions over snapshot columns. Boolean groups (OR) are a follow-up.
- A preset is just a *starting* filter set over its base snapshot; users / the MCP
  add or adjust conditions on top (this is the "필터를 추가/조정" model).
- Fail-closed: a row whose field is NULL or non-numeric is excluded, never passed.

This PR ships the schema + per-snapshot field catalog + the pure apply/validate/
merge helpers + the two pilot presets (consecutive_gainers, high_yield_value).
Wiring it into the loaders + the screen_stocks MCP tool is the follow-up PR.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

FilterOperator = Literal["gte", "lte", "eq"]
FilterValueType = Literal["int", "float", "percent"]
# range is expressed as two conditions (a gte + an lte) rather than a dedicated op,
# keeping apply/validate trivial for the MVP.

_ALLOWED_OPERATORS: frozenset[str] = frozenset({"gte", "lte", "eq"})


@dataclass(frozen=True)
class ScreenerFilterDefinition:
    """A single adjustable filter, keyed to a base-snapshot column.

    Drives the UI chip (label/unit/step/bounds), the loader predicate (field +
    operator), and validation of MCP overrides (bounds clamp). ``default`` is the
    preset's starting value for this field (None = not part of the starting set).
    """

    field: (
        str  # base-snapshot column / row-dict key (e.g. "per", "consecutive_up_days")
    )
    label: str  # Korean chip label (e.g. "PER", "연속상승일")
    operator: FilterOperator  # the preset's default comparison for this field
    value_type: FilterValueType
    default: float | int | None = None
    min_bound: float | int | None = None
    max_bound: float | int | None = None
    step: float | int | None = None
    unit: str | None = None  # "일" / "%" / "배" / "원"


@dataclass(frozen=True)
class ScreenerFilterCondition:
    """A concrete applied condition: row[field] <op> value (AND-combined)."""

    field: str
    operator: FilterOperator
    value: float | int


# --- per-base-snapshot filterable-field catalog -------------------------------
# Keyed by base-snapshot kind. The field is the ROW-DICT key the loader emits, so
# apply_filter_conditions can run over loaded rows. Each entry lists the columns a
# user / the MCP may filter on, with the chip metadata + bounds. Extend per snapshot
# as more presets migrate (ROB-439 follow-ups); MVP catalogs the 2 pilot snapshots.
SNAPSHOT_FILTER_FIELDS: dict[str, dict[str, ScreenerFilterDefinition]] = {
    # consecutive_gainers / oversold_recovery / volume presets (KIS OHLCV snapshot)
    "invest_screener_snapshots": {
        "consecutive_up_days": ScreenerFilterDefinition(
            field="consecutive_up_days",
            label="연속상승일",
            operator="gte",
            value_type="int",
            default=5,
            min_bound=2,
            max_bound=20,
            step=1,
            unit="일",
        ),
        "week_change_rate": ScreenerFilterDefinition(
            field="week_change_rate",
            label="주가등락률(1주)",
            operator="gte",
            value_type="percent",
            default=0.0,
            min_bound=-50.0,
            max_bound=100.0,
            step=1.0,
            unit="%",
        ),
        "change_rate": ScreenerFilterDefinition(
            field="change_rate",
            label="주가등락률(당일)",
            operator="gte",
            value_type="percent",
            min_bound=-30.0,
            max_bound=30.0,
            step=1.0,
            unit="%",
        ),
        "volume": ScreenerFilterDefinition(
            field="volume",
            label="거래량",
            operator="gte",
            value_type="int",
            min_bound=0,
            step=1000,
            unit="주",
        ),
        "rsi": ScreenerFilterDefinition(
            field="rsi",
            label="RSI",
            operator="lte",
            value_type="float",
            min_bound=0.0,
            max_bound=100.0,
            step=1.0,
        ),
    },
    # high_yield_value (Naver/Yahoo valuation snapshot)
    "market_valuation_snapshots": {
        "roe": ScreenerFilterDefinition(
            field="roe",
            label="ROE",
            operator="gte",
            value_type="percent",
            default=15.0,
            min_bound=0.0,
            max_bound=100.0,
            step=1.0,
            unit="%",
        ),
        "per": ScreenerFilterDefinition(
            field="per",
            label="PER",
            operator="lte",
            value_type="float",
            default=10.0,
            min_bound=0.0,
            max_bound=100.0,
            step=0.5,
            unit="배",
        ),
        "pbr": ScreenerFilterDefinition(
            field="pbr",
            label="PBR",
            operator="lte",
            value_type="float",
            min_bound=0.0,
            max_bound=20.0,
            step=0.1,
            unit="배",
        ),
        "dividend_yield": ScreenerFilterDefinition(
            field="dividend_yield",
            label="배당수익률",
            operator="gte",
            value_type="percent",
            min_bound=0.0,
            max_bound=30.0,
            step=0.5,
            unit="%",
        ),
    },
    # ROB-443: crypto screener snapshot (tvscreener_upbit + USD-M perp derivatives).
    # Composing filters added ON TOP of a preset's base (the preset's defining
    # filter runs in SQL; these tighten/add in-memory — see screener_service crypto
    # dispatch). Liquidity floor (trade_amount_24h) is the most broadly useful.
    "invest_crypto_screener_snapshots": {
        "trade_amount_24h": ScreenerFilterDefinition(
            field="trade_amount_24h",
            label="거래대금(24h)",
            operator="gte",
            value_type="int",
            min_bound=0,
            step=1_000_000_000,
            unit="원",
        ),
        "rsi": ScreenerFilterDefinition(
            field="rsi",
            label="RSI",
            operator="lte",
            value_type="float",
            min_bound=0.0,
            max_bound=100.0,
            step=1.0,
        ),
        "change_rate": ScreenerFilterDefinition(
            field="change_rate",
            label="등락률(당일)",
            operator="gte",
            value_type="percent",
            min_bound=-30.0,
            max_bound=30.0,
            step=1.0,
            unit="%",
        ),
        "oi_change_24h": ScreenerFilterDefinition(
            field="oi_change_24h",
            label="미결제약정 변화(24h)",
            operator="gte",
            value_type="percent",
            min_bound=-100.0,
            max_bound=500.0,
            step=1.0,
            unit="%",
        ),
        "long_short_account_ratio": ScreenerFilterDefinition(
            field="long_short_account_ratio",
            label="롱숏비율(리테일)",
            operator="gte",
            value_type="float",
            min_bound=0.0,
            max_bound=10.0,
            step=0.1,
        ),
        "funding_rate": ScreenerFilterDefinition(
            field="funding_rate",
            label="펀딩비(비율)",
            operator="lte",
            value_type="float",
            min_bound=-1.0,
            max_bound=1.0,
            step=0.0001,
        ),
    },
}

_CRYPTO_SNAPSHOT_KIND = "invest_crypto_screener_snapshots"
_CRYPTO_PRESET_IDS = (
    "crypto_high_volume",
    "crypto_oversold",
    "crypto_momentum",
    "crypto_funding_squeeze",
    "crypto_funding_overheated",
    "crypto_oi_surge",
    "crypto_long_short_skew",
)

# pilot preset → (base snapshot kind, starting filter set). A preset is just the
# starting conditions; users adjust/add on top. Mirrors the current hardcoded
# thresholds (consecutive_gainers min_consecutive_up_days=5/week_change_rate>=0;
# high_yield_value roe>=15/per 0~10) so derived results match today's behavior.
_PILOT_PRESET_SNAPSHOT: dict[str, str] = {
    "consecutive_gainers": "invest_screener_snapshots",
    # ROB-543: oversold_recovery filters over the same KIS-OHLCV snapshot
    # (read-time RSI14 from closes_window — no RSI column / no migration).
    "oversold_recovery": "invest_screener_snapshots",
    "support_proximity": "invest_screener_snapshots",
    "high_yield_value": "market_valuation_snapshots",
    # ROB-443: crypto presets share one snapshot catalog (composing filters on top).
    **dict.fromkeys(_CRYPTO_PRESET_IDS, _CRYPTO_SNAPSHOT_KIND),
}
_PILOT_PRESET_STARTING: dict[str, tuple[ScreenerFilterCondition, ...]] = {
    "consecutive_gainers": (
        ScreenerFilterCondition("consecutive_up_days", "gte", 5),
        ScreenerFilterCondition("week_change_rate", "gte", 0.0),
    ),
    # ROB-543: oversold_recovery starts at RSI <= 30 (matches _SCREENING_FILTERS).
    "oversold_recovery": (ScreenerFilterCondition("rsi", "lte", 30.0),),
    "high_yield_value": (
        ScreenerFilterCondition("roe", "gte", 15.0),
        ScreenerFilterCondition("per", "lte", 10.0),
    ),
}


def snapshot_kind_for_preset(preset_id: str) -> str | None:
    """Base snapshot a (pilot) preset filters over, or None if not yet migrated."""
    return _PILOT_PRESET_SNAPSHOT.get(preset_id)


def preset_starting_filters(preset_id: str) -> list[ScreenerFilterCondition]:
    """The preset's starting filter set (empty for '직접 만들기' / unmigrated)."""
    return list(_PILOT_PRESET_STARTING.get(preset_id, ()))


class ScreenerFilterError(ValueError):
    """Raised when a requested filter field/operator is not allowed for a snapshot."""


def validate_conditions(
    conditions: list[ScreenerFilterCondition], *, snapshot_kind: str
) -> list[ScreenerFilterCondition]:
    """Validate against the snapshot's field catalog + clamp values to bounds.

    Fail-closed: an unknown snapshot, field, or operator raises ScreenerFilterError
    (never silently dropped or applied). Values outside a definition's min/max are
    clamped (the UI step/bounds keep the user inside the valid range).
    """
    catalog = SNAPSHOT_FILTER_FIELDS.get(snapshot_kind)
    if catalog is None:
        raise ScreenerFilterError(f"unknown snapshot kind: {snapshot_kind!r}")
    out: list[ScreenerFilterCondition] = []
    for c in conditions:
        if c.operator not in _ALLOWED_OPERATORS:
            raise ScreenerFilterError(f"unsupported operator: {c.operator!r}")
        definition = catalog.get(c.field)
        if definition is None:
            raise ScreenerFilterError(
                f"field {c.field!r} not filterable on {snapshot_kind}"
            )
        value: float | int = c.value
        if definition.min_bound is not None and value < definition.min_bound:
            value = definition.min_bound
        if definition.max_bound is not None and value > definition.max_bound:
            value = definition.max_bound
        out.append(ScreenerFilterCondition(c.field, c.operator, value))
    return out


def merge_filter_overrides(
    base: list[ScreenerFilterCondition],
    overrides: list[ScreenerFilterCondition],
) -> list[ScreenerFilterCondition]:
    """Merge user overrides onto a preset's starting set.

    An override with the same (field, operator) replaces the base condition; a new
    (field, operator) is appended (adds a filter). Order: base order preserved,
    then any genuinely new conditions. This is the "필터를 추가/조정" merge.
    """
    by_key: dict[tuple[str, str], ScreenerFilterCondition] = {
        (c.field, c.operator): c for c in base
    }
    appended: list[ScreenerFilterCondition] = []
    for o in overrides:
        key = (o.field, o.operator)
        if key in by_key:
            by_key[key] = o  # adjust existing
        else:
            appended.append(o)  # add new
    return list(by_key.values()) + appended


def _passes(row: dict[str, Any], cond: ScreenerFilterCondition) -> bool:
    raw = row.get(cond.field)
    if raw is None:
        return False  # fail-closed: missing field never passes
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False
    if cond.operator == "gte":
        return value >= cond.value
    if cond.operator == "lte":
        return value <= cond.value
    if cond.operator == "eq":
        return value == cond.value
    return False


def apply_filter_conditions(
    rows: list[dict[str, Any]], conditions: list[ScreenerFilterCondition]
) -> list[dict[str, Any]]:
    """Return rows satisfying ALL conditions (AND). Fail-closed on NULL/non-numeric.

    Pure over loaded snapshot rows (row-dict keys = catalog fields), so the same
    conditions drive the loader display path and the screen_stocks MCP tool.
    """
    if not conditions:
        return list(rows)
    return [row for row in rows if all(_passes(row, c) for c in conditions)]


def consecutive_gainers_loader_thresholds(
    conditions: list[ScreenerFilterCondition],
) -> dict[str, float | int]:
    """Map a consecutive_gainers filter set → snapshot-loader WHERE kwargs.

    Only this pilot loader's own SQL columns translate to WHERE thresholds:
    ``consecutive_up_days`` (>=) and ``week_change_rate`` (>=). Other conditions
    are ignored here — the loader has no column for them (those would post-filter).
    Returns kwargs for ``_load_consecutive_gainers_from_snapshots`` so a loosened
    threshold (e.g. days>=3) actually reaches the snapshot query, not just a
    post-filter that can never widen the loader's own predicate.
    """
    out: dict[str, float | int] = {}
    for c in conditions:
        if c.operator != "gte":
            continue
        if c.field == "consecutive_up_days":
            out["min_consecutive_up_days"] = int(c.value)
        elif c.field == "week_change_rate":
            out["min_week_change_rate"] = float(c.value)
    return out
