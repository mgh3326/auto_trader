"""Markdown tables (Korean headings) from ``results.json`` and ``dev_records.json``.

Aggregates only: no raw candle, no per-signal row beyond the five worst / best
signals per selected configuration. Used to assemble the committed report.
"""

from __future__ import annotations

from typing import Any

from . import rules as R

FAMILY_KO = {
    "A_breakout": "A 신규 추세 돌파",
    "B_held_rebound": "B 보유 코인 반등",
    "C_downside": "C 하방 이탈",
}
HORIZON_KO = {4: "1시간", 16: "4시간", 96: "24시간"}


def pct(x: float | None, digits: int = 2) -> str:
    return "-" if x is None else f"{x * 100:+.{digits}f}%"


def pct_plain(x: float | None, digits: int = 1) -> str:
    return "-" if x is None else f"{x * 100:.{digits}f}%"


def ci(pair: list[float | None] | None) -> str:
    if not pair or pair[0] is None:
        return "-"
    return f"[{pair[0] * 100:+.2f}%, {pair[1] * 100:+.2f}%]"


def table(headers: list[str], rows: list[list[str]]) -> str:
    out = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _find(records: list[dict], **kw: Any) -> dict | None:
    for r in records:
        if all(r.get(k) == v for k, v in kw.items()):
            return r
    return None


def grid_table(records: list[dict], half: str, h: int = R.PRIMARY_H) -> str:
    rows = []
    for cfg in R.all_configs():
        r = _find(
            records,
            half=half,
            config=cfg.id,
            h=h,
            delay=0,
            scenario="base",
            bucket=None,
        )
        if r is None:
            continue
        rows.append(
            [
                FAMILY_KO[cfg.family],
                f"N{cfg.n} X{cfg.x:g}",
                str(r["n_used"]),
                str(r["n_days"]),
                pct(r["mean_net"]),
                pct(r["ctrl_b_mean_net"]),
                pct(r["excess_b_mean"]),
                ci(r.get("excess_b_ci95")),
                pct(r["excess_a_mean"]),
                pct_plain(r["hit_rate"]),
            ]
        )
    return table(
        [
            "계열",
            "설정",
            "신호",
            "거래일",
            "순수익 평균",
            "대조 B 평균",
            "초과(B)",
            "초과(B) 95% 구간",
            "초과(A)",
            "적중률",
        ],
        rows,
    )


def verdict_table(results: dict[str, Any]) -> str:
    rows = []
    for family in R.FAMILIES:
        sel = results["selection"]["selected"][family]
        v = results["verdicts"][family]
        vf = results["verdicts_warning_filter"][family]
        rec = None
        if sel:
            rec = _find(
                results["selected_records"],
                variant="all",
                family=family,
                half="test",
                h=R.PRIMARY_H,
                delay=0,
                scenario="base",
                bucket=None,
            )
        rows.append(
            [
                FAMILY_KO[family],
                "-" if not sel else f"N{sel['n']} X{sel['x']:g}",
                v["verdict"],
                "-" if rec is None else str(rec["n_used"]),
                "-" if rec is None else pct(rec["mean_net"]),
                "-" if rec is None else pct(rec["excess_b_mean"]),
                "-" if rec is None else ci(rec.get("excess_b_ci95")),
                "-" if rec is None else pct(rec["excess_a_mean"]),
                vf["verdict"],
            ]
        )
    return table(
        [
            "계열",
            "선택 설정",
            "판정",
            "시험 신호",
            "순수익 평균",
            "초과(B)",
            "초과(B) 95% 구간",
            "초과(A)",
            "필터 있음 판정",
        ],
        rows,
    )


def horizon_table(
    results: dict[str, Any], half: str = "test", variant: str = "all"
) -> str:
    rows = []
    for family in R.FAMILIES:
        if not results["selection"]["selected"][family]:
            continue
        for h in R.HORIZONS:
            r = _find(
                results["selected_records"],
                variant=variant,
                family=family,
                half=half,
                h=h,
                delay=0,
                scenario="base",
                bucket=None,
            )
            if r is None:
                continue
            rows.append(
                [
                    FAMILY_KO[family],
                    HORIZON_KO[h],
                    str(r["n_used"]),
                    pct(r["mean_gross"]),
                    pct(r["mean_net"]),
                    pct(r["median_net"]),
                    pct_plain(r["hit_rate"]),
                    pct_plain(r["mae_mean"]),
                    pct_plain(r["mae_p90"]),
                    pct(r["excess_b_mean"]),
                    ci(r.get("excess_b_ci95")),
                    pct(r["excess_a_mean"]),
                ]
            )
    return table(
        [
            "계열",
            "지평",
            "신호",
            "총수익",
            "순수익 평균",
            "순수익 중앙값",
            "적중률",
            "MAE 평균",
            "MAE p90",
            "초과(B)",
            "초과(B) 95% 구간",
            "초과(A)",
        ],
        rows,
    )


def bucket_table(results: dict[str, Any], half: str = "test") -> str:
    rows = []
    for family in R.FAMILIES:
        if not results["selection"]["selected"][family]:
            continue
        for b in R.BUCKET_NAMES:
            r = _find(
                results["selected_records"],
                variant="all",
                family=family,
                half=half,
                h=R.PRIMARY_H,
                delay=0,
                scenario="base",
                bucket=b,
            )
            if r is None or r["n_signals"] == 0:
                continue
            rows.append(
                [
                    FAMILY_KO[family],
                    b,
                    str(r["n_used"]),
                    pct(r["mean_net"]),
                    pct_plain(r["hit_rate"]),
                    pct(r["excess_b_mean"]),
                    ci(r.get("excess_b_ci95")),
                    pct(r["excess_a_mean"]),
                ]
            )
    return table(
        [
            "계열",
            "거래대금 구간",
            "신호",
            "순수익 평균",
            "적중률",
            "초과(B)",
            "초과(B) 95% 구간",
            "초과(A)",
        ],
        rows,
    )


def sensitivity_table(results: dict[str, Any], half: str = "test") -> str:
    rows = []
    for family in R.FAMILIES:
        if not results["selection"]["selected"][family]:
            continue
        for label, kw in (
            ("기본 비용", {"variant": "all", "delay": 0, "scenario": "base"}),
            ("수수료만", {"variant": "all", "delay": 0, "scenario": "fee_only"}),
            ("슬리피지 2배", {"variant": "all", "delay": 0, "scenario": "heavy"}),
            ("한 봉 늦은 경보", {"variant": "all", "delay": 1, "scenario": "base"}),
            (
                "경고·주의 마켓 제외",
                {"variant": "warning_filter", "delay": 0, "scenario": "base"},
            ),
        ):
            r = _find(
                results["selected_records"],
                family=family,
                half=half,
                h=R.PRIMARY_H,
                bucket=None,
                **kw,
            )
            if r is None:
                continue
            rows.append(
                [
                    FAMILY_KO[family],
                    label,
                    str(r["n_used"]),
                    pct(r["mean_net"]),
                    pct_plain(r["hit_rate"]),
                    pct(r["excess_b_mean"]),
                    ci(r.get("excess_b_ci95")),
                    pct(r["excess_a_mean"]),
                ]
            )
    return table(
        [
            "계열",
            "시나리오(4시간)",
            "신호",
            "순수익 평균",
            "적중률",
            "초과(B)",
            "초과(B) 95% 구간",
            "초과(A)",
        ],
        rows,
    )


def listing_table(results: dict[str, Any]) -> str:
    rows = []
    for family, item in results["listings"].items():
        for kind, label in (("worst", "최악"), ("best", "최선")):
            for e in item[kind]:
                rows.append(
                    [
                        FAMILY_KO[family],
                        label,
                        e["market"],
                        e["entry_kst"],
                        pct(e["net"]),
                    ]
                )
    return table(["계열", "구분", "마켓", "진입 시각(KST)", "4시간 순수익"], rows)


def counts_table(results: dict[str, Any]) -> str:
    rows = []
    for cfg in R.all_configs():
        c = results["grid_counts"][cfg.id]
        rows.append(
            [
                FAMILY_KO[cfg.family],
                f"N{cfg.n} X{cfg.x:g}",
                str(c["dev"]),
                str(c["test"]),
                f"{c['entry_missing']['dev']}/{c['entry_missing']['test']}",
                str(c["thin_control_b"]),
                str(c["thin_control_a"]),
            ]
        )
    return table(
        [
            "계열",
            "설정",
            "개발 신호",
            "시험 신호",
            "진입 봉 없음(개발/시험)",
            "대조 B 풀 부족",
            "대조 A 풀 부족",
        ],
        rows,
    )
