from __future__ import annotations

import pytest

from scripts.news_quality_baseline import parse_args, run_baseline_on_articles


def test_news_quality_baseline_us_metrics_are_pure_and_aggregate():
    report = run_baseline_on_articles(
        "us",
        [
            {
                "title": "S&P 500 rises as big tech climbs with Apple and Microsoft",
                "summary": "The Nasdaq gained broadly as Apple and Microsoft advanced.",
                "keywords": [],
                "feed_source": "fixture",
            },
            {
                "title": "Apple reports record earnings and raises guidance",
                "summary": "Apple revenue beat expectations.",
                "keywords": [],
                "feed_source": "fixture",
            },
        ],
    )

    assert report["sample_count"] == 2
    assert report["scope_distribution"]["market_wide"] == 1
    assert report["scope_distribution"]["symbol_specific"] == 1
    assert report["big_tech_fp_rate_before"] == 0.5
    assert report["top_sources"] == {"fixture": 2}


def test_news_quality_baseline_crypto_metrics_are_pure_and_aggregate():
    report = run_baseline_on_articles(
        "crypto",
        [
            {
                "title": "Bitcoin open interest jumps as funding rate turns positive",
                "summary": "BTC futures liquidation risk rises.",
                "feed_source": "rss_coindesk",
                "keywords": ["BTC"],
            },
            {
                "title": "OpenAI releases new coding model",
                "summary": "Developer tool ships without blockchain or token support.",
                "feed_source": "rss_decrypt",
                "keywords": [],
            },
        ],
    )

    assert report["sample_count"] == 2
    assert report["include_count"] == 1
    assert report["noise_reason_distribution"]["broad_tech_without_crypto_signal"] == 1
    assert report["supported_universe_coverage_pct"] == 50.0


def test_news_quality_baseline_rejects_invalid_market():
    with pytest.raises(SystemExit):
        parse_args(["--markets", "us,kr"])
