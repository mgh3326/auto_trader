"""Final report: every number comes from the stage JSONs written by ``run``.

Each policy's contract ``EvaluationReport`` is rebuilt here with the registry's
manifest hash and ``k`` and rendered next to the run; a registry entry with
``k < 1`` is refused by the contract itself, so no report can print before the
look is counted.
"""

from __future__ import annotations

import json
import pickle
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from research.eval_contract.contract import (
    BRACKET_CONSERVATIVE,
    BRACKET_OPTIMISTIC,
    EvaluationReport,
    Market,
    Provenance,
    RegistryRef,
    sha256_canonical,
)
from research.eval_contract.report import render_report
from research.kr_boosting import config as cfg
from research.kr_boosting import pipeline as pl
from research.kr_boosting.exposure_audit import provably_untouched, run_audit
from research.kr_boosting.gate import CONTINUE
from research.kr_boosting.panel import ExportIdentity

FILLS = (BRACKET_OPTIMISTIC, BRACKET_CONSERVATIVE, "volume_share")
_MARKER_HASH = "Experiment-Manifest-Sha256"
_MARKER_LOOK = "Experiment-Test-Look"


def _pct(value: Any, digits: int = 2) -> str:
    return "n/a" if value is None else f"{float(value) * 100:+.{digits}f}%"


def _plain(value: Any, digits: int = 1) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.{digits}f}%"


def _policy_table(section: Mapping[str, Any], order: tuple[str, ...]) -> list[str]:
    lines = [
        "| policy | optimistic, annual | conservative, annual | volume share, annual | conservative, total |",
        "|---|---:|---:|---:|---:|",
    ]
    for policy in order:
        row = section["policies"][policy]
        cells = [_pct(row[f]["net_vs_cash_annual"]) for f in FILLS]
        lines.append(
            f"| {policy} | {cells[0]} | {cells[1]} | {cells[2]} | "
            f"{_pct(row[BRACKET_CONSERVATIVE]['net_vs_cash_total'])} |"
        )
    return lines


def _diagnostic_table(section: Mapping[str, Any], order: tuple[str, ...]) -> list[str]:
    lines = [
        "| policy | fill | max drawdown | turnover / yr | mean deployed | entries | fill rate | mean net R5 | mean net R20 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    short = {BRACKET_OPTIMISTIC: "optimistic", BRACKET_CONSERVATIVE: "conservative"}
    for policy in order:
        for fill in (BRACKET_OPTIMISTIC, BRACKET_CONSERVATIVE):
            m = section["policies"][policy][fill]
            lines.append(
                f"| {policy} | {short[fill]} | {_pct(m['max_drawdown'])} | "
                f"{m['turnover_annual']:.1f}x | {_plain(m['deployed_mean'])} | {m['entries']} | "
                f"{_plain(m['fill_rate'])} | {_pct(m['mean_net_r5'])} | {_pct(m['mean_net_r20'])} |"
            )
    return lines


def _year_table(section: Mapping[str, Any], order: tuple[str, ...]) -> list[str]:
    years = sorted(section["policies"][order[0]][BRACKET_CONSERVATIVE]["by_year"])
    lines = [
        "| policy | " + " | ".join(years) + " |",
        "|---|" + "---:|" * len(years),
    ]
    for policy in order:
        by_year = section["policies"][policy][BRACKET_CONSERVATIVE]["by_year"]
        lines.append(
            f"| {policy} | " + " | ".join(_pct(by_year[y]) for y in years) + " |"
        )
    return lines


def _gate_table(verdict: Mapping[str, Any]) -> list[str]:
    lines = [
        "| gbm-baseline vs | annual difference | p raw | p Holm | passed |",
        "|---|---:|---:|---:|---|",
    ]
    for c in verdict["comparisons"]:
        lines.append(
            f"| {c['control']} | {_pct(c['annual_diff'])} | {c['p_raw']:.4f} | "
            f"{c['p_holm']:.4f} | {'yes' if c['passed'] else 'no'} |"
        )
    return lines


def _sensitivity(section: Mapping[str, Any]) -> list[str]:
    lines = [
        "| policy | fee 0.415% per side, annual | terminal write-off, annual | write-offs |",
        "|---|---:|---:|---|",
    ]
    for policy, row in section["sensitivity"].items():
        lines.append(
            f"| {policy} | {_pct(row['fee_0.415pct_per_side']['net_vs_cash_annual'])} | "
            f"{_pct(row['terminal_writeoff']['net_vs_cash_annual'])} | "
            f"{row['terminal_writeoff']['terminal_writeoffs']} |"
        )
    return lines


def _render_contract_reports(
    outputs: Mapping[str, Any],
    *,
    hypothesis_id: str,
    registry: RegistryRef,
    data_sha: str,
    code_ref: str,
    label: str,
    root: Path,
) -> dict[str, str]:
    """Render one contract report per policy; return policy -> report sha256."""

    shas: dict[str, str] = {}
    target = root / "contract_reports" / label
    target.mkdir(parents=True, exist_ok=True)
    for policy, results in outputs.items():
        report = EvaluationReport(
            hypothesis_id=hypothesis_id,
            market=Market.KR,
            provenance=Provenance(
                code_ref=code_ref,
                data_manifest_sha256=data_sha,
                config_sha256=sha256_canonical(
                    {"policy": policy, "label": label, "grid": cfg.grid_sha256()}
                ),
                registry=registry,
            ),
            results=tuple(results.values()),
        )
        shas[policy] = report.sha256
        (target / f"{hypothesis_id}.{policy}.md").write_text(render_report(report))
    return shas


def _order(hypothesis_id: str) -> tuple[str, ...]:
    return (
        pl.POL_GBM,
        pl.unchanged_policy(hypothesis_id),
        pl.POL_MOMENTUM,
        pl.POL_REVERSION,
        pl.POL_CASH,
    )


def write_report(
    *, root: Path, repo: Path, out: Path, identity: ExportIdentity
) -> None:
    results = json.loads((root / "validate_results.json").read_text())
    diag = json.loads((root / "diagnostic_results.json").read_text())
    with (root / "validate_outputs.pkl").open("rb") as handle:
        validate_outputs = pickle.load(handle)
    with (root / "diagnostic_outputs.pkl").open("rb") as handle:
        diagnostic_outputs = pickle.load(handle)
    registry = json.loads((repo / "research/experiments/registry.json").read_text())
    entries = {h["id"]: h for h in registry["hypotheses"]}
    hits = run_audit(repo)
    git = results["git"]

    contract_shas: dict[str, dict[str, dict[str, str]]] = {}
    for hyp in (cfg.HYP_ADMIT, cfg.HYP_ENTRY):
        ref = RegistryRef(hyp, entries[hyp]["manifest_sha256"], int(entries[hyp]["k"]))
        contract_shas[hyp] = {
            label: _render_contract_reports(
                outputs[hyp],
                hypothesis_id=hyp,
                registry=ref,
                data_sha=identity.parquet_sha256,
                code_ref=git["head"],
                label=label,
                root=root,
            )
            for label, outputs in (
                ("validate", validate_outputs),
                ("diagnostic", diagnostic_outputs),
            )
        }

    verdicts = {
        h: results[h]["verdict"]["verdict"] for h in (cfg.HYP_ADMIT, cfg.HYP_ENTRY)
    }
    advancing = [h for h, v in verdicts.items() if v == CONTINUE]
    if advancing:
        overall = "CONTINUE to U8 TFT for " + " and ".join(advancing) + " only."
    else:
        overall = "STOP. Neither hypothesis passed; U8 TFT does not run for KR."

    L: list[str] = [
        "# U7-KR boosting baseline + frozen controls (ROB-1441)",
        "",
        f"{_MARKER_HASH}: {entries[cfg.HYP_ADMIT]['manifest_sha256']}",
        f"{_MARKER_HASH}: {entries[cfg.HYP_ENTRY]['manifest_sha256']}",
        f"{_MARKER_LOOK}: yes",
        "",
        "## Verdict",
        "",
        f"**{overall}**",
        "",
        "| hypothesis | gate verdict | conservative net vs cash, annual | optimistic | G1 cash | G2 controls | G3 stability | G4 bracket |",
        "|---|---|---:|---:|---|---|---|---|",
    ]
    for hyp in (cfg.HYP_ADMIT, cfg.HYP_ENTRY):
        v = results[hyp]["verdict"]
        yn = {True: "pass", False: "fail"}
        L.append(
            f"| {hyp} | **{v['verdict']}** | {_pct(v['net_vs_cash_annual_conservative'])} | "
            f"{_pct(v['net_vs_cash_annual_optimistic'])} | {yn[v['g1_net_vs_cash']]} | "
            f"{yn[v['g2_controls']]} | {yn[v['g3_stability']]} | {yn[v['g4_bracket']]} |"
        )
    L += [
        "",
        "The verdict rests on the 2019-2022 walk-forward path, conservative fill end. "
        "2023-2024 played no part in it.",
        "",
        "## Registry",
        "",
        "| hypothesis | status | manifest version | manifest sha256 | k |",
        "|---|---|---|---|---:|",
    ]
    for hyp in (cfg.HYP_ADMIT, cfg.HYP_ENTRY):
        e = entries[hyp]
        version = len(e["previous_manifest_sha256"]) + 1
        L.append(
            f"| {hyp} | {e['status']} | {version} | `{e['manifest_sha256']}` | {e['k']} |"
        )
    L += [
        "",
        "`k = 1` on each hypothesis counts the single 2023-2024 diagnostic read below. "
        "It is counted because the rules define any metric on those rows as a look; it is not "
        "a sealed test. The manifests stay `draft` only because their US and crypto sections "
        "are pending for those markets' own U7 units; every KR section is fixed and pinned by the hash.",
        "",
        f"- code at the run: `{git['head']}` (tree clean: {git['clean']})",
        f"- data: `kr_daily_causal.v1.parquet` sha256 `{identity.parquet_sha256}`, "
        f"export manifest sha256 `{identity.manifest_sha256}`",
        f"- configuration grid sha256: `{results['grid_sha256']}`; seeds {results['seeds']}",
        f"- frozen bundle sha256: `{results['frozen_bundle_sha256']}`",
        "",
        "## Exposure audit of 2023-2024",
        "",
    ]
    if provably_untouched(hits):
        raise ValueError(
            "audit found no exposure; this report assumes the period is exposed"
        )
    L += [
        "**Result: not provably untouched. 2023-2024 is diagnostics only and is not called a sealed test.** "
        "The test for these manifests is a prospective seal, unopened.",
        "",
        "| evidence | line | what it shows |",
        "|---|---:|---|",
    ]
    L += [f"| `{h.path}` | {h.line} | {h.meaning} |" for h in hits]
    L += [
        "",
        "Rerun with `research/kr_boosting/exposure_audit.py`.",
        "",
        "## Method",
        "",
        "- **Decision**: after the close of session t, orders at 08:30 KST of t+1. Features come only "
        "through `feature_join`, which refuses `available_at` after the decision time.",
        "- **Population**: the frozen D3 signal (RSI-14 below 45; fib and Bollinger confluence within "
        "-8%..-3% of the close; 120 contiguous bars), over the U2 universe including delisted listings. "
        "Bars carry no analyst data, so this is the bar-reproducible part of the live gate.",
        "- **Splits**: train 2015-2018 / validate 2019, expanding to validate 2022. In front of every "
        "boundary, purge then gap: 5+5 sessions for H-admit, 25+25 for H-entry-price.",
        f"- **Search**: {cfg.MAX_CONFIGS_PER_FAMILY} LightGBM configurations per family. The "
        "configuration for year Y is chosen on year Y-1 by model loss, so Y does not choose its own "
        f"model. The top {cfg.FINALISTS} refit with {len(cfg.SEEDS)} seeds; the top one's seed mean trades.",
        f"- **Frame for every policy**: capital {int(cfg.CAPITAL):,} KRW, {int(cfg.ORDER_NOTIONAL):,} KRW "
        f"per order, at most {cfg.MAX_NEW_PER_SESSION} new entries per session, one position per listing, "
        f"{float(cfg.FEE_RATE) * 100:.3f}% per side (D3 scalar, tax included), cash earns 0.",
        "- **H-admit**: DAY limit at prior close +2%, exit at the t+5 close. Admit when q50 clears the "
        "round trip and q10 is above -10%; rank by q50.",
        "- **H-entry-price**: DAY child orders at one ladder rung for up to 5 sessions, exit 20 sessions "
        "after the fill. Rung and rank by P(touch) x (xret - round trip); skip if not positive.",
        "- **Controls**: unchanged policy (D3 rank, nothing skipped; for H-entry-price the D3 L2 anchor), "
        f"momentum (top {cfg.MOMENTUM_LOOKBACK}-session return), mean reversion (lowest "
        f"{cfg.MEAN_REVERSION_LOOKBACK}-session return), both over names with median 20-session traded "
        f"value of at least {int(cfg.LIQUIDITY_FLOOR_KRW):,} KRW, plus cash.",
        f"- **Gate, fixed before the first fit**: conservative end, 2019-2022. G1 annual net vs cash above "
        f"{cfg.GATE_DELTA_MIN_ANNUAL * 100:.0f}%; G2 every control beaten by more than "
        f"{cfg.GATE_DELTA_MIN_ANNUAL * 100:.0f}% a year at Holm p ≤ {cfg.GATE_ALPHA} (circular block bootstrap, "
        f"{cfg.BOOTSTRAP_BLOCK}-session blocks); G3 ahead of cash and the unchanged policy in at least "
        f"{cfg.GATE_MIN_POSITIVE_YEARS} of 4 years; G4 positive at the optimistic end.",
        "",
    ]
    for hyp in (cfg.HYP_ADMIT, cfg.HYP_ENTRY):
        section = results[hyp]
        order = _order(hyp)
        d = diag[hyp]
        L += [
            f"## {hyp}: walk-forward 2019-2022",
            "",
            f"Decisions {section['decision_window'][0]} to {section['decision_window'][1]}. "
            f"Rows: {json.dumps(section['rows'])}.",
            "",
            "Net return vs cash at equal capital, primary metric:",
            "",
            *_policy_table(section, order),
            "",
            f"Gate comparisons (verdict **{section['verdict']['verdict']}**; ahead of cash in "
            f"{section['verdict']['positive_years_vs_cash']} of 4 years, of the unchanged policy in "
            f"{section['verdict']['positive_years_vs_unchanged']}):",
            "",
            *_gate_table(section["verdict"]),
            "",
            "P&L by year, share of starting capital, conservative end:",
            "",
            *_year_table(section, order),
            "",
            "Diagnostics:",
            "",
            *_diagnostic_table(section, order),
            "",
            "Stress, conservative end:",
            "",
            *_sensitivity(section),
            "",
            f"Calibration: `{json.dumps(section['calibration'])}`",
            "",
            f"Annual net vs cash by seed of the primary configuration: "
            f"{[_pct(x) for x in section['seed_net_vs_cash_annual']]}. By finalist: "
            f"{[_pct(x) for x in section['finalist_net_vs_cash_annual']]}. Neither enters the verdict.",
            "",
            f"Contract report sha256, gbm-baseline: `{contract_shas[hyp]['validate'][pl.POL_GBM]}`.",
            "",
            f"## {hyp}: 2023-2024, diagnostics only (not a sealed test)",
            "",
            f"Frozen models, no refit. Decisions {d['decision_window'][0]} to {d['decision_window'][1]}. "
            "This period does not change the verdict.",
            "",
            *_policy_table(d, order),
            "",
            *_diagnostic_table(d, order),
            "",
            f"Calibration: `{json.dumps(d['calibration'])}`",
            "",
        ]
    L += [
        "## Limits",
        "",
        "- **Prices are adjusted** (`price_mode=adjusted`). Ticks on adjusted prices are approximate, and "
        "the tick table is the seven-band table from the evaluation contract's fixtures, because the "
        "frozen D3 YAML is not on this host. Every policy shares this.",
        "- **`invalid_ohlc` sessions are unobserved.** U2 keeps no prices for them, and kr-corpus-v1 "
        "discloses they lean strongly to up-closes (+2.81% mean). Labels ending on one are unknown and "
        "orders cannot fill on one, which biases returns down for every policy.",
        "- **Terminal payout is an assumption.** U2 asserts no amount, so a delisted position settles at "
        "its last close; the write-off stress zeroes `residual_after_liquidation_trading` and `ambiguous`. "
        "A listing's known delisting is not used to block entries, because U2 dates many events only at the delist date.",
        "- **Fills at the daily open and close are idealised.** Near-marketable H-admit orders barely "
        "separate the bracket, so its cost and open-price risk are not captured by fill uncertainty; "
        "the fee stress is the relevant check. Volume share caps at 10% of the session's whole volume, not the auction's.",
        "- **No T+2 model.** Proceeds are usable next session.",
        "- **D3 is not the whole live gate.** No analyst upside, no NXT session, no per-symbol policy.",
        "- **Validation years are not independent of this work's design**: the frame and rules were chosen "
        "knowing ROB-1383's findings about the same market.",
        "",
        "## Reproduce",
        "",
        "```bash",
        'uv venv --python 3.13 "$ROOT/venv"',
        'VIRTUAL_ENV="$ROOT/venv" uv pip install -r research/kr_boosting/requirements-ml.txt',
        '"$ROOT/venv/bin/python" -m research.kr_boosting.run validate --export-dir "$EXPORT" --root "$ROOT/run"',
        '"$ROOT/venv/bin/python" -m research.kr_boosting.run diagnostic --export-dir "$EXPORT" --root "$ROOT/run"',
        '"$ROOT/venv/bin/python" -m research.kr_boosting.run report --export-dir "$EXPORT" --root "$ROOT/run" --report research/experiments/reports/rob-1441-kr-boosting-baseline.md',
        "```",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L))
