"""hk 6661: news shadow CLI with the KIS headline source (no network, no DB).

The KIS reader is replaced by a fake through ``cli._open_kis_reader``, so no
test constructs a real ``KISClient``. Benzinga and Finnhub are faked as in
``test_benzinga_news_shadow.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.benzinga_news_shadow as cli
from app.services import benzinga_news as bz
from app.services import kis_news_shadow as kn

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kis_news"


def _fixture(name: str, key: str) -> list[dict]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))[key]


class _FakeKIS:
    def __init__(
        self,
        *,
        brk_error: Exception | None = None,
        overseas_error: Exception | None = None,
    ) -> None:
        self.brk_error = brk_error
        self.overseas_error = overseas_error
        self.closed = False
        self.calls: list[tuple[str, str | None]] = []

    async def news_titles_overseas_brknews(self) -> list[dict]:
        self.calls.append(("brknews", None))
        if self.brk_error is not None:
            raise self.brk_error
        return _fixture("brknews_title", "output")

    async def news_titles_overseas(
        self, symbol: str, *, nation_cd: str = "US"
    ) -> list[dict]:
        self.calls.append(("overseas", symbol))
        if self.overseas_error is not None:
            raise self.overseas_error
        return [
            {**row, "symb": symbol}
            for row in _fixture("overseas_news_title", "outblock1")
        ]

    async def news_titles_domestic(self, symbol: str) -> list[dict]:
        self.calls.append(("domestic", symbol))
        return _fixture("domestic_news_title", "output")

    async def close(self) -> None:
        self.closed = True


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(bz.SHADOW_ENABLED_ENV, "true")
    monkeypatch.setenv(bz.API_KEY_ENV, "TEST-KEY")


def _patch_benzinga_and_finnhub(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ben(ticker, **kwargs):  # noqa: ANN001, ANN202
        return [
            bz.normalize_record(
                {
                    "id": 7,
                    "title": "Benzinga story",
                    "url": "https://b.example/1",
                    "created": "Tue, 25 Nov 2025 09:30:00 -0400",
                    "stocks": ["NVDA"],
                    "channels": [],
                },
                poll_id=kwargs["poll_id"],
                first_received_at=kwargs["now"],
            )
        ]

    async def _fin(symbol, market, limit):  # noqa: ANN001, ANN202
        return {"news": []}

    import app.services.finnhub_news as finnhub_news

    monkeypatch.setattr(bz, "fetch_benzinga_news", _ben)
    monkeypatch.setattr(finnhub_news, "fetch_news_finnhub", _fin)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_kis_rows_carry_symbol_provider_time_and_first_received(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """Acceptance 1: KIS article rows with symbol, UTC time, first_received_at;
    market-wide rows matched by iscd code; unmatched kept with symbol null."""
    _enable(monkeypatch)
    _patch_benzinga_and_finnhub(monkeypatch)
    fake = _FakeKIS()
    monkeypatch.setattr(cli, "_open_kis_reader", lambda: fake)

    code = cli.main(
        [
            "poll",
            "--symbols",
            "NVDA,AMD",
            "--out-dir",
            str(tmp_path),
            "--poll-id",
            "p-kis",
            "--skip-finnhub",
        ]
    )
    assert code == 0
    assert fake.closed is True
    rows = _rows(tmp_path / "p-kis.jsonl")
    kis_articles = [
        r
        for r in rows
        if r.get("source") == "kis" and r.get("record_type") == "article"
    ]

    brk_story = [
        r for r in kis_articles if r["title"] == "NVIDIA unveils new data center chip"
    ]
    assert sorted(r["query_symbol"] for r in brk_story) == ["AMD", "NVDA"]
    assert brk_story[0]["created_utc"] == "2025-11-25T00:30:15+00:00"
    assert brk_story[0]["first_received_at"]
    assert brk_story[0]["created_basis"] == kn.KIS_TIME_BASIS

    unmatched = [r for r in kis_articles if r["title"] == "Oil prices edge higher"]
    assert len(unmatched) == 1 and unmatched[0]["query_symbol"] is None

    per_symbol = [r for r in kis_articles if r["query_param"] == kn.PARAM_OVERSEAS_NEWS]
    assert sorted(r["query_symbol"] for r in per_symbol) == ["AMD", "NVDA"]
    # One brknews call per pass, one per-symbol overseas call per symbol.
    assert fake.calls.count(("brknews", None)) == 1
    assert ("overseas", "NVDA") in fake.calls and ("overseas", "AMD") in fake.calls

    report = json.loads(capsys.readouterr().out)
    assert report["counts"]["NVDA"]["kis"] >= 2  # matched brknews + per-symbol


def test_kis_failure_writes_error_row_and_does_not_stop_other_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """Acceptance 2: a KIS failure is an error row; Benzinga still polls and the
    pass exits 1 as any source outage does."""
    _enable(monkeypatch)
    _patch_benzinga_and_finnhub(monkeypatch)
    fake = _FakeKIS(
        brk_error=RuntimeError("kis down"), overseas_error=RuntimeError("kis down")
    )
    monkeypatch.setattr(cli, "_open_kis_reader", lambda: fake)

    code = cli.main(
        [
            "poll",
            "--symbols",
            "NVDA",
            "--out-dir",
            str(tmp_path),
            "--poll-id",
            "p-kerr",
            "--skip-finnhub",
        ]
    )
    assert code == 1
    rows = _rows(tmp_path / "p-kerr.jsonl")
    kis_status = [
        r
        for r in rows
        if r.get("record_type") == "poll_status" and r.get("source") == "kis"
    ]
    assert {s["query_param"] for s in kis_status} == {
        kn.PARAM_BRKNEWS,
        kn.PARAM_OVERSEAS_NEWS,
    }
    assert all(
        s["status"] == "error" and s["error_class"] == "RuntimeError"
        for s in kis_status
    )
    ben_articles = [
        r
        for r in rows
        if r.get("source") == "benzinga" and r.get("record_type") == "article"
    ]
    assert ben_articles and ben_articles[0]["title"] == "Benzinga story"
    assert any(r.get("record_type") == "poll_completion" for r in rows)


def test_kis_client_construction_failure_is_an_error_row_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    _enable(monkeypatch)
    _patch_benzinga_and_finnhub(monkeypatch)

    def _boom():  # noqa: ANN202
        raise ValueError("no creds")

    monkeypatch.setattr(cli, "_open_kis_reader", _boom)
    code = cli.main(
        [
            "poll",
            "--symbols",
            "NVDA",
            "--out-dir",
            str(tmp_path),
            "--poll-id",
            "p-kopen",
            "--skip-finnhub",
        ]
    )
    assert code == 1
    rows = _rows(tmp_path / "p-kopen.jsonl")
    kis_status = [
        r
        for r in rows
        if r.get("source") == "kis" and r.get("record_type") == "poll_status"
    ]
    assert kis_status and all(s["error_class"] == "ValueError" for s in kis_status)


def test_kr_market_polls_domestic_headlines_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    _enable(monkeypatch)
    fake = _FakeKIS()
    monkeypatch.setattr(cli, "_open_kis_reader", lambda: fake)
    monkeypatch.delenv(bz.API_KEY_ENV, raising=False)  # KR never reads the Benzinga key

    code = cli.main(
        [
            "poll",
            "--market",
            "kr",
            "--symbols",
            "005930",
            "--out-dir",
            str(tmp_path),
            "--poll-id",
            "p-kr",
        ]
    )
    assert code == 0
    assert fake.calls == [("domestic", "005930")]
    rows = _rows(tmp_path / "p-kr.jsonl")
    assert {r.get("source") for r in rows if r.get("record_type") == "article"} == {
        "kis"
    }
    report = json.loads(capsys.readouterr().out)
    assert report["counts"] == {"005930": {"kis": 1}}


def test_skip_kis_means_no_kis_client_and_no_kis_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    _enable(monkeypatch)
    _patch_benzinga_and_finnhub(monkeypatch)

    def _must_not_open():  # noqa: ANN202
        raise AssertionError("KIS client opened with --skip-kis")

    monkeypatch.setattr(cli, "_open_kis_reader", _must_not_open)
    code = cli.main(
        [
            "poll",
            "--symbols",
            "NVDA",
            "--out-dir",
            str(tmp_path),
            "--poll-id",
            "p-skip",
            "--skip-finnhub",
            "--skip-kis",
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["counts"] == {
        "NVDA": {"benzinga": 1, "benzinga_tickers": 1, "finnhub": 0}
    }


def test_kr_with_skip_kis_has_no_source_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    code = cli.main(
        [
            "poll",
            "--market",
            "kr",
            "--skip-kis",
            "--symbols",
            "005930",
            "--out-dir",
            str(tmp_path),
            "--poll-id",
            "p-krskip",
        ]
    )
    assert code == 2
    assert list(tmp_path.iterdir()) == []


def test_summary_reports_kis_alongside_benzinga_and_finnhub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    _enable(monkeypatch)
    """Acceptance 3: the summary's kis block has coverage and ordering."""
    ts = "2025-11-25T14:00:0{}+00:00"
    lines = [
        {"record_type": "poll_header", "poll_id": "p1", "symbols": ["NVDA"]},
        {
            "record_type": "poll_completion",
            "poll_id": "p1",
            "symbols_completed": ["NVDA"],
        },
        {
            "record_type": "article",
            "source": "kis",
            "title": "Shared",
            "poll_id": "p1",
            "query_symbol": "NVDA",
            "query_param": kn.PARAM_BRKNEWS,
            "provider_id": "kis:1",
            "first_received_at": ts.format(1),
            "symbols": ["NVDA"],
        },
        {
            "record_type": "article",
            "source": "benzinga",
            "title": "Shared",
            "poll_id": "p1",
            "query_symbol": "NVDA",
            "query_param": "primaryTickers",
            "provider_id": "b:1",
            "first_received_at": ts.format(3),
            "symbols": ["NVDA"],
        },
        {
            "record_type": "article",
            "source": "finnhub",
            "title": "Shared",
            "poll_id": "p1",
            "query_symbol": "NVDA",
            "provider_id": "f:1",
            "first_received_at": ts.format(2),
            "symbols": [],
        },
    ]
    path = tmp_path / "p1.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    assert cli.main(["summary", "--input", str(path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    kis = summary["kis"]
    assert kis["coverage_by_symbol"]["NVDA"] == {"kis": 1, "benzinga": 1, "finnhub": 1}
    assert kis["headline_overlap"] == {"kis_with_benzinga": 1, "kis_with_finnhub": 1}
    assert kis["first_seen_wins"] == {"kis": 1, "benzinga": 0, "finnhub": 0}
    # Existing Benzinga/Finnhub keys are unchanged.
    assert summary["benzinga_count"] == 1 and summary["finnhub_count"] == 1


def test_summary_unaffected_by_kis_status_rows_for_benzinga_cells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    _enable(monkeypatch)
    """A KIS error must not add Benzinga/Finnhub cells to unreliable_symbol_polls."""
    lines = [
        {"record_type": "poll_header", "poll_id": "p2", "symbols": ["AMD"]},
        {
            "record_type": "poll_completion",
            "poll_id": "p2",
            "symbols_completed": ["AMD"],
        },
        {
            "record_type": "poll_status",
            "source": "kis",
            "poll_id": "p2",
            "query_symbol": None,
            "query_param": kn.PARAM_BRKNEWS,
            "status": "error",
            "error_class": "RuntimeError",
            "first_received_at": "2025-11-25T14:00:00+00:00",
        },
    ]
    path = tmp_path / "p2.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    assert cli.main(["summary", "--input", str(path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["unreliable_symbol_polls"] == []
    assert summary["kis"]["unreliable_polls"] == [
        {"poll_id": "p2", "symbol": "AMD", "reason": "market_wide_error"}
    ]
