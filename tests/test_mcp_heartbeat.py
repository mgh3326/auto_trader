"""ROB-469 PR3: MCP heartbeat writer tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.mcp_server.heartbeat import write_heartbeat


@pytest.mark.unit
def test_write_heartbeat_atomic_payload(tmp_path: Path) -> None:
    hb = tmp_path / "state" / "heartbeat" / "mcp-blue.json"
    write_heartbeat(hb, color="blue", is_running=True)
    assert hb.exists()
    data = json.loads(hb.read_text())
    assert data["color"] == "blue"
    assert data["is_running"] is True
    assert isinstance(data["updated_at_unix"], (int, float))
    # no leftover temp file
    assert not (hb.with_suffix(".tmp")).exists()


@pytest.mark.unit
def test_write_heartbeat_creates_parent_dirs(tmp_path: Path) -> None:
    hb = tmp_path / "a" / "b" / "c" / "mcp-green.json"
    write_heartbeat(hb, color="green", is_running=False)
    assert hb.exists()
    assert json.loads(hb.read_text())["is_running"] is False


@pytest.mark.unit
def test_write_heartbeat_swallows_oserror(tmp_path: Path) -> None:
    # A path whose parent is a FILE (not a dir) makes mkdir/replace fail; the
    # writer must warn and NOT raise (a heartbeat failure must never crash the loop).
    clash = tmp_path / "clash"
    clash.write_text("i am a file")
    hb = clash / "mcp-blue.json"
    write_heartbeat(hb, color="blue", is_running=True)  # must not raise


@pytest.mark.unit
@pytest.mark.asyncio
async def test_heartbeat_loop_writes_then_marks_stopped_on_cancel(tmp_path: Path) -> None:
    from app.mcp_server.heartbeat import heartbeat_loop

    hb = tmp_path / "mcp-blue.json"
    task = asyncio.create_task(heartbeat_loop(hb, interval_s=0.05, color="blue"))
    await asyncio.sleep(0.12)  # at least one write
    assert json.loads(hb.read_text())["is_running"] is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # graceful cancel writes a final is_running=False
    assert json.loads(hb.read_text())["is_running"] is False
