from __future__ import annotations

import json

import pytest

from scripts import news_issue_lab_quality_eval as quality_eval


@pytest.mark.asyncio
async def test_tag_precision_mode_writes_summary_artifacts(tmp_path):
    us_labels = tmp_path / "us.jsonl"
    crypto_labels = tmp_path / "crypto.jsonl"
    us_labels.write_text(
        '{"title":"S&P 500 rallies as Apple and Microsoft climb","summary":"Big tech led the Nasdaq higher.","expected_scope":"market_wide","expected_demoted":["AAPL","MSFT"]}\n'
        '{"title":"Apple reports record earnings","summary":"Apple guidance rose.","expected_scope":"symbol_specific","expected_demoted":[]}\n',
        encoding="utf-8",
    )
    crypto_labels.write_text(
        '{"title":"Bitcoin open interest jumps as funding rate turns positive","summary":"BTC futures positioning rose.","feed_source":"rss_coindesk","expected_include":true,"expected_category":"funding_onchain"}\n'
        '{"title":"OpenAI releases new coding model","summary":"No blockchain or token support.","feed_source":"rss_decrypt","expected_include":false,"expected_noise_reason":"broad_tech_without_crypto_signal"}\n',
        encoding="utf-8",
    )

    rc = await quality_eval.async_main(
        [
            "--mode",
            "tag-precision",
            "--us-labels",
            str(us_labels),
            "--crypto-labels",
            str(crypto_labels),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    assert rc == 0
    summary = json.loads(
        (tmp_path / "out" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["mode"] == "tag-precision"
    assert summary["safety"]["read_only"] is True
    assert summary["us"]["sample_count"] == 2
    assert summary["crypto"]["sample_count"] == 2
    assert (tmp_path / "out" / "summary.md").exists()


def test_tag_precision_us_evaluation_checks_demoted_symbols():
    rows = [
        {
            "title": "S&P 500 rallies as Apple and Microsoft climb",
            "summary": "Big tech led the Nasdaq higher.",
            "expected_scope": "market_wide",
            "expected_demoted": ["AAPL", "MSFT"],
        }
    ]

    result = quality_eval._run_us_tag_precision(rows)

    assert result["sample_count"] == 1
    assert result["demotion_accuracy"] == 1.0
    assert result["precision"] == 1.0
