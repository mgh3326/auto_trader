from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.brokers.kis.overseas_orders import OverseasOrderClient


@pytest.fixture
def overseas():
    parent = MagicMock()
    parent._hdr_base = {"content-type": "application/json"}
    parent._ensure_token = AsyncMock()
    parent._kis_url = lambda path: f"https://host{path}"
    tm = MagicMock()
    tm.clear_token = AsyncMock()
    parent._token_manager = tm
    settings = MagicMock()
    settings.kis_account_no = "1234567890"  # exactly 10 -> passes len guard
    settings.kis_access_token = "test-token"
    parent._settings = settings
    parent._request_with_rate_limit = AsyncMock()
    instance = OverseasOrderClient(parent)
    return instance, parent


_EGW = {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "token expired"}
_EGW121 = {"rt_cd": "1", "msg_cd": "EGW00121", "msg1": "token expired"}
_OK = {"rt_cd": "0", "output": {"ODNO": "0001", "ORD_TMD": "090000"}, "msg1": "정상"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_resubmits_exactly_once_on_egw00123(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(side_effect=[_EGW, _OK])
    result = await instance.order_overseas_stock("AAPL", "NASD", "buy", 1, 100.0)
    assert parent._request_with_rate_limit.call_count == 2  # exactly one resubmit
    assert parent._token_manager.clear_token.await_count == 1
    assert result["odno"] == "0001"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_resubmits_exactly_once_on_egw00121(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(side_effect=[_EGW121, _OK])
    await instance.order_overseas_stock("AAPL", "NASD", "buy", 1, 100.0)
    assert parent._request_with_rate_limit.call_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_non_token_error_raises_no_resubmit(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(
        return_value={"rt_cd": "1", "msg_cd": "APBK1234", "msg1": "거부"}
    )
    with pytest.raises(RuntimeError, match="APBK1234"):
        await instance.order_overseas_stock("AAPL", "NASD", "buy", 1, 100.0)
    assert parent._request_with_rate_limit.call_count == 1
    assert parent._token_manager.clear_token.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_egw00123_unbounded_recursion_is_a_known_gap(overseas):
    """가드 부재 문서화: EGW00123가 계속 반환되면 재귀가 무한(RecursionError).

    소스에 재귀 깊이 캡이 없어 '정확히 1회'는 코드가 아니라 응답에 의존한다.
    이 테스트는 이중전송/스택오버플로 리스크를 표면화하며, 실제 가드 추가는
    별도 (behavior-change) 이슈로 분리한다.
    """
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(return_value=_EGW)
    with pytest.raises(RecursionError):
        await instance.order_overseas_stock("AAPL", "NASD", "buy", 1, 100.0)


def _sent(parent):
    """마지막 _request_with_rate_limit 호출의 kwargs."""
    _, kwargs = parent._request_with_rate_limit.call_args
    return kwargs


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_uses_rvse_02_real_tr_and_zero_price(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(return_value=_OK)
    await instance.cancel_overseas_order("0001", "AAPL", "NASD", 1)
    kw = _sent(parent)
    assert kw["json_body"]["RVSE_CNCL_DVSN_CD"] == "02"
    assert kw["json_body"]["OVRS_ORD_UNPR"] == "0"
    assert kw["tr_id"] == "TTTT1004U"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_mock_tr_id(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(return_value=_OK)
    await instance.cancel_overseas_order("0001", "AAPL", "NASD", 1, is_mock=True)
    assert _sent(parent)["tr_id"] == "VTTT1004U"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_normalizes_exchange_alias(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(return_value=_OK)
    await instance.cancel_overseas_order("0001", "AAPL", "NASDAQ", 1)
    assert _sent(parent)["json_body"]["OVRS_EXCG_CD"] == "NASD"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_rejects_unsupported_exchange(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(return_value=_OK)
    with pytest.raises(ValueError, match="Unsupported KIS exchange_code"):
        await instance.cancel_overseas_order("0001", "AAPL", "XXXX", 1)
    assert parent._request_with_rate_limit.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_short_account_raises(overseas):
    instance, parent = overseas
    parent._settings.kis_account_no = "123"
    parent._request_with_rate_limit = AsyncMock(return_value=_OK)
    with pytest.raises(ValueError, match="계좌번호 형식"):
        await instance.cancel_overseas_order("0001", "AAPL", "NASD", 1)
    assert parent._request_with_rate_limit.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modify_uses_rvse_01_and_new_price(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(return_value=_OK)
    await instance.modify_overseas_order("0001", "AAPL", "NASD", 1, 123.45)
    kw = _sent(parent)
    assert kw["json_body"]["RVSE_CNCL_DVSN_CD"] == "01"
    assert kw["json_body"]["OVRS_ORD_UNPR"] == str(123.45)
    assert kw["tr_id"] == "TTTT1004U"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modify_short_account_raises(overseas):
    instance, parent = overseas
    parent._settings.kis_account_no = "123"
    parent._request_with_rate_limit = AsyncMock(return_value=_OK)
    with pytest.raises(ValueError, match="계좌번호 형식"):
        await instance.modify_overseas_order("0001", "AAPL", "NASD", 1, 123.45)
    assert parent._request_with_rate_limit.await_count == 0


# ---- ROB-645 no-re-POST guard on cancel/modify mutations ----
# The order path is covered by test_kis_order_no_double_submit.py; cancel and
# modify carry the SAME retry_request_errors=False / max_retries_override=0
# transport guard (source L496-497 / L777-778). Without these, a POST timeout
# could re-submit a cancel/modify mutation. Assert they are actually sent.


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_disables_transport_repost(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(return_value=_OK)
    await instance.cancel_overseas_order("0001", "AAPL", "NASD", 1)
    kw = _sent(parent)
    assert kw["retry_request_errors"] is False
    assert kw["max_retries_override"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modify_disables_transport_repost(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(return_value=_OK)
    await instance.modify_overseas_order("0001", "AAPL", "NASD", 1, 123.45)
    kw = _sent(parent)
    assert kw["retry_request_errors"] is False
    assert kw["max_retries_override"] == 0


# ---- token-refresh resubmit-once on cancel/modify (mirrors order path) ----
# EGW00123/EGW00121 trigger clear_token + resubmit for cancel (source L501-504)
# and modify (L782-785) too. A regression that double-resubmits would double a
# live cancel/modify mutation. Pin exactly-one resubmit.


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_resubmits_exactly_once_on_egw00123(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(side_effect=[_EGW, _OK])
    await instance.cancel_overseas_order("0001", "AAPL", "NASD", 1)
    assert parent._request_with_rate_limit.call_count == 2  # exactly one resubmit
    assert parent._token_manager.clear_token.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modify_resubmits_exactly_once_on_egw00123(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(side_effect=[_EGW, _OK])
    await instance.modify_overseas_order("0001", "AAPL", "NASD", 1, 123.45)
    assert parent._request_with_rate_limit.call_count == 2  # exactly one resubmit
    assert parent._token_manager.clear_token.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_symbol_dot_to_slash(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(return_value=_OK)
    await instance.order_overseas_stock("BRK.B", "NYSE", "buy", 1, 100.0)
    assert _sent(parent)["json_body"]["PDNO"] == "BRK/B"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_symbol_dot_to_slash(overseas):
    instance, parent = overseas
    parent._request_with_rate_limit = AsyncMock(return_value=_OK)
    await instance.cancel_overseas_order("0001", "BRK.B", "NYSE", 1)
    assert _sent(parent)["json_body"]["PDNO"] == "BRK/B"
