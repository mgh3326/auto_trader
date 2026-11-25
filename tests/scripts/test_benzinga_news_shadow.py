"""hk 6395: benzinga_news_shadow CLI tests (no network, no DB)."""

from __future__ import annotations

import json

import pytest

import scripts.benzinga_news_shadow as cli
from app.services import benzinga_news as bz


def _enable(monkeypatch: pytest.MonkeyPatch, key: str = "TEST-KEY") -> None:
    monkeypatch.setenv(bz.SHADOW_ENABLED_ENV, "true")
    monkeypatch.setenv(bz.API_KEY_ENV, key)


def test_gate_off_exits_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    monkeypatch.delenv(bz.SHADOW_ENABLED_ENV, raising=False)
    monkeypatch.delenv(bz.API_KEY_ENV, raising=False)

    async def _boom(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("network call made while gated off")

    monkeypatch.setattr(bz, "fetch_benzinga_news", _boom)
    code = cli.main(
        ["poll", "--symbols", "NVDA", "--out-dir", str(tmp_path), "--poll-id", "p-gate"]
    )
    assert code == 2
    assert list(tmp_path.iterdir()) == []


def test_out_dir_inside_repo_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _enable(monkeypatch)

    async def _boom(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("network call made for refused out-dir")

    monkeypatch.setattr(bz, "fetch_benzinga_news", _boom)
    code = cli.main(
        ["poll", "--symbols", "NVDA", "--out-dir", "docs", "--poll-id", "p-refuse"]
    )
    assert code == 2
    assert "outside the repo" in capsys.readouterr().err


def test_poll_writes_append_only_jsonl(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    _enable(monkeypatch)

    async def _fake_fetch(ticker, **kwargs):
        return [
            bz.normalize_record(
                {
                    "id": 1,
                    "title": f"{ticker} story",
                    "url": f"https://x.com/{ticker.lower()}",
                    "created": "Tue, 25 Nov 2025 09:30:00 -0400",
                    "updated": "Tue, 25 Nov 2025 09:30:00 -0400",
                    "stocks": [ticker],
                    "channels": ["News"],
                },
                poll_id=kwargs["poll_id"],
                first_received_at=kwargs["now"],
            )
        ]

    monkeypatch.setattr(bz, "fetch_benzinga_news", _fake_fetch)
    out_dir = tmp_path / "shadow"
    argv = [
        "poll",
        "--symbols",
        "NVDA",
        "--out-dir",
        str(out_dir),
        "--poll-id",
        "p1",
        "--skip-finnhub",
    ]
    assert cli.main(argv) == 0
    assert cli.main(argv) == 0  # second run appends
    lines = (out_dir / "p1.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["source"] == "benzinga"
    assert first["created_utc"] == "2025-11-25T13:30:00+00:00"
    assert first["poll_id"] == "p1"
    assert "TEST-KEY" not in (out_dir / "p1.jsonl").read_text(encoding="utf-8")


def test_poll_explicit_zero_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    """A symbol with no coverage is recorded as an explicit zero (the
    tickers=NVDA anomaly), not as silence."""

    _enable(monkeypatch)

    async def _empty(ticker, **kwargs):
        return []

    monkeypatch.setattr(bz, "fetch_benzinga_news", _empty)
    code = cli.main(
        [
            "poll",
            "--symbols",
            "NVDA",
            "--out-dir",
            str(tmp_path),
            "--poll-id",
            "p-zero",
            "--skip-finnhub",
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["counts"] == {"NVDA": {"benzinga": 0, "finnhub": 0}}


def test_summary_reports_overlap_unique_latency(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    _enable(monkeypatch)
    rows = [
        {
            "source": "benzinga",
            "url": "https://x.com/a?x=1",
            "title": "Shared",
            "poll_id": "p9",
            "first_received_at": "2025-11-25T14:00:20+00:00",
        },
        {
            "source": "benzinga",
            "url": "https://x.com/b-only",
            "title": "B only",
            "poll_id": "p9",
            "first_received_at": "2025-11-25T14:00:20+00:00",
        },
        {
            "source": "finnhub",
            "url": "https://x.com/a",
            "title": "Shared",
            "poll_id": "p9",
            "first_received_at": "2025-11-25T14:00:00+00:00",
        },
    ]
    path = tmp_path / "p9.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert cli.main(["summary", "--input", str(path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["overlap_articles"] == 1
    assert summary["benzinga_unique"] == 1
    assert summary["finnhub_unique"] == 0
    assert summary["latency_seconds"]["min"] == 20.0
