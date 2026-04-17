"""CIO Scout Report Quality Gate service.

Parses a Scout Report markdown, runs the ROB-170 §3.2 per-row same-depth-check
and the G1~G6 gate sweep, and emits a structured report suitable for CIO
decisioning (ACCEPT / ACCEPT-WITH-FLAG / REOPEN).

Public API consumed by scripts/cio_quality_gate.py and by ROB-196 e2e tests:

    from app.services.cio_quality_gate_service import evaluate_scout_report

    report = evaluate_scout_report(
        markdown=report_md,
        cash_balance=1_670_000,                            # KRW, optional, G6
        tool_failures=["screen_stocks: schema mismatch"],  # optional, G3
    )
    report.overall_status  # "PASS" | "PARTIAL" | "FAIL"
    report.violations      # list[GateViolation]  (only failed gates)
    report.reopen_comment  # str | None  — §7.2 template when hard-gate fails
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Keyword tables for §3 per-candidate checklist items (1~8)
# ---------------------------------------------------------------------------

KEYWORDS: dict[str, list[str]] = {
    "source": [
        r"\bmomentum\b",
        r"\boversold\b",
        r"\bscreen(er)?\b",
        r"\bholdings?\b",
        r"\bpeer\b",
        r"\[신규\]",
        r"신규",
        r"\bDCA\b",
        r"drawdown",
        r"보유\s*중",
        r"shortlist",
    ],
    "quote": [
        r"\d{1,3}(,\d{3})+",
        r"시장가",
        r"현재가",
        r"시총",
        r"거래량",
        r"₩\s*\d",
    ],
    "indicators": [
        r"\bRSI\b",
        r"\bMACD\b",
        r"\bBB\b",
        r"볼밴",
        r"\bADX\b",
        r"\bEMA\b",
        r"bollinger",
    ],
    "sr": [
        r"지지",
        r"저항",
        r"buy[\s_]?zone",
        r"bb[_\s]?(lower|upper|mid)",
        r"\bVAL\b",
        r"\bVAH\b",
        r"\bPOC\b",
        r"\bfib\b",
        r"support",
        r"resistance",
    ],
    "news": [
        r"뉴스",
        r"\bnews\b",
        r"catalyst",
        # Bare "Naver" is ambiguous — "NAVER" is a Korean stock ticker that
        # would false-pass shallow rows on news evidence. Require an explicit
        # news-context qualifier ("Naver news" / "네이버뉴스").
        r"\bNaver\s*(news|뉴스)\b",
        r"네이버\s*(news|뉴스)",
        r"\bReuters\b",
        r"\bBloomberg\b",
        r"한경",
        r"매경",
        r"earning",
        r"실적",
        r"guidance",
    ],
    "fundamental": [
        r"목표가",
        r"컨센서스",
        r"\bPER\b",
        r"\bPBR\b",
        r"매출",
        r"영업이익",
        r"margin",
        r"fundamental",
        r"consensus",
        r"애널\s*목표",
    ],
    "execution": [
        r"\bKIS\b",
        r"\bToss\b",
        r"해외",
        r"미지원",
        r"실행\s*경로",
        r"execution\s*path",
    ],
    "dca_compare": [
        r"\b대비\b",
        r"우위",
        r"열위",
        r"\bvs\b",
        r"비교",
        r"중복",
        r"더\s*안전",
        r"더\s*유리",
    ],
}

CHECKLIST_KEY: dict[int, str] = {
    1: "source",
    2: "quote",
    3: "indicators",
    4: "sr",
    5: "news",
    6: "fundamental",
    7: "execution",
    8: "dca_compare",
}

CHECKLIST_LABELS: dict[int, str] = {
    1: "Source",
    2: "Quote",
    3: "Indicators",
    4: "S/R",
    5: "News",
    6: "Fund/consensus",
    7: "Execution path",
    8: "DCA 비교",
}

# G4 qualifier regex: plain "KIS" or "Toss" alone is insufficient for
# new candidates; need one of the ROB-170 §3.1 forms.
EXEC_QUALIFIER_RE = re.compile(
    r"(즉시|manual|mixed|KIS\s*\+\s*Toss|KIS\s*일부|Toss\s*일부|해외|미지원|수동|자동)",
    re.IGNORECASE,
)


def has_keyword(text: str, group: str) -> bool:
    """True if any KEYWORDS[group] pattern matches the text."""
    for pat in KEYWORDS[group]:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------------------------

CODE_RE = re.compile(r"\b(\d{6})\b")
SKIP_CELL_WORDS = {"---", ""}

TABLE_SEPARATOR_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")
EXEC_HEADER_RE = re.compile(
    r"(실행\s*경로|execution\s*path|계좌.*실행|\bexec(ution)?\b)",
    re.IGNORECASE,
)
CATEGORY_HEADER_RE = re.compile(
    r"(^|\s)(분류|category)(\s|$)",
    re.IGNORECASE,
)
# v2 §6.2 sub-bullets label the execution path like
# "· execution path: KIS 즉시 ·" or "· 실행경로: Toss manual ·". Capture
# only the path-labeled segment so G4's qualifier check does not match
# unrelated middle-dot bullet segments (뉴스: 해외 매출 / 전기 자동차 …).
EXEC_PATH_SEGMENT_RE = re.compile(
    r"(?:execution\s*path|실행\s*경로)\s*[:：]\s*([^·\n|]+)",
    re.IGNORECASE,
)


@dataclass
class Candidate:
    name: str
    code: str
    is_new: bool = False
    row_texts: list[str] = field(default_factory=list)
    items: dict[int, bool] = field(default_factory=dict)
    execution_cell: str = ""

    @property
    def context_text(self) -> str:
        return "\n".join(self.row_texts)

    @property
    def is_avoid(self) -> bool:
        txt = self.context_text.lower()
        return "avoid" in txt or "기각" in self.context_text

    @property
    def verdict(self) -> str:
        has_7 = self.items.get(7, False)
        others = sum(1 for k in (1, 2, 3, 4, 5, 6, 8) if self.items.get(k, False))
        if has_7 and others >= 6:
            return "pass"
        if self.is_avoid and self.items.get(1) and self.items.get(2):
            return "pass (avoid-simplified)"
        return "fail"


def _detect_exec_col_idx(header_line: str) -> int | None:
    """Return the 0-based column index labelled 실행경로/execution path, or None."""
    cells = [c.strip() for c in header_line.strip().strip("|").split("|")]
    for idx, cell in enumerate(cells):
        if EXEC_HEADER_RE.search(cell):
            return idx
    return None


def _detect_category_col_idx(header_line: str) -> int | None:
    """Return the 0-based column index labelled 분류/category, or None."""
    cells = [c.strip() for c in header_line.strip().strip("|").split("|")]
    for idx, cell in enumerate(cells):
        if CATEGORY_HEADER_RE.search(cell):
            return idx
    return None


def _exec_path_segments(text: str) -> list[str]:
    """Return only the substrings that follow an 'execution path:' / '실행경로:' label.

    Used for G4's sub-bullet fallback so unrelated middle-dot bullet segments
    (뉴스 headlines, S/R notes, etc.) cannot satisfy EXEC_QUALIFIER_RE via
    incidental 해외 / 자동 / 수동 tokens.
    """
    return [m.strip() for m in EXEC_PATH_SEGMENT_RE.findall(text)]


def _extract_execution_cell(row_line: str, col_idx: int | None = None) -> str:
    """Return the execution-path cell text from a markdown pipe row.

    If ``col_idx`` is given (header-detected 실행경로/execution path column),
    pull that column directly — the row's column order is irrelevant. When
    no header column matched, fall back to the trailing cell.
    """
    cells = [c.strip() for c in row_line.strip().strip("|").split("|")]
    if not cells:
        return ""
    if col_idx is not None and 0 <= col_idx < len(cells):
        return cells[col_idx]
    return cells[-1]


def extract_candidates(md: str) -> list[Candidate]:
    """Scan the markdown for candidate rows and dedup/merge by 6-digit code.

    Any markdown table row containing a 6-digit code is treated as a candidate.
    Sub-line bullets ("|   | • ...") immediately following the row are folded
    into the same row_text for keyword detection (ROB-170 §6.2 v2 format).

    A subline is identified by (a) starting with `|`, (b) having an empty
    first cell (depth continuation marker), (c) containing a bullet (`•`),
    and (d) not introducing a new 6-digit code. The pipe-count of the
    subline itself is irrelevant — v2 reports frequently write multi-cell
    bullet rows like `|   | • RSI 54 | • BB mid |`.

    Table-header tracking: when a markdown table separator (`|---|---|`) is
    encountered, the row immediately above is treated as the column header
    for the table that follows. If any header cell matches `실행경로` /
    `execution path`, that column index is used to extract the execution
    cell from subsequent candidate rows — regardless of column position
    (v2 §6.2 reports put 액션 in the last column, not 실행경로).
    """
    lines = md.splitlines()
    by_code: dict[str, Candidate] = {}
    current_exec_col_idx: int | None = None
    current_category_col_idx: int | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if TABLE_SEPARATOR_RE.match(line):
            if i > 0 and lines[i - 1].lstrip().startswith("|"):
                current_exec_col_idx = _detect_exec_col_idx(lines[i - 1])
                current_category_col_idx = _detect_category_col_idx(lines[i - 1])
            else:
                current_exec_col_idx = None
                current_category_col_idx = None
            i += 1
            continue
        if line.count("|") < 3:
            i += 1
            continue
        code_match = CODE_RE.search(line)
        if not code_match:
            i += 1
            continue

        code = code_match.group(1)
        row_parts = [line]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.lstrip().startswith("|"):
                break
            if CODE_RE.search(nxt):
                break
            cells = [c.strip() for c in nxt.strip().strip("|").split("|")]
            first_cell_empty = (not cells) or cells[0] == ""
            has_bullet = "•" in nxt
            if first_cell_empty and has_bullet:
                row_parts.append(nxt)
                j += 1
                continue
            break

        row_text = "\n".join(row_parts)

        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        name_cell = next(
            (c for c in cells if CODE_RE.search(c)), cells[0] if cells else code
        )
        name = re.sub(r"\*+", "", name_cell).strip()
        # 신규 detection is scoped to avoid false positives from 액션/뉴스/메모
        # cells that incidentally mention "신규":
        #   - If the table has a 분류 header column (v2 §6.2), trust only that
        #     cell. No leakage from other columns.
        #   - Otherwise fall back to the 종목 name cell where v1 reports embed
        #     the `[신규]` tag (e.g. "**[신규]** Krafton 259960").
        if current_category_col_idx is not None and 0 <= current_category_col_idx < len(
            cells
        ):
            is_new = "신규" in cells[current_category_col_idx]
        else:
            is_new = "신규" in name_cell

        exec_cell = _extract_execution_cell(line, current_exec_col_idx)

        if code in by_code:
            c = by_code[code]
            c.row_texts.append(row_text)
            c.is_new = c.is_new or is_new
            if EXEC_QUALIFIER_RE.search(exec_cell) and not EXEC_QUALIFIER_RE.search(
                c.execution_cell
            ):
                c.execution_cell = exec_cell
            elif not c.execution_cell and exec_cell:
                c.execution_cell = exec_cell
        else:
            c = Candidate(
                name=name,
                code=code,
                is_new=is_new,
                row_texts=[row_text],
                execution_cell=exec_cell,
            )
            by_code[code] = c

        i = j if j > i + 1 else i + 1

    cands = list(by_code.values())
    for c in cands:
        ctx = c.context_text
        for k, key in CHECKLIST_KEY.items():
            c.items[k] = has_keyword(ctx, key)
    return cands


# ---------------------------------------------------------------------------
# Gate checks
# ---------------------------------------------------------------------------


def detect_limitations_section(md: str) -> bool:
    return bool(re.search(r"^\s*#{1,3}\s*제한사항", md, re.MULTILINE))


TOOL_FAILURE_PATTERNS: list[tuple[str, str]] = [
    (r"schema\s*mismatch", "schema mismatch"),
    (r"parameter\s*validation", "parameter validation"),
    (r"invalid\s*symbol", "invalid symbol"),
    (r"rate\s*limit", "rate limit"),
    (r"upstream\s*timeout", "upstream timeout"),
    (r"fallback\s*path", "fallback path"),
    (r"fallback.*Yahoo", "Yahoo fallback"),
]


def detect_tool_failure_signals(md: str) -> list[str]:
    signals = []
    for pat, label in TOOL_FAILURE_PATTERNS:
        if re.search(pat, md, re.IGNORECASE):
            signals.append(label)
    return signals


GROUPED_RE = re.compile(
    r"(전원|전부|모두).*(microcap|SPAC|REIT|과열|퀄리티|미달|avoid|기각|부적합|기준)",
    re.IGNORECASE,
)
GROUPED_ETC_RE = re.compile(r"(전원|전부|모두).{0,80}등\)?", re.IGNORECASE)


def detect_grouped_rejection(md: str) -> tuple[bool, bool]:
    """Return (has_grouped, has_exception_breakdown)."""
    has_grouped = bool(GROUPED_RE.search(md))
    truncated = bool(GROUPED_ETC_RE.search(md))
    has_breakdown = has_grouped and not truncated
    return has_grouped, has_breakdown


CASH_CALL_RE = re.compile(
    r"(get_cash_balance|get_available_capital|예수금\s*(실측|조회|확인)|"
    r"현금\s*(실측|조회|확인))",
    re.IGNORECASE,
)
CASH_FIELD_MISSING_RE = re.compile(
    r"(KRW\s*잔고\s*필드가?\s*없|주문가능.*없|예수금\s*필드.*없|"
    r"cash.*not\s*available|잔고\s*정보\s*없)",
    re.IGNORECASE,
)


def detect_cash_balance_call(md: str) -> bool:
    """True if Scout explicitly called get_cash_balance (or equivalent).

    A caveat line like "KRW 잔고 필드가 없음" is NOT a positive call — it's
    disclosure that the call was skipped / unavailable.
    """
    if CASH_FIELD_MISSING_RE.search(md):
        return False
    return bool(CASH_CALL_RE.search(md))


ORDER_TOTAL_RE = re.compile(r"총\s*[~≈]?\s*₩?([\d.,]+)\s*M", re.IGNORECASE)
ACTUAL_CASH_RE = re.compile(r"예수금\s*[~≈:]?\s*₩?([\d.,]+)\s*M", re.IGNORECASE)


def sum_order_amount(md: str) -> float | None:
    total = 0.0
    for m in ORDER_TOTAL_RE.findall(md):
        try:
            total += float(m.replace(",", ""))
        except ValueError:
            pass
    return total if total > 0 else None


def parse_actual_cash(md: str) -> float | None:
    m = ACTUAL_CASH_RE.search(md)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


BUDGET_DISCLOSURE_RE = re.compile(
    r"(CIO.*예수금|예수금.*재(산정|조정)|템플릿\s*사이즈|재산정\s*요망|CIO가.*재산정)",
    re.IGNORECASE,
)


def detect_budget_disclosure(md: str) -> bool:
    return bool(BUDGET_DISCLOSURE_RE.search(md))


GateId = Literal["G1", "G2", "G3", "G4", "G5", "G6"]
Severity = Literal["hard", "soft"]
OverallStatus = Literal["PASS", "PARTIAL", "FAIL"]


@dataclass
class GateResult:
    """Full result for a single gate — used by the CLI renderer."""

    key: str
    label: str
    severity: str  # hard / soft
    passed: bool
    detail: str = ""


@dataclass
class GateViolation:
    """A failed gate, flattened for ROB-196 e2e assertions."""

    gate_id: GateId
    severity: Severity
    detail: str


@dataclass
class QualityGateReport:
    overall_status: OverallStatus
    violations: list[GateViolation]
    reopen_comment: str | None
    gates: list[GateResult]
    candidates: list[Candidate]


def run_gates(
    md: str,
    cands: list[Candidate],
    cash_override_m: float | None = None,
    extra_tool_failures: list[str] | None = None,
) -> list[GateResult]:
    """Evaluate G1~G6 against the candidate set + raw markdown.

    ``cash_override_m`` is the caller-supplied cash balance in **millions KRW**
    (matches how the markdown encodes "₩N.NM"). Top-level callers should go
    through :func:`evaluate_scout_report` which accepts raw KRW and converts.
    ``extra_tool_failures`` is merged with failure signals parsed from the
    markdown; pass Scout/tooling runtime errors the board already knows about.
    """

    results: list[GateResult] = []

    # ---- G1 Depth ----
    failed = [c for c in cands if c.verdict == "fail"]
    results.append(
        GateResult(
            key="G1",
            label="Depth",
            severity="hard",
            passed=not failed,
            detail=(
                "fail 후보: " + ", ".join(f"{c.name} ({c.verdict})" for c in failed)
                if failed
                else f"{len(cands)}건 모두 same-depth-check pass"
            ),
        )
    )

    # ---- G2 Grouped rejection (soft) ----
    has_grouped, has_breakdown = detect_grouped_rejection(md)
    g2_passed = (not has_grouped) or has_breakdown
    if not has_grouped:
        g2_detail = "grouped rejection 없음"
    elif has_breakdown:
        g2_detail = "grouped rejection 존재 — 개별 분해 포함"
    else:
        g2_detail = "grouped rejection 존재 — '…등' 형태로 개별 분해 부족"
    results.append(GateResult("G2", "Grouped rejection", "soft", g2_passed, g2_detail))

    # ---- G3 Tool failure disclosure ----
    signals = detect_tool_failure_signals(md)
    if extra_tool_failures:
        signals = list(signals) + [s for s in extra_tool_failures if s]
    has_limits = detect_limitations_section(md)
    g3_passed = (not signals) or has_limits
    if signals:
        g3_detail = (
            f"signals: {', '.join(signals)} / `### 제한사항`: "
            f"{'있음' if has_limits else '없음'}"
        )
    else:
        g3_detail = "tool failure signal 없음"
    results.append(
        GateResult("G3", "Tool failure disclosure", "hard", g3_passed, g3_detail)
    )

    # ---- G4 Execution path ----
    # The qualifier must live in one of two explicit slots:
    #   1. The 실행경로 / execution path column cell (header-detected or
    #      v1 trailing cell).
    #   2. A sub-bullet segment explicitly labelled "execution path:" or
    #      "실행경로:" — scanned via EXEC_PATH_SEGMENT_RE so that incidental
    #      middle-dot bullet text (뉴스: 해외 매출 / 전기 자동차 / 수동 검증)
    #      cannot false-pass G4 through context_text-wide regex matching.
    def _has_exec_qualifier(cand: Candidate) -> bool:
        if EXEC_QUALIFIER_RE.search(cand.execution_cell):
            return True
        for seg in _exec_path_segments(cand.context_text):
            if EXEC_QUALIFIER_RE.search(seg):
                return True
        return False

    no_qual = [c for c in cands if c.is_new and not _has_exec_qualifier(c)]
    results.append(
        GateResult(
            key="G4",
            label="Execution path",
            severity="hard",
            passed=not no_qual,
            detail=(
                "qualifier 부족(bare 'KIS' 등): "
                + ", ".join(f"{c.name} → '{c.execution_cell}'" for c in no_qual)
                if no_qual
                else "모든 신규 후보 execution qualifier 기록"
            ),
        )
    )

    # ---- G5 Comparison ----
    has_compare = any(c.items.get(8, False) for c in cands)
    if not has_compare and re.search(r"(대비|우위|열위|기존.*DCA.*대비)", md):
        has_compare = True
    results.append(
        GateResult(
            key="G5",
            label="DCA vs 신규 비교",
            severity="soft",
            passed=has_compare,
            detail=("비교 문장 있음" if has_compare else "비교 문장 부재"),
        )
    )

    # ---- G6 Budget reality ----
    cash_called = detect_cash_balance_call(md)
    total_m = sum_order_amount(md)
    cash_m = cash_override_m if cash_override_m is not None else parse_actual_cash(md)
    disclosed = detect_budget_disclosure(md)
    over_ratio = (total_m / cash_m) if (total_m and cash_m) else None
    over_budget = over_ratio is not None and over_ratio > 1.5
    # If caller supplied cash_override_m, treat that as positive evidence the
    # CIO (or e2e test) already resolved the cash balance out-of-band — the
    # Scout report no longer needs to prove it via regex.
    cash_called_effective = cash_called or cash_override_m is not None
    g6_passed = cash_called_effective and (not over_budget or disclosed)
    detail_bits = [f"get_cash_balance: {'있음' if cash_called_effective else '없음'}"]
    if cash_override_m is not None and not cash_called:
        detail_bits[0] += " (caller-supplied)"
    if total_m:
        detail_bits.append(f"주문안 총액 ~₩{total_m:.1f}M")
    if cash_m:
        detail_bits.append(f"예수금 ~₩{cash_m:.2f}M")
    if over_ratio:
        detail_bits.append(f"배수 {over_ratio:.2f}x")
    detail_bits.append(f"disclosure: {'있음' if disclosed else '없음'}")
    results.append(
        GateResult("G6", "Budget reality", "hard", g6_passed, " / ".join(detail_bits))
    )

    return results


# ---------------------------------------------------------------------------
# Reopen comment builder + rendering
# ---------------------------------------------------------------------------


REOPEN_TEMPLATE = """## Scout reopen 요청 — same-depth gate 위반

- 위반 gate: {violations}
- 구체 사항:
{details}
- 재요청 범위: 위반 gate에서 지적된 후보 deep-dive + 누락된 disclosure 보충
- 기대 산출물: §3 checklist 기준 `same-depth-check = pass` Scout Report v2
"""


def build_reopen_comment(results: list[GateResult]) -> str | None:
    hard_failed = [r for r in results if r.severity == "hard" and not r.passed]
    if not hard_failed:
        return None
    violations = ", ".join(f"{r.key} {r.label}" for r in hard_failed)
    detail_lines = [f"  - {r.key} {r.label}: {r.detail}" for r in hard_failed]
    return REOPEN_TEMPLATE.format(
        violations=violations, details="\n".join(detail_lines)
    )


def render_report(cands: list[Candidate], results: list[GateResult]) -> str:
    out: list[str] = []
    out.append("# CIO Quality Gate — Scout Report Sweep")
    out.append("")
    out.append(f"추출 후보: **{len(cands)}건**")
    out.append("")

    if cands:
        out.append("## 후보별 same-depth-check")
        out.append("")
        out.append(
            "| 종목 (코드) | 신규 | #1 Src | #2 Qt | #3 Ind | #4 S/R | #5 News | #6 Fund | #7 Exec | #8 Cmp | 판정 |"
        )
        out.append("|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|---|")
        for c in sorted(cands, key=lambda x: (not x.is_new, x.code)):
            marks = ["✅" if c.items.get(k) else "❌" for k in range(1, 9)]
            new_mark = "🆕" if c.is_new else "—"
            label = f"{c.name}"
            out.append(
                f"| {label} | {new_mark} | {marks[0]} | {marks[1]} | {marks[2]} | "
                f"{marks[3]} | {marks[4]} | {marks[5]} | {marks[6]} | {marks[7]} | {c.verdict} |"
            )
        out.append("")

    out.append("## Gate 판정")
    out.append("")
    for r in results:
        icon = "✅" if r.passed else ("❌" if r.severity == "hard" else "⚠️")
        out.append(f"- {icon} **{r.key} {r.label}** ({r.severity}) — {r.detail}")
    out.append("")

    hard_failed = [r for r in results if r.severity == "hard" and not r.passed]
    soft_failed = [r for r in results if r.severity == "soft" and not r.passed]
    if hard_failed:
        out.append(f"**결론**: REOPEN — hard-gate {len(hard_failed)}건 위반")
    elif soft_failed:
        out.append(
            f"**결론**: ACCEPT-WITH-FLAG — soft-gate {len(soft_failed)}건 위반 "
            "(본문에 한계 명시 필요)"
        )
    else:
        out.append("**결론**: ACCEPT — 모든 gate 통과")

    reopen = build_reopen_comment(results)
    if reopen:
        out.append("")
        out.append("## Reopen 코멘트 템플릿")
        out.append("")
        out.append("```markdown")
        out.append(reopen.rstrip())
        out.append("```")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Top-level public API
# ---------------------------------------------------------------------------


def _gates_to_violations(results: list[GateResult]) -> list[GateViolation]:
    out: list[GateViolation] = []
    for r in results:
        if r.passed:
            continue
        out.append(
            GateViolation(
                gate_id=r.key,  # type: ignore[arg-type]
                severity=r.severity,  # type: ignore[arg-type]
                detail=r.detail,
            )
        )
    return out


def _classify(results: list[GateResult]) -> OverallStatus:
    hard_failed = any(r.severity == "hard" and not r.passed for r in results)
    if hard_failed:
        return "FAIL"
    any_failed = any(not r.passed for r in results)
    return "PARTIAL" if any_failed else "PASS"


def evaluate_scout_report(
    markdown: str,
    *,
    cash_balance: float | None = None,
    tool_failures: list[str] | None = None,
) -> QualityGateReport:
    """Run the full G1~G6 quality gate and return a structured report.

    Args:
        markdown: Scout Report markdown body.
        cash_balance: Actual cash balance in **KRW** (not millions). When
            provided, G6 uses this as ground truth instead of parsing the
            report body and credits the call as "caller-supplied".
        tool_failures: Additional tool failure signals known to the caller
            (e.g. from the wake payload). Merged with signals auto-detected
            from the markdown for G3 disclosure check.
    """
    cands = extract_candidates(markdown)
    cash_override_m = (cash_balance / 1_000_000) if cash_balance is not None else None
    results = run_gates(
        markdown,
        cands,
        cash_override_m=cash_override_m,
        extra_tool_failures=tool_failures,
    )
    return QualityGateReport(
        overall_status=_classify(results),
        violations=_gates_to_violations(results),
        reopen_comment=build_reopen_comment(results),
        gates=results,
        candidates=cands,
    )


__all__ = [
    "Candidate",
    "GateResult",
    "GateViolation",
    "QualityGateReport",
    "evaluate_scout_report",
    "extract_candidates",
    "run_gates",
    "build_reopen_comment",
    "render_report",
    "detect_limitations_section",
    "detect_tool_failure_signals",
    "detect_grouped_rejection",
    "detect_cash_balance_call",
    "detect_budget_disclosure",
    "sum_order_amount",
    "parse_actual_cash",
    "has_keyword",
    "CHECKLIST_KEY",
    "CHECKLIST_LABELS",
    "EXEC_QUALIFIER_RE",
]
