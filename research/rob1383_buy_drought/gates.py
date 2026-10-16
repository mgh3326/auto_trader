"""Cohort enrichment (rev2 §1): failed gates, support_only_fail, episodes, scored.

Two independent sources per gate, both kept:

* ``text``       -- regex over the session's own rejection reason
  (``contrary_evidence``). Fail-direction phrases only; "X 통과/pass" never
  marks a failure.
* ``recomputed`` -- the point-in-time reconstruction (live S/R impl + live RSI)
  judged against policy 2026-09-30.1 (``core.recomputed_support_failures``).

``upside_min``, ``data_fail`` and ``other`` exist only in text (no bar corpus
holds analyst targets or tool failures).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from research.rob1383_buy_drought.core import classify_deep_limit_out, parse_reason_rsi

RSI_MAX = 45.0  # screen.rsi_max (2026-09-30.1)

SUPPORT_TEXT_GATES = frozenset(
    {
        "support_strength",
        "independent_sources",
        "support_within_pct",
        "deep_limit_band",
        "support_distance",  # text says the distance failed without naming which band
    }
)
SUPPORT_RECOMPUTED_GATES = frozenset(
    {
        "support_strength",
        "independent_sources",
        "support_within_pct",
        "deep_limit_band_shallow",
        "deep_limit_band_deep",
        "no_support_below",
    }
)


# Clause-local gap: any char except a sentence break, but a dot *between
# digits* (39.83%) is not a break -- otherwise every decimal ends the clause.
_GAP = r"(?:[^.;。\n]|(?<=\d)\.(?=\d))"


def _rx(*patterns: str) -> list[re.Pattern[str]]:
    return [
        re.compile(
            p.replace("[^.;。\\n]", _GAP).replace("[^.;\\n]", _GAP), re.IGNORECASE
        )
        for p in patterns
    ]


_TEXT_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "support_strength": _rx(
        r"weak\s*(단일|single|only|뿐)",
        r"weak[- ]only",
        r"only weak",
        r"(전부|모두|all|both)[^.;\n]{0,12}weak",
        r"every support[^.;\n]{0,80}weak",
        r"support_strength_min[^.;\n]{0,40}(미달|fail|not met)",
        r"fails?\s+(screen\.)?support_strength_min",
        r"strong 지지\s*\**0",
        r"zero strong",
        r"strong[^.;\n]{0,6}(지지\s*)?0개",
        r"moderate( 이상)?[^.;\n]{0,25}(없음|미달|없다|없고|absent|부재|0개)",
        r"지지 (품질|강도)[^.;\n]{0,10}(미달|실패|불충족)",
        r"strength gate not met",
    ),
    "independent_sources": _rx(
        r"2\s*(패밀리|계열|famil\w*)[^.;\n]{0,25}(없음|미달|없다|없고|not met|absent|부재)",
        r"count 2 미달",
        r"independent_support_source_count_min[^.;\n]{0,40}(미달|fail|not met|at once)",
        r"단일\s*(패밀리|계열)",
        r"single[- ](family|source)",
        r"2계열 충족 0",
    ),
    "support_within_pct": _rx(
        r"support_within_pct[^.;\n]{0,60}(밖|fail|미달|outside|at once|rejected)",
        r"rejected on screen\.support_within_pct",
        r"no support within",
        r"outside the 8% band",
        r"8% ?(창|band|window)[^.;\n]{0,25}(밖|outside|없)",
        r"창 밖",
        r"no named support",
        r"명명(된)? 지지(가)? 없",
        r"지지 없음",
    ),
    "support_distance": _rx(
        r"거리(?:만)?\s*(?:게이트\s*)?(미달|실패|탈락|밖)",
        r"distance (only )?(fails?|failed|not met)",
    ),
    "upside_min": _rx(
        # The gap may not cross a pass token: "upside 46.98 통과 ... 거리만 미달"
        # is an upside *pass* followed by another gate's failure.
        r"(upside|업사이드)(?:(?!통과|pass|✓|≥)[^.;\n]){0,80}?(<|미달|below|short of|fails?\b|탈락)",
        # Value-anchored: the comparator follows the upside value itself,
        # optionally after one parenthetical ("31.2% (RSI pass) below 40").
        r"(upside|업사이드)[^\d.;\n]{0,25}?\+?\d+(?:\.\d+)?%?\s*(?:\([^)]{0,30}\)\s*)?(<|미달|below|short of)",
        # Negated pass: "upside 통과하지 못함" / "upside did not pass".
        r"(upside|업사이드)[^.;\n]{0,20}?(통과하지 못|통과 못|did not pass|failed to pass)",
        r"upside_min_pct[^.;\n]{0,30}(미달|fail|<)",
        r"\+\d+(\.\d+)?%\s*<\s*40",
    ),
    "data_fail": _rx(
        r"fail-closed",
        r"timeout",
        r"timed out",
        r"조회 (실패|불가)",
        r"rows missing",
        r"근거 부재",
        r"unavailable",
        r"권한[^.;\n]{0,10}(없|부재|차단|거부)",
    ),
}

_OTHER_PATTERNS: dict[str, re.Pattern[str]] = {
    "recovery_gate": re.compile(
        r"recovery gate|회복 ?게이트|C1 (market|recovery)|\bm=0\b", re.I
    ),
    "liquidity": re.compile(r"low liquidity|유동성|illiquid", re.I),
    "no_chasing": re.compile(r"no_chasing|chas(e|ing)", re.I),
    "notional_band": re.compile(
        r"per_symbol_notional|band upper|notional band|> band", re.I
    ),
    "thesis": re.compile(r"thesis_reconfirmed|thesis (broken|fails?)", re.I),
    "averaging_ak": re.compile(r"A\(k\)|A_limit", re.I),
    "portfolio_cap": re.compile(
        r"포트폴리오 캡|portfolio cap|sector_cluster|max_symbols_per", re.I
    ),
    "crash_day_hold": re.compile(r"crash_day hold|crash_day 예외 불충족", re.I),
}


def parse_text_gates(reason: str | None) -> tuple[frozenset[str], tuple[str, ...]]:
    """Failed gate codes stated in the reason, plus matched ``other`` keywords."""

    if not reason:
        return frozenset(), ()
    found = {
        g for g, pats in _TEXT_PATTERNS.items() if any(p.search(reason) for p in pats)
    }
    if classify_deep_limit_out(reason):
        found.add("deep_limit_band")
    rsi = parse_reason_rsi(reason)
    if rsi is not None and rsi > RSI_MAX:
        found.add("rsi_max")
    others = tuple(sorted(k for k, p in _OTHER_PATTERNS.items() if p.search(reason)))
    if others:
        found.add("other")
    return frozenset(found), others


@dataclass(frozen=True, slots=True)
class GateVerdict:
    failed_text: frozenset[str]
    failed_recomputed: frozenset[str] | None  # None -> not reconstructed
    other_keywords: tuple[str, ...]
    support_only_fail: bool
    support_only_basis: str  # text | recomputed_text_silent | none
    rsi_value: float | None
    rsi_source: str | None


def compose(
    reason: str | None,
    recomputed_support: frozenset[str] | None,
    recon_rsi: float | None,
) -> GateVerdict:
    """Combine text and recomputed verdicts into ``support_only_fail``.

    RSI authority: the session's own stated RSI when present (it is what the
    session gated on), else the reconstructed RSI.

    ``support_only_fail`` is true only when the text itself names a support
    sub-gate failure, names no non-support failure (upside/data/other), and
    RSI <= 45 (basis ``text``). A row whose text names *no* gate while the
    reconstruction shows a support failure has unverified upside/data gates,
    so it is **not** a support-only failure: it is reported separately as
    ``support_only_unverified`` (basis ``recomputed_text_silent``).
    """

    text, others = parse_text_gates(reason)
    stated_rsi = parse_reason_rsi(reason)
    rsi_value, rsi_source = (
        (stated_rsi, "text") if stated_rsi is not None else (recon_rsi, "recomputed")
    )
    if rsi_value is None:
        rsi_source = None
    recomputed = set(recomputed_support or ())
    if recon_rsi is not None and recon_rsi > RSI_MAX:
        recomputed.add("rsi_max")
    rsi_ok = rsi_value is not None and rsi_value <= RSI_MAX
    text_support = text & SUPPORT_TEXT_GATES
    text_non_support = text - SUPPORT_TEXT_GATES - {"rsi_max"}
    rec_support = (recomputed_support or frozenset()) & SUPPORT_RECOMPUTED_GATES
    if text_support and not text_non_support and rsi_ok:
        basis = "text"
    elif not text and rec_support and rsi_ok:
        basis = "recomputed_text_silent"
    else:
        basis = "none"
    return GateVerdict(
        failed_text=text,
        failed_recomputed=frozenset(recomputed)
        if recomputed_support is not None
        else None,
        other_keywords=others,
        support_only_fail=basis == "text",
        support_only_basis=basis,
        rsi_value=rsi_value,
        rsi_source=rsi_source,
    )


def union_gates(
    text: frozenset[str], recomputed: frozenset[str] | None
) -> frozenset[str]:
    """Union with recomputed deep-limit detail folded into ``deep_limit_band``."""

    rec = set(recomputed or ())
    if rec & {"deep_limit_band_shallow", "deep_limit_band_deep"}:
        rec -= {"deep_limit_band_shallow", "deep_limit_band_deep"}
        rec.add("deep_limit_band")
    return frozenset(text | rec)


# ------------------------------------------------------------------ episodes

EPISODE_SESSIONS_MAX = 5  # KR/US: next record within 5 trading sessions
EPISODE_CRYPTO_HOURS = 72  # crypto: within 72 h
EPISODE_ANCHOR_TOL_PCT = 5.0  # anchor within ±5% of the episode's first anchor


@dataclass(frozen=True, slots=True)
class EpisodeInput:
    key: int
    market: str
    symbol: str
    created_at: datetime
    anchor: float | None


def assign_episodes(
    items: Sequence[EpisodeInput],
    sessions_between: Callable[[str, str, datetime, datetime], int | None],
) -> dict[int, str]:
    """Group repeated records of one symbol in one regime.

    A record joins the open episode of its (market, symbol) when (a) it follows
    the *previous record* by <= 5 sessions (KR/US, counted on the symbol's own
    daily bars; 7 calendar days if bars are missing) or <= 72 h (crypto), and
    (b) its anchor is within ±5% of the episode's *first* anchor (skipped when
    either anchor is unknown). Otherwise it opens a new episode.
    """

    out: dict[int, str] = {}
    ordered = sorted(items, key=lambda i: (i.market, i.symbol, i.created_at, i.key))
    prev: EpisodeInput | None = None
    first_anchor: float | None = None
    episode = ""
    for it in ordered:
        joins = False
        if prev is not None and (prev.market, prev.symbol) == (it.market, it.symbol):
            if it.market == "crypto":
                gap_ok = (
                    it.created_at - prev.created_at
                ).total_seconds() <= EPISODE_CRYPTO_HOURS * 3600
            else:
                n = sessions_between(
                    it.market, it.symbol, prev.created_at, it.created_at
                )
                gap_ok = (
                    n <= EPISODE_SESSIONS_MAX
                    if n is not None
                    else (it.created_at - prev.created_at).days <= 7
                )
            anchor_ok = (
                first_anchor is None
                or it.anchor is None
                or abs(it.anchor / first_anchor - 1.0) * 100.0 <= EPISODE_ANCHOR_TOL_PCT
            )
            joins = gap_ok and anchor_ok
        if not joins:
            episode = f"{it.market}:{it.symbol}:{it.created_at:%Y%m%dT%H%MZ}"
            first_anchor = it.anchor
        out[it.key] = episode
        prev = it
    return out


_PASS_PATTERNS = _rx(
    r"지지[^.;。\n]{0,70}(는|은|이)?\s*통과",
    r"support[^.;\n]{0,80}\bpass(es|ed)?\b",
    r"passes every symbol gate",
    r"symbol gates pass",
    r"지지 품질은 (좋|통과)",
    r"\bin band\b",
    r"gate-passing",
)


def text_states_support_pass(reason: str | None) -> bool:
    """The reason says the support gate passed and names no support failure."""

    if not reason:
        return False
    text, _ = parse_text_gates(reason)
    if text & SUPPORT_TEXT_GATES:
        return False
    return any(p.search(reason) for p in _PASS_PATTERNS)
