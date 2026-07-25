"""ROB-941 (AC9) — opt-in LIVE network test against data.binance.vision.

Skipped by default (no ``ROB941_RUN_LIVE=1`` env var): the default suite is
fixture-only and network-free, per the AC9 requirement. This test does not use
the repo-root ``tests/conftest.py`` ``--run-live`` flag because
``research/nautilus_scalping`` is a separate pytest subtree (its own
``conftest.py``, not collected under the root ``tests/`` path) — adding a second
``--run-live`` ``addoption`` here would collide if both trees were ever
collected together, so this uses a scoped env-var gate instead of touching the
shared ``research/nautilus_scalping/conftest.py``.

Run explicitly:
    ROB941_RUN_LIVE=1 uv run python -m pytest \\
        research/nautilus_scalping/tests/test_rob941_corpus_builder_live.py -v
"""

import os

import pytest
import rob941_archive_fetch as af
import rob941_corpus_builder as cb
import rob941_kline_schema as ks

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("ROB941_RUN_LIVE") != "1",
        reason="live network test: set ROB941_RUN_LIVE=1 to run",
    ),
]


def test_fetch_and_verify_one_real_month_of_xrpusdt_klines():
    url = af.kline_archive_url("XRPUSDT", "1m", 2025, 7)
    fetched = af.fetch_verified_archive(url)  # real urllib_opener, real checksum
    csv_text = af.extract_single_csv(fetched.zip_bytes)
    rows = ks.parse_kline_csv(
        "XRPUSDT", csv_text, 1751328000000, 1751328000000 + 31 * 86_400_000
    )
    assert len(rows) > 40_000  # a full month of 1m bars, minus any real exchange gaps
    assert rows[0].open_time_ms == 1751328000000


def test_fetch_and_verify_one_real_month_of_funding_rate():
    url = af.funding_archive_url("XRPUSDT", 2025, 7)
    fetched = af.fetch_verified_archive(url)
    csv_text = af.extract_single_csv(fetched.zip_bytes)
    from funding_oi_archive import parse_funding_csv

    rows = parse_funding_csv(csv_text)
    assert len(rows) >= 60  # ~90 events/month at 8h cadence, allow for interval changes


def test_build_symbol_kline_shard_against_real_binance_first_month_only(monkeypatch):
    # bound the live RUN to a single month so this smoke test stays fast; the full
    # 12-month x 4-symbol corpus is built by the operator-gated build_rob941_corpus.py
    monkeypatch.setattr("rob941_frozen_scope.months_in_window", lambda: [(2025, 7)])
    rows, provenance, gap_ranges = cb.build_symbol_kline_shard("XRPUSDT")
    assert len(provenance) == 1
    assert provenance[0].checksum_sha256
    assert len(rows) > 40_000
