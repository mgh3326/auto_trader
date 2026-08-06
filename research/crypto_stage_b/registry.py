"""Verbatim-bound registry for the admitted crypto Stage-B candidates.

The upstream return is intentionally Markdown, not a hand-maintained Python
configuration.  This registry reads that exact file, verifies its frozen
SHA-256 first, and derives each candidate contract from the raw candidate
block.  Numerical strategy parameters are therefore never copied into this
module as a second source of truth.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

__all__ = [
    "ADMITTED_STRATEGY_IDS",
    "EXPECTED_RETURN_SHA256",
    "CandidateDefinition",
    "CandidateParseError",
    "CandidateRegistry",
    "mandatory_labels",
]


EXPECTED_RETURN_SHA256 = (
    "59050a15cc89aa1cbaa680471707d0d09d2908c69e982769eaeb7112fdacdcbf"
)
"""Frozen SHA-256 of ``gptpro-crypto-candidates-v1.md``."""

ADMITTED_STRATEGY_IDS = (
    "CR-SPOT-ETR-01",
    "CR-SPOT-TPR-01",
    "CR-SPOT-CEB-01",
)
"""The three operator-admitted candidates; HTA-01 remains preserved only."""

_PRESERVED_NOT_IMPLEMENTED_ID = "CR-SPOT-HTA-01"
_ETR_RESEARCH_LABELS: Final[tuple[str, ...]] = (
    "CR-S1 verdict = BLOCKED (B2 unresolved)",
    "ETR-01×Upbit PASS = exploratory, not promotable",
)
_CANDIDATE_START = re.compile(r"(?m)^- strategy_id: (?P<strategy_id>[^\r\n]+)$")
_TOP_LEVEL_FIELD = re.compile(r"(?m)^  [A-Za-z_][A-Za-z0-9_]*:")
_PARAMETER_LINE = re.compile(
    r"\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*): (?P<value>[^\r\n]+)\Z"
)
_REQUIRED_HISTORY_DAYS = re.compile(r"(?P<days>\d+)개의")
_INTEGER = re.compile(r"-?(?:0|[1-9]\d*)\Z")
_DECIMAL = re.compile(r"-?(?:0|[1-9]\d*)\.\d+\Z")


class CandidateParseError(ValueError):
    """The sealed return cannot be used as an unambiguous strategy contract."""


def mandatory_labels(strategy_id: str) -> tuple[str, ...]:
    """Return additive registry labels that prevent research-status revival.

    These labels are intentionally outside the verbatim candidate blocks: they
    cannot alter the frozen contract hash, parameters, costs, or ablation
    definition.  They are carried by every serialized registry definition so a
    successful exploratory pair cannot be mistaken for a promotion decision.
    """
    if strategy_id == "CR-SPOT-ETR-01":
        return _ETR_RESEARCH_LABELS
    return ()


@dataclass(frozen=True)
class CandidateDefinition:
    """A candidate contract parsed directly from one raw Markdown block."""

    strategy_id: str
    family_id: str
    venue_scope: str
    required_history_days: int
    parameter_values: Mapping[str, int | float]
    signal_text: str
    entry_text: str
    exit_text: str
    ranking_text: str
    sizing_text: str
    ablation_text: str
    harness_query_text: str
    raw_contract_text: str
    source_return_sha256: str
    contract_hash: str

    @property
    def labels(self) -> tuple[str, ...]:
        """Return the mandatory non-promotional registry labels for this candidate."""
        return mandatory_labels(self.strategy_id)

    def parameter(self, name: str) -> int | float:
        """Return one source-derived parameter or fail closed on contract drift."""
        try:
            return self.parameter_values[name]
        except KeyError as exc:
            raise CandidateParseError(
                f"{self.strategy_id}: source contract lacks parameter {name!r}"
            ) from exc

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe, provenance-stamped contract representation."""
        return {
            "strategy_id": self.strategy_id,
            "contract_hash": self.contract_hash,
            "labels": self.labels,
            "source_return_sha256": self.source_return_sha256,
            "family_id": self.family_id,
            "venue_scope": self.venue_scope,
            "required_history_days": self.required_history_days,
            "parameter_values": dict(self.parameter_values),
            "signal_text": self.signal_text,
            "entry_text": self.entry_text,
            "exit_text": self.exit_text,
            "ranking_text": self.ranking_text,
            "sizing_text": self.sizing_text,
            "ablation_text": self.ablation_text,
            "harness_query_text": self.harness_query_text,
        }


@dataclass(frozen=True)
class CandidateRegistry:
    """A SHA-bound, source-parsed registry with no formula transcription path."""

    source_return_sha256: str
    definitions: tuple[CandidateDefinition, ...]

    @classmethod
    def load(
        cls,
        source_path: str | Path,
        *,
        expected_return_sha256: str = EXPECTED_RETURN_SHA256,
    ) -> CandidateRegistry:
        """Read and verify a verbatim upstream return before parsing it."""
        path = Path(source_path)
        try:
            source_bytes = path.read_bytes()
        except OSError as exc:
            raise CandidateParseError(
                f"cannot read verbatim candidate return: {path}"
            ) from exc
        return cls.from_verbatim_bytes(
            source_bytes,
            expected_return_sha256=expected_return_sha256,
        )

    @classmethod
    def from_verbatim_bytes(
        cls,
        source_bytes: bytes,
        *,
        expected_return_sha256: str,
    ) -> CandidateRegistry:
        """Parse bytes only after their exact SHA-256 matches the caller's seal."""
        actual_sha256 = hashlib.sha256(source_bytes).hexdigest()
        if actual_sha256 != expected_return_sha256:
            raise CandidateParseError(
                "candidate return SHA-256 mismatch: "
                f"expected={expected_return_sha256} actual={actual_sha256}"
            )
        try:
            source_text = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CandidateParseError("candidate return is not UTF-8") from exc

        blocks = _candidate_blocks(source_text)
        definitions = tuple(
            _parse_candidate_block(block, source_return_sha256=actual_sha256)
            for block in blocks
        )
        _validate_admission(definitions)
        return cls(source_return_sha256=actual_sha256, definitions=definitions)

    @property
    def admitted(self) -> tuple[CandidateDefinition, ...]:
        """Return only the three approved candidates in operator-specified order."""
        by_id = {definition.strategy_id: definition for definition in self.definitions}
        return tuple(by_id[strategy_id] for strategy_id in ADMITTED_STRATEGY_IDS)

    @property
    def preserved_not_implemented(self) -> CandidateDefinition:
        """Expose HTA-01 for provenance without making it executable."""
        for definition in self.definitions:
            if definition.strategy_id == _PRESERVED_NOT_IMPLEMENTED_ID:
                return definition
        raise CandidateParseError("preserved HTA-01 candidate is absent from return")

    def get(self, strategy_id: str) -> CandidateDefinition:
        """Look up one parsed candidate without silently falling back by family."""
        for definition in self.definitions:
            if definition.strategy_id == strategy_id:
                return definition
        raise CandidateParseError(f"unknown parsed candidate: {strategy_id!r}")


def _candidate_blocks(source_text: str) -> tuple[str, ...]:
    matches = tuple(_CANDIDATE_START.finditer(source_text))
    if not matches:
        raise CandidateParseError("candidate return contains no strategy blocks")

    blocks: list[str] = []
    for index, match in enumerate(matches):
        start = match.start()
        if index + 1 < len(matches):
            end = matches[index + 1].start()
        else:
            boundary = source_text.find("\n요구 산출물 2", start)
            end = len(source_text) if boundary < 0 else boundary + 1
        blocks.append(source_text[start:end])
    return tuple(blocks)


def _field(block: str, name: str) -> str:
    match = re.search(rf"(?m)^  {re.escape(name)}:\s*(?P<first>[^\r\n]*)$", block)
    if match is None:
        raise CandidateParseError(f"candidate block lacks required field {name!r}")
    remainder = block[match.end() :]
    next_field = _TOP_LEVEL_FIELD.search(remainder)
    continuation = (
        remainder[: next_field.start()] if next_field is not None else remainder
    )
    first = match.group("first")
    if first in {">", "|"}:
        lines = continuation.splitlines()
        normalized = [line[4:] if line.startswith("    ") else line for line in lines]
        value = "\n".join(normalized).strip()
    elif continuation.strip():
        value = (first + continuation).strip()
    else:
        value = first.strip()
    if not value:
        raise CandidateParseError(f"candidate field {name!r} is empty")
    return value


def _parse_number(raw: str, *, field: str) -> int | float:
    text = raw.strip()
    if _INTEGER.fullmatch(text) is not None:
        return int(text)
    if _DECIMAL.fullmatch(text) is not None:
        return float(text)
    raise CandidateParseError(f"parameter {field!r} is not a plain numeric literal")


def _parameter_values(block: str) -> Mapping[str, int | float]:
    parameter_section = _field(block, "parameter_values")
    values: dict[str, int | float] = {}
    for line in parameter_section.splitlines():
        match = _PARAMETER_LINE.fullmatch(line)
        if match is None:
            raise CandidateParseError(
                f"candidate parameter_values has malformed row: {line!r}"
            )
        name = match.group("name")
        if name in values:
            raise CandidateParseError(f"duplicate candidate parameter {name!r}")
        values[name] = _parse_number(match.group("value"), field=name)
    if not values:
        raise CandidateParseError("candidate parameter_values has no numeric rows")
    return MappingProxyType(values)


def _parse_candidate_block(
    block: str,
    *,
    source_return_sha256: str,
) -> CandidateDefinition:
    strategy_match = _CANDIDATE_START.match(block)
    if strategy_match is None:  # pragma: no cover - guarded by _candidate_blocks
        raise CandidateParseError("candidate block has no strategy_id header")
    strategy_id = strategy_match.group("strategy_id").strip()
    required_history = _field(block, "required_history")
    history_match = _REQUIRED_HISTORY_DAYS.search(required_history)
    if history_match is None:
        raise CandidateParseError(
            f"{strategy_id}: required_history does not declare a day count"
        )
    raw_contract_bytes = block.encode("utf-8")
    return CandidateDefinition(
        strategy_id=strategy_id,
        family_id=_field(block, "family_id"),
        venue_scope=_field(block, "venue_scope"),
        required_history_days=int(history_match.group("days")),
        parameter_values=_parameter_values(block),
        signal_text=_field(block, "signal"),
        entry_text=_field(block, "entry"),
        exit_text=_field(block, "exit"),
        ranking_text=_field(block, "ranking"),
        sizing_text=_field(block, "sizing"),
        ablation_text=_field(block, "falsification"),
        harness_query_text=_field(block, "expected_trade_frequency"),
        raw_contract_text=block,
        source_return_sha256=source_return_sha256,
        contract_hash=hashlib.sha256(raw_contract_bytes).hexdigest(),
    )


def _validate_admission(definitions: tuple[CandidateDefinition, ...]) -> None:
    ids = tuple(definition.strategy_id for definition in definitions)
    if len(set(ids)) != len(ids):
        raise CandidateParseError("candidate return has duplicate strategy_id values")
    missing = set(ADMITTED_STRATEGY_IDS) - set(ids)
    if missing:
        raise CandidateParseError(
            f"candidate return is missing admitted strategies: {sorted(missing)!r}"
        )
    if _PRESERVED_NOT_IMPLEMENTED_ID not in ids:
        raise CandidateParseError("candidate return is missing preserved HTA-01")
