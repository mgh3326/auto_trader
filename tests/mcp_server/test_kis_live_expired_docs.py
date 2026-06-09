# tests/mcp_server/test_kis_live_expired_docs.py
import inspect

from app.mcp_server.tooling import orders_kis_variants as mod


def test_reconcile_desc_mentions_expired():
    src = inspect.getsource(mod)
    assert "expired" in src
