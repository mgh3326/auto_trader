"""Frozen hash gates and strict KOSPI index input."""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from research.kr_corpus.d3_engine.constants import (
    CONTRACT_SHA256,
    INDEX_SHA256,
    ArtifactPaths,
)

INDEX_HEADER = (
    "날짜",
    "시가",
    "고가",
    "저가",
    "종가",
    "거래량",
    "거래대금",
    "상장시가총액",
)


class ContractDrift(RuntimeError):
    code = "NEEDS_UPSTREAM(contract_or_golden_drift)"


class InvalidIndexInput(ValueError):
    code = "RUN_INVALID_INDEX_INPUT"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_start_gate(paths: ArtifactPaths) -> tuple[dict[str, str], ...]:
    targets = (
        (paths.contract_v3, CONTRACT_SHA256["d3-contract-draft-v3-20260806.md"]),
        (paths.contract_v2, CONTRACT_SHA256["d3-contract-draft-v2-20260806.md"]),
        (paths.baseline, CONTRACT_SHA256["operator-style-baseline-v1-20260806.md"]),
        (paths.index_csv, CONTRACT_SHA256["kospi_index_daily_2014_2024.csv"]),
        (paths.tick_yaml, CONTRACT_SHA256["krx_tick_table_frozen.yaml"]),
        (
            paths.tick_python_provenance,
            CONTRACT_SHA256["krx_tick_size_frozen.py"],
        ),
        (paths.golden_root / "CONTRACT.md", CONTRACT_SHA256["CONTRACT.md"]),
        (paths.golden_root / "provenance.json", CONTRACT_SHA256["provenance.json"]),
        (
            paths.golden_root / "checksums.sha256",
            CONTRACT_SHA256["checksums.sha256"],
        ),
    )
    results: list[dict[str, str]] = []
    for path, expected in targets:
        if not path.is_file():
            raise ContractDrift(f"missing:{path}")
        actual = sha256_file(path)
        if actual != expected:
            raise ContractDrift(f"sha:{path.name}:{actual}!={expected}")
        results.append({"file": path.name, "sha256": actual, "status": "PASS"})
    return tuple(results)


def verify_golden_checksums(root: Path) -> dict[str, int | bool]:
    checksum_path = root / "checksums.sha256"
    entries: list[tuple[str, str]] = []
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            expected, relative = line.split(maxsplit=1)
        except ValueError as exc:
            raise ContractDrift("malformed golden checksum row") from exc
        entries.append((expected, relative.lstrip("*")))
    passed = 0
    for expected, relative in entries:
        path = root / relative
        if not path.is_file() or sha256_file(path) != expected:
            raise ContractDrift(f"golden checksum drift:{relative}")
        passed += 1
    vector_ids = sorted(path.stem for path in (root / "vectors").glob("*.json"))
    expected_ids = sorted(path.stem for path in (root / "expected").glob("*.json"))
    total_files = sum(1 for path in root.rglob("*") if path.is_file())
    if vector_ids != expected_ids or len(vector_ids) != 33:
        raise ContractDrift("golden id/count drift")
    if len(entries) != 68 or total_files != 69:
        raise ContractDrift("golden physical file/count drift")
    return {
        "vectors": len(vector_ids),
        "expected": len(expected_ids),
        "files": total_files,
        "checksums_passed": passed,
        "checksums_total": len(entries),
        "ids_match": True,
    }


@dataclass(frozen=True, slots=True)
class IndexRow:
    session: date
    close: Decimal


@dataclass(frozen=True, slots=True)
class FrozenKospiIndex:
    rows: tuple[IndexRow, ...]

    @classmethod
    def load(
        cls, path: Path, *, expected_sha256: str = INDEX_SHA256
    ) -> FrozenKospiIndex:
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected_sha256:
            raise InvalidIndexInput(f"sha drift {actual}!={expected_sha256}")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InvalidIndexInput("index must be UTF-8(-SIG)") from exc
        return cls.from_text(text)

    @classmethod
    def from_text(cls, text: str) -> FrozenKospiIndex:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if tuple(reader.fieldnames or ()) != INDEX_HEADER:
            raise InvalidIndexInput("index header drift")
        rows: list[IndexRow] = []
        previous: date | None = None
        seen: set[date] = set()
        for raw in reader:
            if set(raw) != set(INDEX_HEADER):
                raise InvalidIndexInput("index row field drift")
            try:
                session = date.fromisoformat(raw["날짜"])
                close = Decimal(raw["종가"])
            except (ValueError, InvalidOperation) as exc:
                raise InvalidIndexInput("invalid index date/close") from exc
            if not close.is_finite() or close <= 0:
                raise InvalidIndexInput("index close must be finite positive")
            if session in seen or (previous is not None and session <= previous):
                raise InvalidIndexInput("index dates must be strict ascending unique")
            rows.append(IndexRow(session, close))
            seen.add(session)
            previous = session
        if not rows:
            raise InvalidIndexInput("empty index")
        return cls(tuple(rows))

    def regime_allows(
        self,
        *,
        decision_session: date,
        required_xkrx_sessions: Iterable[date] | None = None,
    ) -> tuple[bool, Decimal | None, Decimal | None, date | None]:
        by_date = {row.session: index for index, row in enumerate(self.rows)}
        if decision_session not in by_date:
            return False, None, None, None
        decision_index = by_date[decision_session]
        if decision_index < 200:
            return False, None, None, None
        previous_rows = self.rows[decision_index - 200 : decision_index]
        if required_xkrx_sessions is not None:
            required = tuple(required_xkrx_sessions)
            if (
                len(required) < 201
                or tuple(row.session for row in previous_rows) != required[-201:-1]
            ):
                return False, None, None, None
        previous = previous_rows[-1]
        sma = sum((row.close for row in previous_rows), Decimal(0)) / Decimal(200)
        return previous.close >= sma, previous.close, sma, previous.session
