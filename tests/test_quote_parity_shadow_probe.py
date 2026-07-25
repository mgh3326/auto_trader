from __future__ import annotations

import json

import pytest

from scripts.quote_parity_shadow_probe import (
    exit_code_for,
    load_symbols_file,
    main,
    parse_args,
)

pytestmark = pytest.mark.unit


class TestArgs:
    def test_defaults_are_dry_run(self):
        ns = parse_args(["--user-id", "1"])
        assert ns.confirm_live is False
        assert ns.us_kis_live_last is False
        assert ns.limit == 200

    def test_exit_code_map(self):
        assert exit_code_for("go") == 0
        assert exit_code_for("no_go") == 2
        assert exit_code_for("blocked") == 2
        assert exit_code_for("???") == 1


class TestSymbolsFile:
    def test_split_kr_us_and_normalize_dotted(self, tmp_path):
        p = tmp_path / "u.json"
        p.write_text(
            json.dumps(
                [
                    {"market": "US", "symbol": "BRK-B"},
                    {"market": "KR", "symbol": "005930"},
                ]
            ),
            encoding="utf-8",
        )
        kr, us = load_symbols_file(p)
        assert kr == ["005930"]
        assert us == ["BRK.B"]  # to_db_symbol normalization

    def test_rejects_secrets(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("symbol,authorization\nAAPL,Bearer abc123\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_symbols_file(p)


@pytest.mark.asyncio
async def test_dry_run_performs_no_network(tmp_path, capsys, monkeypatch):
    # A symbols-file dry-run must not construct any broker client.
    import scripts.quote_parity_shadow_probe as probe

    def _boom(*a, **k):
        raise AssertionError("dry-run must not build a live client")

    monkeypatch.setattr(probe, "_build_live_clients", _boom, raising=False)
    p = tmp_path / "u.json"
    p.write_text(json.dumps([{"market": "US", "symbol": "AAPL"}]), encoding="utf-8")

    rc = await main(["--symbols-file", str(p)])  # no --confirm-live
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["mode"] == "dry_run"
    assert out["universe"]["us_count"] == 1
