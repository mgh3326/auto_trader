# tests/mcp_server/test_kis_live_order_history_expired.py
# expired status가 화이트리스트 필터에 막히지 않는지 확인하는 회귀 가드.
from app.mcp_server.tooling import kis_live_ledger as mod


def test_expired_is_a_terminal_status_value():
    # expired는 lifecycle 매핑에 존재(터미널). 조회에서 누락 필터가 없는지의 회귀 가드.
    assert mod._status_to_lifecycle("expired") == "cancelled"
