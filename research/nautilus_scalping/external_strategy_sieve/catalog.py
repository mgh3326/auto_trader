"""ROB-383 — JSON catalog loader for candidate cards.

JSON (not YAML) because PyYAML is absent from the research venv; this matches the
existing ``data_manifests/*.json`` convention. ``load_catalog`` never raises on
malformed input — it returns the cards it could build plus a list of errors so
the whole file can be reviewed at once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields

from external_strategy_sieve.schema import CandidateCard, validate

_CARD_FIELDS = {f.name for f in fields(CandidateCard)}
_TUPLE_FIELDS = ("data_requirements", "tail_risk_flags")


@dataclass(frozen=True)
class CatalogLoad:
    cards: tuple[CandidateCard, ...]
    errors: tuple[str, ...]


def load_catalog(path: str) -> CatalogLoad:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    cards: list[CandidateCard] = []
    errors: list[str] = []
    seen: set[str] = set()

    if not isinstance(raw, list):
        return CatalogLoad(cards=(), errors=("catalog root must be a JSON array",))

    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            errors.append(f"entry[{idx}] is not an object")
            continue
        unknown = set(entry) - _CARD_FIELDS
        if unknown:
            errors.append(f"entry[{idx}] has unknown fields {sorted(unknown)}")
        missing = _CARD_FIELDS - set(entry)
        if missing:
            errors.append(f"entry[{idx}] missing fields {sorted(missing)}")
            continue
        kw = {k: entry[k] for k in _CARD_FIELDS}
        for tf in _TUPLE_FIELDS:
            kw[tf] = tuple(kw[tf])
        card = CandidateCard(**kw)
        cards.append(card)
        for err in validate(card):
            errors.append(f"{card.candidate_id}: {err}")
        if card.candidate_id in seen:
            errors.append(f"duplicate candidate_id {card.candidate_id!r}")
        seen.add(card.candidate_id)

    return CatalogLoad(cards=tuple(cards), errors=tuple(errors))
