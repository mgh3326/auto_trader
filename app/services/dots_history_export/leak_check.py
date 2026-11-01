"""The export's fail-closed gate: closed columns, closed shapes, leak patterns.

The check reports where a violation is and which rule it broke, never the value.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import asdict, dataclass

from app.services.dots_history_export.spec import Column, Kind, TableSpec

#: Shortest identifier compared by substring. A shorter one would match inside
#: legitimate labels, so it is compared against the whole cell instead.
MIN_FORBIDDEN_VALUE_CHARS = 6

#: A tag column carries a short ASCII label. Spaces and non-ASCII mean prose or a
#: name, which is free text the gate cannot judge, so they never pass.
TAG_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+/\-]{0,63}")
#: A label's digit run this long could be a short order number.
LABEL_DIGIT_RUN = re.compile(r"\d{5,}")
HASHED_TAG_SHAPE = re.compile(r"oth_[0-9a-f]{32}")
SYMBOL_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9./\-]{0,23}")
DECIMAL_SHAPE = re.compile(r"-?(?:0|[1-9][0-9]*)(?P<fraction>\.[0-9]+)?")
#: An integer column holds a small count or ordinal (revision, rung index,
#: fill_seq). Seven or more digits is shaped like an order or account number.
INTEGER_SHAPE = re.compile(r"-?(?:0|[1-9][0-9]{0,5})")
UTC_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")
LOCAL_SHAPE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?[+-]\d{2}:\d{2}"
)
DATE_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: An integer this long with no fractional part is shaped like an order or
#: account number, not a quantity. Prices and quantities keep their DB scale.
MAX_BARE_INTEGER_DIGITS = 9

LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("account_number", re.compile(r"(?<!\d)\d{8}-\d{2}(?!\d)")),
    ("account_number", re.compile(r"(?<!\d)\d{3,6}-\d{2,6}-\d{4,8}(?!\d)")),
    ("long_digit_run", re.compile(r"\d{8,}")),
    (
        "uuid",
        re.compile(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        ),
    ),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{6,}")),
    (
        "token_prefix",
        re.compile(
            r"(?i)\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|glpat|xox[abprs]|ya29)"
            r"[-_.][A-Za-z0-9]{16,}"
        ),
    ),
    ("token_prefix", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+\S+")),
    (
        "credential_assignment",
        re.compile(r"(?i)(?:api[_-]?key|secret|token|passw(?:or)?d)\s*[:=]\s*\S"),
    ),
    ("hex_secret", re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{16,}(?![0-9a-f])")),
    ("long_opaque_run", re.compile(r"[A-Za-z0-9+/=]{32,}")),
)
LEAK_RULE_NAMES = tuple(sorted({name for name, _ in LEAK_PATTERNS}))


class LeakCheckFailed(Exception):
    def __init__(self, violations: Iterable[Violation]) -> None:
        self.violations = tuple(violations)
        super().__init__(f"leak check failed: {len(self.violations)} violation(s)")


@dataclass(frozen=True)
class Violation:
    table: str
    column: str
    row: int
    rule: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _identifier_rule(value: str) -> str | None:
    for name, pattern in LEAK_PATTERNS:
        if pattern.search(value):
            return name
    return None


def is_safe_label(value: str) -> bool:
    """Whether a label may be copied as is; anything else must be hashed."""
    return (
        TAG_SHAPE.fullmatch(value) is not None
        and LABEL_DIGIT_RUN.search(value) is None
        and _identifier_rule(value) is None
    )


def _shape_rule(column: Column, value: str) -> str | None:
    kind = column.kind
    if kind is Kind.REF:
        prefix = column.ref_prefix or ""
        if re.fullmatch(rf"{prefix}_[0-9a-f]{{32}}", value) is None:
            return "shape_ref"
    elif kind is Kind.DECIMAL:
        match = DECIMAL_SHAPE.fullmatch(value)
        if match is None:
            return "shape_decimal"
        if (
            match.group("fraction") is None
            and len(value.lstrip("-")) > MAX_BARE_INTEGER_DIGITS
        ):
            return "order_id_shaped_number"
    elif kind is Kind.INTEGER:
        if INTEGER_SHAPE.fullmatch(value) is None:
            return "shape_integer"
    elif kind is Kind.BOOLEAN:
        if value not in ("true", "false"):
            return "shape_boolean"
    elif kind is Kind.TS_UTC:
        if UTC_SHAPE.fullmatch(value) is None:
            return "shape_ts_utc"
    elif kind is Kind.TS_LOCAL:
        if LOCAL_SHAPE.fullmatch(value) is None:
            return "shape_ts_local"
    elif kind is Kind.DATE:
        if DATE_SHAPE.fullmatch(value) is None:
            return "shape_date"
    elif kind is Kind.ENUM:
        if value not in (column.values or frozenset()):
            return "value_not_in_enum"
    elif kind is Kind.SYMBOL:
        if SYMBOL_SHAPE.fullmatch(value) is None:
            return "shape_symbol"
        if re.search(r"\d{7,}", value):
            return "long_digit_run"
    elif kind is Kind.TAG:
        if TAG_SHAPE.fullmatch(value) is None:
            return "shape_tag"
    return None


def _forbidden_variants(values: Iterable[str]) -> frozenset[str]:
    variants: set[str] = set()
    for value in values:
        text = value.strip().casefold()
        variants.update(item for item in (text, text.lstrip("0")) if item)
    return frozenset(variants)


class _CellScanner:
    """Memoized per distinct free-form value, since most tags repeat."""

    def __init__(self, forbidden: Collection[str]) -> None:
        variants = _forbidden_variants(forbidden)
        self._substrings = frozenset(
            item for item in variants if len(item) >= MIN_FORBIDDEN_VALUE_CHARS
        )
        self._whole_cells = variants - self._substrings
        # A numeric cell is a quantity, price or amount, so only an exact match
        # with a long all-digit identifier (an account or order number written
        # without separators) is refused. Substrings would refuse real amounts.
        self._numeric = frozenset(item for item in self._substrings if item.isdigit())
        self._cache: dict[tuple[Kind, str], str | None] = {}

    def _pattern_rule(self, kind: Kind, value: str) -> str | None:
        if kind is Kind.TAG and HASHED_TAG_SHAPE.fullmatch(value):
            return None
        rule = _identifier_rule(value)
        if rule is not None:
            return rule
        if kind is Kind.TAG and LABEL_DIGIT_RUN.search(value):
            return "long_digit_run"
        folded = value.casefold()

        # A KR symbol can equal a short numeric order number by coincidence, and
        # the symbol's shape already refuses a longer digit run.
        def applies(item: str) -> bool:
            return kind is Kind.TAG or not item.isdigit()

        if folded in self._whole_cells and applies(folded):
            return "forbidden_value"
        if any(item in folded and applies(item) for item in self._substrings):
            return "forbidden_value"
        return None

    def rule_for(self, column: Column, value: str) -> str | None:
        shape = _shape_rule(column, value)
        if shape is not None:
            return shape
        if column.kind in (Kind.DECIMAL, Kind.INTEGER):
            digits = value.lstrip("-")
            if digits in self._numeric or digits.lstrip("0") in self._numeric:
                return "forbidden_value"
            return None
        if column.kind not in (Kind.TAG, Kind.SYMBOL):
            return None
        key = (column.kind, value)
        if key not in self._cache:
            self._cache[key] = self._pattern_rule(column.kind, value)
        return self._cache[key]


def scan_table(
    spec: TableSpec,
    columns: Iterable[str],
    rows: Iterable[Mapping[str, str | None]],
    *,
    forbidden: Collection[str] = (),
) -> list[Violation]:
    violations: list[Violation] = []
    allowed = spec.column_names
    found = tuple(columns)
    for name in found:
        if name not in allowed:
            violations.append(Violation(spec.name, name, 0, "column_not_allowlisted"))
    for name in allowed:
        if name not in found:
            violations.append(Violation(spec.name, name, 0, "column_missing"))
    by_name = {column.name: column for column in spec.columns}
    scanner = _CellScanner(forbidden)
    for index, row in enumerate(rows, start=1):
        for name, value in row.items():
            column = by_name.get(name)
            if column is None:
                violations.append(
                    Violation(spec.name, name, index, "column_not_allowlisted")
                )
                continue
            if value is None or value == "":
                continue
            rule = scanner.rule_for(column, value)
            if rule is not None:
                violations.append(Violation(spec.name, name, index, rule))
    return violations
