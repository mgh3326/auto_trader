"""ROB-384 — runner tests (self-contained; no dependency on gitignored artifacts)."""

from __future__ import annotations

import csv
import json

from external_strategy_sieve.postmortem import runner


def test_build_records_skips_missing_sources(tmp_path):
    # Empty artifact root -> all 4 reparsed sources skipped, documented row remains.
    records, notes = runner.build_records(tmp_path)
    assert len(records) == 1  # only the documented ROB-342 row
    assert records[0].issue == "ROB-342"
    assert any("SKIPPED (missing)" in n for n in notes)
    assert sum(1 for n in notes if "SKIPPED" in n) == 4
    assert any("documented: 1 candidate" in n for n in notes)


def test_build_records_reparses_present_source(tmp_path):
    (tmp_path / "rob353").mkdir()
    camp = {
        "verdict_table": {
            "families": [
                {
                    "name": "family1_breakout_continuation",
                    "screen": "screened_out",
                    "cost_binding_screen": False,
                    "screen_reason": "OOS gross expectancy -70.99bps <= 0",
                }
            ]
        },
        "controls": {"btc_buy_hold_bps": 35938.6},
        "spec_sample_counts": {"family1_breakout_continuation": 1366},
    }
    (tmp_path / "rob353/rob351_campaign.v1.json").write_text(
        json.dumps(camp), encoding="utf-8"
    )
    records, notes = runner.build_records(tmp_path)
    assert len(records) == 2  # 1 reparsed family + 1 documented
    fam = [r for r in records if r.issue == "ROB-353"][0]
    assert fam.failure_modes == ["gross_zero"]
    assert any("reparsed: ROB-353" in n for n in notes)


def test_emit_csv_is_counts_only_and_sanitized(tmp_path):
    (tmp_path / "rob353").mkdir()
    (tmp_path / "rob353/rob351_campaign.v1.json").write_text(
        json.dumps(
            {
                "verdict_table": {
                    "families": [
                        {
                            "name": "f1",
                            "screen": "screened_out",
                            "cost_binding_screen": False,
                            "screen_reason": "gross expectancy -70.99bps <= 0",
                        }
                    ]
                },
                "controls": {"btc_buy_hold_bps": 35938.6},
                "spec_sample_counts": {"f1": 1366},
            }
        ),
        encoding="utf-8",
    )
    records, _ = runner.build_records(tmp_path)
    out = tmp_path / "out.csv"
    runner.emit_csv(records, out, tmp_path)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert {"issue", "gross_bps", "failure_modes", "citation"} <= set(rows[0].keys())
    # citation must be sanitized (no local artifact-root absolute path leaked)
    assert str(tmp_path) not in rows[0]["citation"]
    assert "<artifact_root>" in rows[0]["citation"]


def test_build_summary_has_decision_and_uncited_flag(tmp_path):
    records, notes = runner.build_records(tmp_path)
    summary = runner.build_summary(records, tmp_path, notes)
    assert summary["schema_version"] == "rob384_postmortem.v1"
    assert "verdict" in summary["closure_decision"]
    assert "NOT traceable" in summary["memory_only_uncited"]


def test_main_dry_run_no_writes(tmp_path, capsys):
    out_csv = tmp_path / "should_not_exist.csv"
    rc = runner.main(["--artifact-root", str(tmp_path), "--out-csv", str(out_csv)])
    assert rc == 0
    assert not out_csv.exists()  # no --emit -> no write
    assert "CLOSURE DECISION" in capsys.readouterr().out
