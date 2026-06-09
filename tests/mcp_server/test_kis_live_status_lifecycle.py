# tests/mcp_server/test_kis_live_status_lifecycle.py
from app.mcp_server.tooling.kis_live_ledger import _status_to_lifecycle


def test_expired_maps_to_terminal_cancelled_lifecycle():
    assert _status_to_lifecycle("expired") == "cancelled"
