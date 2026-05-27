from typing import Any

import pytest

from app.services.action_report.remote_debug_audit.cdp_client import (
    CdpClient,
    FakeCdpSession,
    await_rendered_value,
)
from app.services.action_report.remote_debug_audit.host_allowlist import (
    CdpDebugHostBlocked,
)


def test_client_construction_host_locked() -> None:
    CdpClient(host_port="127.0.0.1:9222")  # ok
    with pytest.raises(CdpDebugHostBlocked):
        CdpClient(host_port="127.0.0.1:9999")


@pytest.mark.asyncio
async def test_fake_session_returns_canned_value_by_url() -> None:
    fake = FakeCdpSession(results={"https://x/?code=005930": '{"code":"005930"}'})
    out = await fake.fetch_rendered("https://x/?code=005930", "js", timeout_s=1.0)
    assert out == '{"code":"005930"}'


@pytest.mark.asyncio
async def test_fake_session_raises_for_unknown_url() -> None:
    fake = FakeCdpSession(results={})
    with pytest.raises(RuntimeError):
        await fake.fetch_rendered("https://x/?code=000660", "js", timeout_s=1.0)


@pytest.mark.asyncio
async def test_fake_session_ignores_ready_js_kwarg() -> None:
    fake = FakeCdpSession(results={"u": "v"})
    out = await fake.fetch_rendered("u", "js", timeout_s=1.0, ready_js="READY")
    assert out == "v"


class _ScriptedCmd:
    """Fake CDP command channel: records calls, scripts ready/extract evals.

    ``READY`` evaluates to False for the first ``ready_after_polls`` calls then
    True; any other expression is the extraction and returns ``extract_value``.
    """

    def __init__(self, *, ready_after_polls: int, extract_value: Any) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self._ready_after = ready_after_polls
        self._ready_polls = 0
        self._extract_value = extract_value

    async def __call__(
        self, method: str, params: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        expr = params.get("expression")
        self.calls.append((method, expr, session_id))
        if method != "Runtime.evaluate":
            return {"result": {}}
        if expr == "READY":
            self._ready_polls += 1
            return {
                "result": {"result": {"value": self._ready_polls > self._ready_after}}
            }
        return {"result": {"result": {"value": self._extract_value}}}


class _SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _FakeClock:
    """Monotonic stand-in returning ``start`` then advancing by ``step`` each call."""

    def __init__(self, *, start: float = 0.0, step: float = 0.0) -> None:
        self._t = start
        self._step = step

    def __call__(self) -> float:
        v = self._t
        self._t += self._step
        return v


@pytest.mark.asyncio
async def test_await_rendered_value_polls_until_ready_then_extracts() -> None:
    cmd = _ScriptedCmd(ready_after_polls=2, extract_value='{"code":"005930"}')
    sleeps = _SleepRecorder()
    out = await await_rendered_value(
        cmd,
        session_id="s1",
        extract_js="EXTRACT",
        ready_js="READY",
        timeout_s=10.0,
        poll_interval_s=0.25,
        sleep=sleeps,
        monotonic=_FakeClock(),  # stuck at 0 → ready gate, not timeout, ends it
    )
    assert out == '{"code":"005930"}'

    methods = [m for (m, _e, _s) in cmd.calls]
    # Page + Runtime enabled before the first evaluate.
    first_eval = methods.index("Runtime.evaluate")
    assert "Page.enable" in methods[:first_eval]
    assert "Runtime.enable" in methods[:first_eval]
    # Three ready polls (False, False, True), two sleeps between them.
    ready_evals = [c for c in cmd.calls if c[1] == "READY"]
    assert len(ready_evals) == 3
    assert sleeps.calls == [0.25, 0.25]
    # Final call is the single extraction.
    assert cmd.calls[-1] == ("Runtime.evaluate", "EXTRACT", "s1")


@pytest.mark.asyncio
async def test_await_rendered_value_is_bounded_when_never_ready() -> None:
    # Clock never advances, so the deadline never fires; the hard max-poll cap
    # must still terminate the loop (no infinite poll).
    cmd = _ScriptedCmd(ready_after_polls=10**9, extract_value=None)
    sleeps = _SleepRecorder()
    out = await await_rendered_value(
        cmd,
        session_id="s1",
        extract_js="EXTRACT",
        ready_js="READY",
        timeout_s=1.0,
        poll_interval_s=0.25,
        sleep=sleeps,
        monotonic=_FakeClock(step=0.0),
    )
    assert out is None
    ready_evals = [c for c in cmd.calls if c[1] == "READY"]
    assert 1 <= len(ready_evals) <= 6  # bounded by timeout/interval cap
    # Still performs the final extraction even though the gate never opened.
    assert cmd.calls[-1] == ("Runtime.evaluate", "EXTRACT", "s1")


@pytest.mark.asyncio
async def test_await_rendered_value_times_out_via_deadline() -> None:
    cmd = _ScriptedCmd(ready_after_polls=10**9, extract_value=None)
    sleeps = _SleepRecorder()
    out = await await_rendered_value(
        cmd,
        session_id="s1",
        extract_js="EXTRACT",
        ready_js="READY",
        timeout_s=1.0,
        poll_interval_s=0.25,
        sleep=sleeps,
        monotonic=_FakeClock(step=0.5),  # 0 (deadline=1.0), 0.5, 1.0 → break
    )
    assert out is None
    ready_evals = [c for c in cmd.calls if c[1] == "READY"]
    assert len(ready_evals) == 2  # poll@0.5 sleeps, poll@1.0 hits deadline


@pytest.mark.asyncio
async def test_await_rendered_value_without_ready_js_extracts_once() -> None:
    cmd = _ScriptedCmd(ready_after_polls=0, extract_value="V")
    sleeps = _SleepRecorder()
    out = await await_rendered_value(
        cmd,
        session_id="s1",
        extract_js="EXTRACT",
        ready_js=None,
        timeout_s=5.0,
        sleep=sleeps,
        monotonic=_FakeClock(),
    )
    assert out == "V"
    assert sleeps.calls == []  # no polling
    assert [c for c in cmd.calls if c[1] == "READY"] == []
    methods = [m for (m, _e, _s) in cmd.calls]
    assert methods.count("Runtime.evaluate") == 1
    assert "Page.enable" in methods and "Runtime.enable" in methods
