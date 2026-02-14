from types import SimpleNamespace

import pytest

import data.disclosures.dart_corp_index as corp_index


@pytest.fixture
def restore_indices():
    prev_name = dict(corp_index.NAME_TO_CORP)
    prev_stock = dict(corp_index.STOCK_TO_CORP)
    prev_corp = dict(corp_index.CORP_TO_NAME)
    try:
        yield
    finally:
        corp_index.NAME_TO_CORP.clear()
        corp_index.NAME_TO_CORP.update(prev_name)
        corp_index.STOCK_TO_CORP.clear()
        corp_index.STOCK_TO_CORP.update(prev_stock)
        corp_index.CORP_TO_NAME.clear()
        corp_index.CORP_TO_NAME.update(prev_corp)


def test_fetch_corp_index_uses_stock_code_and_stock_id(monkeypatch):
    monkeypatch.setattr(corp_index.settings, "opendart_api_key", "dummy-key")
    monkeypatch.setattr(corp_index.dart_fss, "set_api_key", lambda api_key=None: None)

    corps = [
        SimpleNamespace(
            corp_name="삼성전자",
            corp_code="00126380",
            stock_code="005930",
        ),
        SimpleNamespace(
            corp_name="NAVER",
            corp_code="00266961",
            stock_id="035420",
        ),
    ]
    monkeypatch.setattr(
        corp_index.dart_fss,
        "get_corp_list",
        lambda: SimpleNamespace(corps=corps),
    )

    result = corp_index._fetch_corp_index_sync()

    assert result["name_to_corp"]["삼성전자"] == "00126380"
    assert result["name_to_corp"]["NAVER"] == "00266961"
    assert result["stock_to_corp"]["005930"] == "00126380"
    assert result["stock_to_corp"]["035420"] == "00266961"
    assert result["corp_to_name"]["00126380"] == "삼성전자"
    assert result["corp_to_name"]["00266961"] == "NAVER"


def test_resolve_symbol_supports_stock_code(restore_indices):
    corp_index.NAME_TO_CORP.clear()
    corp_index.STOCK_TO_CORP.clear()
    corp_index.CORP_TO_NAME.clear()

    corp_index.NAME_TO_CORP["삼성전자"] = "00126380"
    corp_index.STOCK_TO_CORP["005930"] = "00126380"
    corp_index.CORP_TO_NAME["00126380"] = "삼성전자"

    corp_code, corp_name = corp_index.resolve_symbol("005930")

    assert corp_code == "00126380"
    assert corp_name == "삼성전자"
