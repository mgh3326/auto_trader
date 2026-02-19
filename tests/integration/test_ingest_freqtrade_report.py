from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


class _SessionContext:
    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        _ = (exc_type, exc, tb)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_file_uses_ingestion_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.ingest_freqtrade_report import ingest_file

    payload_path = tmp_path / "summary.json"
    payload_path.write_text(
        json.dumps(
            {
                "run_id": "run-int-1",
                "strategy_name": "NFI",
                "timeframe": "5m",
                "runner": "pi",
                "total_trades": 42,
                "profit_factor": 1.5,
                "max_drawdown": 0.12,
            }
        ),
        encoding="utf-8",
    )

    fake_session = AsyncMock()
    monkeypatch.setattr(
        "scripts.ingest_freqtrade_report.AsyncSessionLocal",
        lambda: _SessionContext(fake_session),
    )

    ingest_mock = AsyncMock(return_value="run-int-1")
    monkeypatch.setattr(
        "scripts.ingest_freqtrade_report.ingest_summary_payload",
        ingest_mock,
    )

    run_id = await ingest_file(
        payload_path,
        gate_config={"minimum_trade_count": 20},
        runner="pi",
    )

    assert run_id == "run-int-1"
    ingest_mock.assert_awaited_once()
