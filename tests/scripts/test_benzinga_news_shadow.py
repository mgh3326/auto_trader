"""hk 6395: benzinga_news_shadow CLI tests (no network, no DB)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

import scripts.benzinga_news_shadow as cli
from app.services import benzinga_news as bz


def _enable(monkeypatch: pytest.MonkeyPatch, key: str = "TEST-KEY") -> None:
    monkeypatch.setenv(bz.SHADOW_ENABLED_ENV, "true")
    monkeypatch.setenv(bz.API_KEY_ENV, key)


class _TickClock:
    """Deterministic clock: each now() is one second later."""

    def __init__(self) -> None:
        self.calls = 0

    def now(self, tz=None):  # noqa: ANN001, ANN202
        self.calls += 1
        return datetime(2025, 11, 25, 14, 0, self.calls, tzinfo=UTC)


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


def test_poll_id_traversal_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    _enable(monkeypatch)

    async def _boom(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("network call made for refused poll-id")

    monkeypatch.setattr(bz, "fetch_benzinga_news", _boom)
    for bad in ("../evil", "/abs/path", "a/b"):
        code = cli.main(
            ["poll", "--symbols", "NVDA", "--out-dir", str(tmp_path), "--poll-id", bad]
        )
        assert code == 2
        assert "[A-Za-z0-9._-]+" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def _benzinga_item(title: str, url: str) -> dict:
    return {
        "id": 1,
        "title": title,
        "url": url,
        "created": "Tue, 25 Nov 2025 09:30:00 -0400",
        "updated": "Tue, 25 Nov 2025 09:30:00 -0400",
        "stocks": ["NVDA"],
        "channels": ["News"],
    }


def test_poll_writes_append_only_jsonl(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    _enable(monkeypatch)

    async def _fake_fetch(ticker, **kwargs):
        return [
            bz.normalize_record(
                _benzinga_item(f"{ticker} story", f"https://x.com/{ticker.lower()}"),
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
    rows = [
        json.loads(line)
        for line in (out_dir / "p1.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    articles = [r for r in rows if r.get("record_type") == "article"]
    statuses = [r for r in rows if r.get("record_type") == "poll_status"]
    frames = [
        r for r in rows if r.get("record_type") in ("poll_header", "poll_completion")
    ]
    # Two runs x two request params per symbol.
    assert len(articles) == 4
    assert articles[0]["created_utc"] == "2025-11-25T13:30:00+00:00"
    assert articles[0]["poll_id"] == "p1"
    assert articles[0]["query_symbol"] == "NVDA"
    assert [a["query_param"] for a in articles] == [
        "primaryTickers",
        "tickers",
        "primaryTickers",
        "tickers",
    ]
    assert all(s["status"] == "ok" and s["source"] == "benzinga" for s in statuses)
    assert sorted(s["query_param"] for s in statuses) == [
        "primaryTickers",
        "primaryTickers",
        "tickers",
        "tickers",
    ]
    assert len(statuses) == 4
    assert [f["record_type"] for f in frames] == [
        "poll_header",
        "poll_completion",
        "poll_header",
        "poll_completion",
    ]
    assert frames[0]["symbols"] == ["NVDA"]
    assert frames[1]["symbols_completed"] == ["NVDA"]
    assert "TEST-KEY" not in (out_dir / "p1.jsonl").read_text(encoding="utf-8")


def test_poll_stamps_each_source_after_its_own_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    """One shared `now` per symbol would freeze latency at exactly 0.0."""
    _enable(monkeypatch)
    monkeypatch.setattr(cli, "datetime", _TickClock())

    async def _fake_fetch(ticker, **kwargs):
        return [
            bz.normalize_record(
                _benzinga_item("B story", "https://x.com/b"),
                poll_id=kwargs["poll_id"],
                first_received_at=kwargs["now"],
            )
        ]

    async def _fake_finnhub(symbol, market, limit):
        return {
            "news": [
                {
                    "title": "F story",
                    "url": "https://f.io/1",
                    "source": "Finnhub",
                    "datetime": "2025-11-25T14:00:00",
                    "summary": "s",
                }
            ]
        }

    monkeypatch.setattr(bz, "fetch_benzinga_news", _fake_fetch)
    import app.services.finnhub_news as finnhub_news

    monkeypatch.setattr(finnhub_news, "fetch_news_finnhub", _fake_finnhub)
    code = cli.main(
        [
            "poll",
            "--symbols",
            "NVDA",
            "--out-dir",
            str(tmp_path),
            "--poll-id",
            "p-stamp",
        ]
    )
    assert code == 0
    rows = [
        json.loads(line)
        for line in (tmp_path / "p-stamp.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    articles = {r["source"]: r for r in rows if r.get("record_type") == "article"}
    assert set(articles) == {"benzinga", "finnhub"}
    ben_seen = articles["benzinga"]["first_received_at"]
    fin_seen = articles["finnhub"]["first_received_at"]
    assert ben_seen != fin_seen
    assert ben_seen < fin_seen  # Benzinga is polled first in the pass


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
    assert report["counts"] == {
        "NVDA": {"benzinga": 0, "benzinga_tickers": 0, "finnhub": 0}
    }


def test_poll_error_writes_status_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    """A total Benzinga outage must not look like a clean collection —
    even when Finnhub still returns rows."""
    _enable(monkeypatch)

    async def _boom(ticker, **kwargs):
        raise bz.BenzingaNewsHTTPError("benzinga news HTTP 401", status_code=401)

    async def _fake_finnhub(symbol, market, limit):
        return {
            "news": [
                {
                    "title": "F story",
                    "url": "https://f.io/1",
                    "source": "Finnhub",
                    "datetime": "2025-11-25T14:00:00",
                    "summary": "s",
                }
            ]
        }

    monkeypatch.setattr(bz, "fetch_benzinga_news", _boom)
    import app.services.finnhub_news as finnhub_news

    monkeypatch.setattr(finnhub_news, "fetch_news_finnhub", _fake_finnhub)
    code = cli.main(
        [
            "poll",
            "--symbols",
            "NVDA",
            "--out-dir",
            str(tmp_path),
            "--poll-id",
            "p-err",
        ]
    )
    assert code == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "p-err.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_source = {}
    for row in rows:
        by_source.setdefault((row.get("record_type"), row.get("source")), []).append(
            row
        )
    assert by_source[("poll_status", "benzinga")][0]["status"] == "error"
    assert by_source[("poll_status", "benzinga")][0]["count"] == 0
    assert by_source[("article", "finnhub")][0]["title"] == "F story"


def test_poll_filters_finnhub_to_requested_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    """--date-from/--date-to narrow the Finnhub side too, not just Benzinga."""
    _enable(monkeypatch)

    async def _empty(ticker, **kwargs):
        return []

    async def _fake_finnhub(symbol, market, limit):
        return {
            "news": [
                {
                    "title": "in window",
                    "url": "https://f.io/in",
                    "source": "Finnhub",
                    "datetime": "2025-11-25T14:00:00",
                    "summary": "s",
                },
                {
                    "title": "too old",
                    "url": "https://f.io/old",
                    "source": "Finnhub",
                    "datetime": "2025-11-20T14:00:00",
                    "summary": "s",
                },
            ]
        }

    monkeypatch.setattr(bz, "fetch_benzinga_news", _empty)
    import app.services.finnhub_news as finnhub_news

    monkeypatch.setattr(finnhub_news, "fetch_news_finnhub", _fake_finnhub)
    code = cli.main(
        [
            "poll",
            "--symbols",
            "NVDA",
            "--out-dir",
            str(tmp_path),
            "--poll-id",
            "p-win",
            "--date-from",
            "2025-11-25",
            "--date-to",
            "2025-11-25",
        ]
    )
    assert code == 0
    rows = [
        json.loads(line)
        for line in (tmp_path / "p-win.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    articles = [r for r in rows if r.get("record_type") == "article"]
    assert [a["title"] for a in articles] == ["in window"]


def _write(path, rows) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_summary_reports_overlap_unique_latency(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    _enable(monkeypatch)
    rows = [
        {
            "record_type": "article",
            "source": "benzinga",
            "provider_id": "1",
            "url": "https://x.com/a?x=1",
            "title": "Shared",
            "query_symbol": "NVDA",
            "poll_id": "p9",
            "first_received_at": "2025-11-25T14:00:20+00:00",
        },
        {
            "record_type": "article",
            "source": "benzinga",
            "provider_id": "2",
            "url": "https://x.com/b-only",
            "title": "B only",
            "query_symbol": "NVDA",
            "poll_id": "p9",
            "first_received_at": "2025-11-25T14:00:20+00:00",
        },
        {
            "record_type": "article",
            "source": "finnhub",
            "provider_id": "https://x.com/a",
            "url": "https://x.com/a",
            "title": "Shared",
            "query_symbol": "NVDA",
            "poll_id": "p9",
            "first_received_at": "2025-11-25T14:00:00+00:00",
        },
    ]
    path = tmp_path / "p9.jsonl"
    _write(path, rows)
    assert cli.main(["summary", "--input", str(path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["inputs"] == [str(path)]
    assert summary["overlap_articles"] == 1
    assert summary["benzinga_unique"] == 1
    assert summary["finnhub_unique"] == 0
    assert summary["latency_seconds"]["min"] == 20.0


def test_summary_reads_several_files_and_reports_unreliable_cells(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    _enable(monkeypatch)
    good = [
        {
            "record_type": "article",
            "source": "benzinga",
            "provider_id": "1",
            "url": "https://x.com/a",
            "title": "Shared",
            "query_symbol": "NVDA",
            "poll_id": "p1",
            "first_received_at": "2025-11-25T14:00:10+00:00",
        },
        {
            "record_type": "article",
            "source": "finnhub",
            "provider_id": "https://x.com/a",
            "url": "https://x.com/a",
            "title": "Shared",
            "query_symbol": "NVDA",
            "poll_id": "p1",
            "first_received_at": "2025-11-25T14:00:00+00:00",
        },
    ]
    bad = [
        {
            "record_type": "article",
            "source": "finnhub",
            "provider_id": "https://y.com/lonely",
            "url": "https://y.com/lonely",
            "title": "Lonely",
            "query_symbol": "AMD",
            "poll_id": "p1",
            "first_received_at": "2025-11-25T14:00:00+00:00",
        },
        {
            "record_type": "poll_status",
            "source": "benzinga",
            "query_symbol": "AMD",
            "poll_id": "p1",
            "status": "error",
            "count": 0,
            "error_class": "BenzingaNewsHTTPError",
            "first_received_at": "2025-11-25T14:00:00+00:00",
        },
    ]
    _write(tmp_path / "a.jsonl", good)
    _write(tmp_path / "b.jsonl", bad)
    assert (
        cli.main(
            ["summary", "--input", str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["overlap_articles"] == 1
    # The failed AMD cell is reported, not counted as Finnhub-only coverage.
    assert summary["unreliable_symbol_polls"] == [{"poll_id": "p1", "symbol": "AMD"}]
    assert summary["finnhub_unique"] == 0
    assert summary["excluded_unreliable_articles"] == {"benzinga": 0, "finnhub": 1}
    # A directory input reads every *.jsonl inside it.
    assert cli.main(["summary", "--input", str(tmp_path)]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["inputs"] == [str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")]
    assert again["overlap_articles"] == 1


def test_poll_queries_both_params_and_records_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    """Acceptance 1: primaryTickers + tickers are queried and recorded separately."""
    _enable(monkeypatch)
    seen_params: list[str] = []

    async def _fake_fetch(ticker, **kwargs):
        param = kwargs["ticker_param"]
        seen_params.append(param)
        return [
            bz.normalize_record(
                _benzinga_item(f"{ticker} {param}", f"https://x.com/{ticker}-{param}"),
                poll_id=kwargs["poll_id"],
                first_received_at=kwargs["now"],
            )
        ]

    monkeypatch.setattr(bz, "fetch_benzinga_news", _fake_fetch)
    code = cli.main(
        [
            "poll",
            "--symbols",
            "NVDA",
            "--out-dir",
            str(tmp_path),
            "--poll-id",
            "p-params",
            "--skip-finnhub",
        ]
    )
    assert code == 0
    assert seen_params == ["primaryTickers", "tickers"]
    report = json.loads(capsys.readouterr().out)
    assert report["counts"] == {
        "NVDA": {"benzinga": 1, "benzinga_tickers": 1, "finnhub": 0}
    }
    rows = [
        json.loads(line)
        for line in (tmp_path / "p-params.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    articles = [r for r in rows if r.get("record_type") == "article"]
    statuses = [r for r in rows if r.get("record_type") == "poll_status"]
    assert sorted({a["query_param"] for a in articles}) == [
        "primaryTickers",
        "tickers",
    ]
    assert {a["title"] for a in articles} == {
        "NVDA primaryTickers",
        "NVDA tickers",
    }
    assert sorted(s["query_param"] for s in statuses) == [
        "primaryTickers",
        "tickers",
    ]


def test_summary_reports_killed_poll_as_unreliable(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    """Acceptance 2: a poll with a header but no completion record is unreliable."""
    _enable(monkeypatch)
    _write(
        tmp_path / "killed.jsonl",
        [
            {
                "record_type": "poll_header",
                "poll_id": "p-killed",
                "symbols": ["NVDA"],
                "started_at": "2025-11-25T14:00:00+00:00",
            },
        ],
    )
    assert cli.main(["summary", "--input", str(tmp_path / "killed.jsonl")]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["incomplete_polls"] == [
        {
            "poll_id": "p-killed",
            "reason": "no_completion_record",
            "symbols": ["NVDA"],
        }
    ]
    assert summary["unreliable_symbol_polls"] == [
        {"poll_id": "p-killed", "symbol": "NVDA"}
    ]
