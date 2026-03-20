import datetime as dt
import os
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from app.services.disclosures import dart as dart_service


def _filing_dataframe(*rows: dict[str, str]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class FakeCorpCodesFetcherResult:
    def __init__(
        self,
        frame: pd.DataFrame,
        *,
        error: Exception | None = None,
    ) -> None:
        self._frame = frame
        self._error = error
        self.to_pickle_calls: list[Path] = []

    def to_pickle(self, path: str | Path) -> None:
        target = Path(path)
        self.to_pickle_calls.append(target)
        if self._error is not None:
            raise self._error
        self._frame.to_pickle(target)


class FakeOpenDartClient:
    def __init__(
        self,
        *,
        frame: pd.DataFrame | None = None,
        error: Exception | None = None,
        capture: list[dict[str, object]] | None = None,
    ) -> None:
        self._frame = frame if frame is not None else pd.DataFrame()
        self._error = error
        self._capture = capture if capture is not None else []

    def list(
        self,
        corp: str | None = None,
        start: str | None = None,
        end: str | None = None,
        kind: str = "",
        final: bool = True,
    ) -> pd.DataFrame:
        self._capture.append(
            {
                "corp": corp,
                "start": start,
                "end": end,
                "kind": kind,
                "final": final,
            }
        )
        if self._error is not None:
            raise self._error
        return self._frame.copy()


def _install_future_service_double(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: FakeOpenDartClient,
) -> list[dict[str, object]]:
    init_calls: list[dict[str, object]] = []

    def factory(api_key: str) -> FakeOpenDartClient:
        init_calls.append({"api_key": api_key, "cwd": Path.cwd()})
        return client

    monkeypatch.setattr(
        dart_service,
        "OpenDartReaderFactory",
        factory,
        raising=False,
    )
    monkeypatch.setattr(dart_service, "_dart_client", None, raising=False)
    return init_calls


@pytest.fixture(autouse=True)
def reset_dart_service_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(dart_service, "_dart_client", None, raising=False)
    monkeypatch.setattr(
        dart_service,
        "_RUNTIME_CACHE_DIR",
        tmp_path / "opendartreader",
        raising=False,
    )
    monkeypatch.setattr(
        dart_service.settings,
        "opendart_api_key",
        "test-opendart-key",
        raising=False,
    )


class TestDARTService:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("symbol", ["", "   "])
    async def test_list_filings_rejects_blank_symbols_before_client_init(
        self,
        monkeypatch: pytest.MonkeyPatch,
        symbol: str,
    ) -> None:
        client = FakeOpenDartClient(frame=pd.DataFrame())
        init_calls = _install_future_service_double(monkeypatch, client=client)

        result = await dart_service.list_filings(symbol, days=3, limit=1)

        assert result == {
            "success": False,
            "error": "symbol is required",
            "filings": [],
            "symbol": "",
        }
        assert init_calls == []

    @pytest.mark.asyncio
    async def test_list_filings_supports_six_digit_stock_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rows = _filing_dataframe(
            {
                "rcept_dt": "20260310",
                "report_nm": "사업보고서",
                "rcept_no": "20260310000001",
                "corp_name": "삼성전자",
            }
        )
        capture: list[dict[str, object]] = []
        client = FakeOpenDartClient(frame=rows, capture=capture)
        _install_future_service_double(monkeypatch, client=client)

        result = await dart_service.list_filings("005930", days=30, limit=5)

        assert result == {
            "success": True,
            "filings": [
                {
                    "date": "2026-03-10",
                    "report_nm": "사업보고서",
                    "rcp_no": "20260310000001",
                    "corp_name": "삼성전자",
                }
            ],
        }
        assert capture == [
            {
                "corp": "005930",
                "start": (dt.date.today() - dt.timedelta(days=30)).isoformat(),
                "end": dt.date.today().isoformat(),
                "kind": "",
                "final": True,
            }
        ]

    @pytest.mark.asyncio
    async def test_list_filings_supports_company_name_best_effort(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rows = _filing_dataframe(
            {
                "rcept_dt": "20260309",
                "report_nm": "분기보고서",
                "rcept_no": "20260309000002",
                "corp_name": "삼성전자",
            }
        )
        capture: list[dict[str, object]] = []
        client = FakeOpenDartClient(frame=rows, capture=capture)
        _install_future_service_double(monkeypatch, client=client)

        result = await dart_service.list_filings("삼성전자", days=14, limit=3)

        assert result == {
            "success": True,
            "filings": [
                {
                    "date": "2026-03-09",
                    "report_nm": "분기보고서",
                    "rcp_no": "20260309000002",
                    "corp_name": "삼성전자",
                }
            ],
        }
        assert capture[0]["corp"] == "삼성전자"

    @pytest.mark.asyncio
    async def test_list_filings_returns_explicit_error_for_unknown_company_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = FakeOpenDartClient(error=ValueError('could not find "없는회사"'))
        _install_future_service_double(monkeypatch, client=client)

        result = await dart_service.list_filings("없는회사", days=7, limit=2)

        assert isinstance(result, dict)
        assert result["success"] is False
        assert result["filings"] == []
        assert result["symbol"] == "없는회사"
        assert 'could not find "없는회사"' in result["error"]

    @pytest.mark.asyncio
    async def test_list_filings_maps_korean_report_type_to_kind(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture: list[dict[str, object]] = []
        client = FakeOpenDartClient(frame=pd.DataFrame(), capture=capture)
        _install_future_service_double(monkeypatch, client=client)

        result = await dart_service.list_filings(
            "005930",
            days=5,
            limit=1,
            report_type="지분",
        )

        assert result == {"success": True, "filings": []}
        assert capture[0]["kind"] == "D"

    @pytest.mark.asyncio
    async def test_list_filings_rejects_invalid_report_type(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = FakeOpenDartClient(frame=pd.DataFrame())
        capture: list[dict[str, object]] = []
        client._capture = capture
        _install_future_service_double(monkeypatch, client=client)

        result = await dart_service.list_filings(
            "005930",
            days=5,
            limit=1,
            report_type="알수없음",
        )

        assert result == {
            "success": False,
            "error": "Invalid report_type: 알수없음. Use one of: 정기, 주요사항, 발행, 지분, 기타",
            "filings": [],
            "symbol": "005930",
        }
        assert capture == []

    @pytest.mark.asyncio
    async def test_list_filings_applies_date_range_and_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rows = _filing_dataframe(
            {
                "rcept_dt": "20260310",
                "report_nm": "사업보고서",
                "rcept_no": "20260310000001",
                "corp_name": "삼성전자",
            },
            {
                "rcept_dt": "20260309",
                "report_nm": "반기보고서",
                "rcept_no": "20260309000002",
                "corp_name": "삼성전자",
            },
            {
                "rcept_dt": "20260308",
                "report_nm": "분기보고서",
                "rcept_no": "20260308000003",
                "corp_name": "삼성전자",
            },
        )
        capture: list[dict[str, object]] = []
        client = FakeOpenDartClient(frame=rows, capture=capture)
        _install_future_service_double(monkeypatch, client=client)

        result = await dart_service.list_filings("005930", days=2, limit=2)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert [filing["rcp_no"] for filing in result["filings"]] == [
            "20260310000001",
            "20260309000002",
        ]
        assert (
            capture[0]["start"] == (dt.date.today() - dt.timedelta(days=2)).isoformat()
        )
        assert capture[0]["end"] == dt.date.today().isoformat()

    @pytest.mark.asyncio
    async def test_list_filings_returns_missing_api_key_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            dart_service.settings,
            "opendart_api_key",
            "",
            raising=False,
        )

        result = await dart_service.list_filings("005930", days=3, limit=1)

        assert result == {
            "success": False,
            "error": "OPENDART_API_KEY not set. Please set environment variable.",
            "filings": [],
            "symbol": "005930",
        }

    @pytest.mark.asyncio
    async def test_list_filings_returns_empty_success_for_empty_dataframe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = FakeOpenDartClient(frame=pd.DataFrame())
        _install_future_service_double(monkeypatch, client=client)

        result = await dart_service.list_filings("005930", days=1, limit=1)

        assert result == {"success": True, "filings": []}

    @pytest.mark.asyncio
    async def test_list_filings_wraps_unexpected_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = FakeOpenDartClient(error=RuntimeError("boom"))
        _install_future_service_double(monkeypatch, client=client)

        result = await dart_service.list_filings("삼성전자", days=30, limit=5)

        assert isinstance(result, dict)
        assert result["success"] is False
        assert result["filings"] == []
        assert result["symbol"] == "삼성전자"
        assert result["error"] == "boom"

    @pytest.mark.asyncio
    async def test_list_filings_reuses_singleton_client_without_mutating_cwd(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        rows = _filing_dataframe()
        capture: list[dict[str, object]] = []
        client = FakeOpenDartClient(frame=rows, capture=capture)
        original_cwd = Path.cwd()
        runtime_cache_dir = tmp_path / "dart-runtime"

        monkeypatch.setattr(
            dart_service,
            "_RUNTIME_CACHE_DIR",
            runtime_cache_dir,
            raising=False,
        )
        init_calls = _install_future_service_double(monkeypatch, client=client)

        first = await dart_service.list_filings("005930", days=1, limit=1)
        second = await dart_service.list_filings("005930", days=1, limit=1)

        assert first == {"success": True, "filings": []}
        assert second == {"success": True, "filings": []}
        assert len(init_calls) == 1
        assert init_calls[0]["cwd"] == original_cwd
        assert Path.cwd() == original_cwd


def test_corp_codes_cache_path_accepts_injected_today(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        dart_service,
        "_RUNTIME_CACHE_DIR",
        tmp_path / "opendartreader",
        raising=False,
    )

    cache_path = dart_service._corp_codes_cache_path(today=dt.date(2026, 3, 10))

    assert cache_path.name == "opendartreader_corp_codes_20260310.pkl"
    assert cache_path.parent == tmp_path / "opendartreader" / "docs_cache"


def test_seed_corp_codes_cache_writes_via_temp_file_and_replace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docs_cache_dir = tmp_path / "opendartreader" / "docs_cache"
    monkeypatch.setattr(
        dart_service,
        "_RUNTIME_CACHE_DIR",
        tmp_path / "opendartreader",
        raising=False,
    )
    monkeypatch.setattr(
        dart_service,
        "OpenDartReaderFactory",
        dart_service.OpenDartReaderClass,
        raising=False,
    )

    frame = _filing_dataframe(
        {"corp_code": "00126380", "corp_name": "삼성전자"},
    )
    fetcher_result = FakeCorpCodesFetcherResult(frame)
    monkeypatch.setattr(
        dart_service,
        "OpenDartReaderCorpCodesFetcher",
        lambda api_key: fetcher_result,
        raising=False,
    )

    original_mkstemp = tempfile.mkstemp
    original_replace = os.replace
    mkstemp_calls: list[dict[str, object]] = []
    replace_calls: list[tuple[Path, Path]] = []

    def capture_mkstemp(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
        text: bool = False,
    ) -> tuple[int, str]:
        fd, temp_path = original_mkstemp(
            suffix=suffix,
            prefix=prefix,
            dir=dir,
            text=text,
        )
        mkstemp_calls.append(
            {
                "dir": Path(dir) if dir is not None else None,
                "prefix": prefix,
                "path": Path(temp_path),
            }
        )
        return fd, temp_path

    def capture_replace(src: str | Path, dst: str | Path) -> None:
        replace_calls.append((Path(src), Path(dst)))
        original_replace(src, dst)

    monkeypatch.setattr(dart_service.tempfile, "mkstemp", capture_mkstemp)
    monkeypatch.setattr(dart_service.os, "replace", capture_replace)

    result = dart_service._seed_corp_codes_cache(
        "test-opendart-key",
        today=dt.date(2026, 3, 10),
    )

    pd.testing.assert_frame_equal(result, frame)
    assert mkstemp_calls
    assert mkstemp_calls[0]["dir"] == docs_cache_dir
    assert str(mkstemp_calls[0]["prefix"]).startswith(".")
    assert replace_calls == [
        (
            mkstemp_calls[0]["path"],
            docs_cache_dir / "opendartreader_corp_codes_20260310.pkl",
        )
    ]
    assert fetcher_result.to_pickle_calls == [mkstemp_calls[0]["path"]]


def test_seed_corp_codes_cache_does_not_delete_stale_cache_before_new_cache_is_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docs_cache_dir = tmp_path / "opendartreader" / "docs_cache"
    docs_cache_dir.mkdir(parents=True, exist_ok=True)
    stale_path = docs_cache_dir / "opendartreader_corp_codes_20260309.pkl"
    _filing_dataframe({"corp_code": "OLD"}).to_pickle(stale_path)

    monkeypatch.setattr(
        dart_service,
        "_RUNTIME_CACHE_DIR",
        tmp_path / "opendartreader",
        raising=False,
    )
    monkeypatch.setattr(
        dart_service,
        "OpenDartReaderFactory",
        dart_service.OpenDartReaderClass,
        raising=False,
    )
    monkeypatch.setattr(
        dart_service,
        "OpenDartReaderCorpCodesFetcher",
        lambda api_key: FakeCorpCodesFetcherResult(
            _filing_dataframe({"corp_code": "NEW"}),
            error=RuntimeError("seed failed"),
        ),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="seed failed"):
        dart_service._seed_corp_codes_cache(
            "test-opendart-key",
            today=dt.date(2026, 3, 10),
        )

    assert stale_path.exists()


def test_build_client_uses_loaded_corp_codes_without_later_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeReader:
        corp_codes: pd.DataFrame | None = None
        api_key = ""

    frame = _filing_dataframe({"corp_code": "00126380", "corp_name": "삼성전자"})
    monkeypatch.setattr(
        dart_service, "OpenDartReaderFactory", FakeReader, raising=False
    )
    monkeypatch.setattr(dart_service, "OpenDartReaderClass", FakeReader, raising=False)
    monkeypatch.setattr(
        dart_service,
        "OpenDartReaderCorpCodesFetcher",
        object(),
        raising=False,
    )
    monkeypatch.setattr(
        dart_service,
        "_seed_corp_codes_cache",
        lambda api_key, today=None: frame.copy(),
    )

    def fail_read_pickle(*args: object, **kwargs: object) -> None:
        raise AssertionError("pd.read_pickle should not be called outside cache helper")

    monkeypatch.setattr(dart_service.pd, "read_pickle", fail_read_pickle)

    client = dart_service._build_client("test-opendart-key")

    assert isinstance(client, FakeReader)
    assert client.corp_codes is not None
    pd.testing.assert_frame_equal(client.corp_codes, frame)
    assert client.api_key == "test-opendart-key"
