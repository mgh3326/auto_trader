"""The script refuses an export or price snapshot its manifest does not vouch for."""

from __future__ import annotations

import json

import pytest

from research.trade_retro.export import ExportIntegrityError, load_export
from research.trade_retro.prices import load_snapshot
from research.trade_retro.run import main
from tests.research.trade_retro.retro_fixtures import sample_export, sample_prices

pytestmark = pytest.mark.unit


def _report_args(export, prices, tmp_path):
    return [
        "report",
        "--export", str(export),
        "--prices", str(prices),
        "--public-out", str(tmp_path / "public.md"),
        "--private-dir", str(tmp_path / "private"),
    ]  # fmt: skip


def test_intact_export_loads(tmp_path):
    export = load_export(sample_export(tmp_path / "export"))
    assert len(export.tables["execution_ledger"]) == 5
    assert export.tables["execution_ledger"]["symbol"].tolist()[2] == "005930"
    assert len(export.manifest_sha256) == 64


def test_refuses_a_changed_file_with_the_same_size(tmp_path):
    root = sample_export(tmp_path / "export")
    path = root / "execution_ledger.csv"
    data = path.read_bytes()
    path.write_bytes(data.replace(b"7,100", b"8,100", 1))
    assert path.stat().st_size == len(data)
    with pytest.raises(ExportIntegrityError, match="sha256 mismatch"):
        load_export(root)


def test_refuses_a_size_change(tmp_path):
    root = sample_export(tmp_path / "export")
    with (root / "execution_ledger.csv").open("a", encoding="utf-8") as handle:
        handle.write("extra\n")
    with pytest.raises(ExportIntegrityError, match="size mismatch"):
        load_export(root)


def test_refuses_a_missing_file(tmp_path):
    root = sample_export(tmp_path / "export")
    (root / "order_proposals.csv").unlink()
    with pytest.raises(ExportIntegrityError, match="missing"):
        load_export(root)


def test_refuses_a_file_the_manifest_does_not_list(tmp_path):
    root = sample_export(tmp_path / "export")
    (root / "planted.csv").write_text("a\n", encoding="utf-8")
    with pytest.raises(ExportIntegrityError, match="not in the manifest"):
        load_export(root)


def test_refuses_another_schema_version(tmp_path):
    root = sample_export(tmp_path / "export")
    path = root / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "dots-history-export/v0"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ExportIntegrityError, match="schema_version"):
        load_export(root)


def test_refuses_a_directory_without_a_manifest(tmp_path):
    with pytest.raises(ExportIntegrityError, match="manifest"):
        load_export(tmp_path)


def test_refuses_a_changed_price_file(tmp_path):
    root = sample_prices(tmp_path / "prices")
    path = root / "us" / "AAA.csv"
    path.write_text(
        path.read_text(encoding="utf-8").replace("101.0", "109.0"), encoding="utf-8"
    )
    with pytest.raises(ExportIntegrityError, match="price file"):
        load_snapshot(root)


def test_cli_exits_nonzero_and_writes_nothing_on_a_hash_mismatch(tmp_path, capsys):
    export = sample_export(tmp_path / "export")
    prices = sample_prices(tmp_path / "prices")
    path = export / "execution_ledger.csv"
    path.write_bytes(path.read_bytes().replace(b"7,100", b"8,100", 1))

    code = main(_report_args(export, prices, tmp_path))

    assert code == 3
    assert "refused" in capsys.readouterr().err
    assert not (tmp_path / "public.md").exists()
    assert not (tmp_path / "private").exists()
