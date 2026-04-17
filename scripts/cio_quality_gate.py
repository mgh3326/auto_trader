#!/usr/bin/env python3
"""CIO Scout Report Quality Gate checker.

Parses a Scout Report markdown file and runs the ROB-170 same-depth-check
per-row formula + G1~G6 gate sweep. Output is CIO-runbook shaped markdown
(or JSON with --json).

Usage:
    uv run python scripts/cio_quality_gate.py path/to/scout_report.md
    uv run python scripts/cio_quality_gate.py --stdin < scout.md
    uv run python scripts/cio_quality_gate.py --paperclip-issue ROB-158

Exit codes:
    0 = all gates pass (ACCEPT)
    1 = soft-gate only fail (ACCEPT-WITH-FLAG)
    2 = hard-gate fail (REOPEN)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

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
        r"\bNaver\b",
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


def _extract_execution_cell(row_line: str) -> str:
    """Return the trailing cell text from a markdown pipe row."""
    cells = [c.strip() for c in row_line.strip().strip("|").split("|")]
    return cells[-1] if cells else ""


def extract_candidates(md: str) -> list[Candidate]:
    """Scan the markdown for candidate rows and dedup/merge by 6-digit code.

    Any markdown table row containing a 6-digit code is treated as a candidate.
    Sub-line bullets ("|   | • ...") immediately following the row are folded
    into the same row_text for keyword detection (ROB-170 §6.2 v2 format).
    """
    lines = md.splitlines()
    by_code: dict[str, Candidate] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
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
        # v2 sub-line bullets
        while j < len(lines):
            nxt = lines[j]
            if nxt.lstrip().startswith("|") and (
                "•" in nxt
                or nxt.count("|") >= 3
                and not CODE_RE.search(nxt)
                and not re.search(
                    r"\b\d+[,.]?\d*",
                    nxt.split("|")[1] if len(nxt.split("|")) > 1 else "",
                )
            ):
                # Only accept as sub-line if it does NOT have another code
                if CODE_RE.search(nxt):
                    break
                if nxt.count("|") >= 3:
                    # Looks like another table row, stop
                    break
                row_parts.append(nxt)
                j += 1
            else:
                break

        row_text = "\n".join(row_parts)

        # Derive name: first non-empty cell containing the code
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        name_cell = next(
            (c for c in cells if CODE_RE.search(c)), cells[0] if cells else code
        )
        name = re.sub(r"\*+", "", name_cell).strip()
        is_new = "신규" in name_cell or "신규" in cells[0] if cells else False

        exec_cell = _extract_execution_cell(line)

        if code in by_code:
            c = by_code[code]
            c.row_texts.append(row_text)
            c.is_new = c.is_new or is_new
            # Merge execution cell: keep the most qualified one
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

    # Run item detection after merging
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
    """Return (has_grouped, has_exception_breakdown).

    Heuristic: if a grouped-rejection phrase contains trailing "등)" (truncated
    list), treat it as missing exception breakdown.
    """
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


@dataclass
class GateResult:
    key: str
    label: str
    severity: str  # hard / soft
    passed: bool
    detail: str = ""


def run_gates(
    md: str, cands: list[Candidate], cash_override: float | None = None
) -> list[GateResult]:
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
    # [신규] candidate must have a qualified execution path (not bare "KIS" / "Toss").
    no_qual = [
        c
        for c in cands
        if c.is_new and not EXEC_QUALIFIER_RE.search(c.execution_cell or c.context_text)
    ]
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
    # Also accept section-level comparison text
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
    cash_m = cash_override if cash_override is not None else parse_actual_cash(md)
    disclosed = detect_budget_disclosure(md)
    over_ratio = (total_m / cash_m) if (total_m and cash_m) else None
    over_budget = over_ratio is not None and over_ratio > 1.5
    g6_passed = cash_called and (not over_budget or disclosed)
    detail_bits = [f"get_cash_balance: {'있음' if cash_called else '없음'}"]
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
# Entrypoints
# ---------------------------------------------------------------------------


def load_from_paperclip(issue_id: str) -> str:
    api_url = os.environ.get("PAPERCLIP_API_URL")
    api_key = os.environ.get("PAPERCLIP_API_KEY")
    if not (api_url and api_key):
        raise SystemExit(
            "PAPERCLIP_API_URL and PAPERCLIP_API_KEY must be set to use "
            "--paperclip-issue"
        )
    cmd = [
        "curl",
        "-sS",
        "-H",
        f"Authorization: Bearer {api_key}",
        f"{api_url}/api/issues/{issue_id}/comments",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    comments = json.loads(result.stdout)
    if not isinstance(comments, list) or not comments:
        raise SystemExit(f"No comments found on {issue_id}")
    largest = max(comments, key=lambda c: len(c.get("body", "")))
    return largest["body"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("path", nargs="?", help="Path to Scout Report markdown file")
    src.add_argument("--stdin", action="store_true", help="Read markdown from stdin")
    src.add_argument(
        "--paperclip-issue",
        dest="paperclip_issue",
        help="Fetch largest comment from a Paperclip issue (e.g. ROB-158)",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON")
    p.add_argument(
        "--cash",
        type=float,
        default=None,
        help="Override actual cash balance (₩M) for G6 ratio computation",
    )
    args = p.parse_args(argv)

    if args.stdin:
        md = sys.stdin.read()
    elif args.paperclip_issue:
        md = load_from_paperclip(args.paperclip_issue)
    else:
        md = Path(args.path).read_text(encoding="utf-8")

    cands = extract_candidates(md)
    results = run_gates(md, cands, cash_override=args.cash)

    if args.as_json:
        payload = {
            "candidates": [
                {
                    "name": c.name,
                    "code": c.code,
                    "is_new": c.is_new,
                    "execution_cell": c.execution_cell,
                    "items": {str(k): bool(c.items.get(k)) for k in range(1, 9)},
                    "verdict": c.verdict,
                }
                for c in cands
            ],
            "gates": [
                {
                    "key": r.key,
                    "label": r.label,
                    "severity": r.severity,
                    "passed": r.passed,
                    "detail": r.detail,
                }
                for r in results
            ],
            "reopen_comment": build_reopen_comment(results),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_report(cands, results))

    hard_failed = any(r.severity == "hard" and not r.passed for r in results)
    any_failed = any(not r.passed for r in results)
    if hard_failed:
        return 2
    if any_failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
