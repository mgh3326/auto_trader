# ROB-270: KIS mock VTS balance 10초 단일 timeout 정책 적용

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/invest/api/account-panel` KIS mock domestic balance snapshot 경로의 `5s × 3회 ReadTimeout` 증폭을 제거하고, `timeout=10s × 1 attempt` 정책 + reader 레벨 total wall-time bound 로 교체한다. 라이브 KIS 경로는 변경하지 않는다.

**Architecture:**
- Request helper (`_request_with_rate_limit_with_headers`)에 `retry_request_errors: bool = True` 옵션을 추가해 `httpx.RequestError`(ReadTimeout 포함) 분기만 선택적으로 끈다. 429 분기는 기존 그대로.
- `fetch_domestic_balance_snapshot` 에 `timeout`, `retry_request_errors`, `max_pages` 파라미터를 외부 노출하되, 디폴트는 기존 라이브 동작과 동일 (`timeout=5.0`, `retry_request_errors=True`, `max_pages=10`).
- `KISMockHomeReader.fetch` 에서만 `timeout=10.0`, `retry_request_errors=False`, `max_pages=3` 와 `asyncio.wait_for(..., total_timeout_sec=12.0)` 으로 감싸 mock 전용 정책 적용. Timeout/실패 시 기존 `_SourceFetchResult` warning fallback 으로 degrade.
- Sentry 관측성: `kis_mock.*` prefix(reader 레벨) + `kis.request.*` prefix(helper 레벨) 로 분리.

**Tech Stack:** Python 3.13, asyncio, httpx, pytest (asyncio + unit/integration markers), sentry-sdk.

**Safety boundaries:**
- broker/order/watch/order-intent mutation 없음 — read-only path만 손댐.
- live KIS request behavior 변경 없음 (디폴트 인자 보존).
- frontend lazy loading / `includePaper`/`paperSources` semantics 변경 없음.
- 계좌번호/토큰/현금 값 로그 출력 없음.
- ROB-268 snapshot coalescing 방향 유지.
- 해외 VTS 잔고 등 다른 mock endpoint 는 이번 PR 범위 밖.

---

## File Structure

**Modify:**
- `app/services/brokers/kis/base.py` — `_request_with_rate_limit_with_headers` / `_request_with_rate_limit` 시그니처에 `retry_request_errors: bool = True` 추가, `httpx.RequestError` 분기 분기 처리, `kis.request.*` span tag 부여.
- `app/services/brokers/kis/account.py` — `fetch_domestic_balance_snapshot` 시그니처에 `timeout: float = 5.0`, `retry_request_errors: bool = True`, `max_pages: int = 10` 추가, helper 호출 시 propagate.
- `app/services/invest_home_readers.py` — `KISMockHomeReader.fetch` 안에서 `timeout=10.0`, `retry_request_errors=False`, `max_pages=3` 명시 전달; `asyncio.wait_for(..., timeout=12.0)` 으로 전체 bound; timeout/실패 시 warning fallback; `kis_mock.*` span tag 부여.

**Modify (tests):**
- `tests/test_kis_domestic_balance_snapshot.py` — 새 파라미터 propagation 회귀 테스트 추가 (`timeout`, `retry_request_errors`, `max_pages`).
- `tests/test_invest_home_readers.py` — KISMockHomeReader 가 (`timeout=10, retry_request_errors=False, max_pages=3`) 를 전달하는지, total wall-time bound 초과 시 warning 으로 degrade 하는지, 새 span tag 가 set 되는지 확인.

**Create (tests):**
- `tests/test_kis_request_error_retry_policy.py` — `_request_with_rate_limit_with_headers` 단위 테스트: (a) 디폴트 `retry_request_errors=True` 에서 `httpx.ReadTimeout` 이 `max_retries+1` 회 시도되는지, (b) `retry_request_errors=False` 에서 `httpx.ReadTimeout` 이 즉시 raise 되는지, (c) `retry_request_errors=False` 라도 `HTTPStatusError(429)` 는 여전히 retry 되는지.

**Docs:**
- 해당 없음. CLAUDE.md 의 "KIS 분봉 API 문제" 섹션과 무관. 본 변경은 코드 + 테스트만.

---

## Pre-flight verification

브랜치/worktree 는 이미 ROB-270 용 (`/Users/mgh3326/work/auto_trader.rob-270`, branch `rob-270`). 추가 worktree 생성 불필요.

- [ ] **Step 0: 작업 디렉터리/브랜치 확인**

Run: `git rev-parse --abbrev-ref HEAD && pwd`
Expected: `rob-270` / `/Users/mgh3326/work/auto_trader.rob-270`

---

## Task 1: Request helper에 `retry_request_errors` 옵션 추가

**Files:**
- Modify: `app/services/brokers/kis/base.py` (lines ~374-554)
- Create: `tests/test_kis_request_error_retry_policy.py`

**Intent:** `_request_with_rate_limit_with_headers` 와 `_request_with_rate_limit` 에 `retry_request_errors: bool = True` 파라미터를 추가. `httpx.RequestError` 분기에서 `retry_request_errors=False` 일 때만 첫 시도 후 즉시 raise. 디폴트 동작은 변경 없음. 429 (`HTTPStatusError` + `is_rate_limited` heuristic) 는 분리 유지.

- [ ] **Step 1: Write the failing test — default behavior unchanged on ReadTimeout (retries)**

Create `tests/test_kis_request_error_retry_policy.py`:

```python
"""ROB-270: ReadTimeout/RequestError retry vs 429 retry separation tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.brokers.kis.base import BaseKISClient


class _FakeSettings:
    kis_app_key = "key"
    kis_app_secret = "secret"
    kis_access_token = "token"
    api_rate_limit_retry_429_max = 2  # → max 3 attempts total
    api_rate_limit_retry_429_base_delay = 0.01


class _FakeClient(BaseKISClient):
    def __init__(self) -> None:  # type: ignore[override]
        self._unmapped_rate_limit_keys_logged: set = set()
        type(self)._shared_client_lock = None

    @property  # type: ignore[override]
    def _settings(self):  # type: ignore[override]
        return _FakeSettings()


def _make_client() -> _FakeClient:
    return _FakeClient()


def _patch_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get_limiter(*args, **kwargs):
        limiter = MagicMock()
        limiter.acquire = AsyncMock()
        return limiter

    monkeypatch.setattr(
        "app.services.brokers.kis.base.get_limiter", _fake_get_limiter
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_request_helper_retries_read_timeout_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-270: Default behavior unchanged — ReadTimeout still retries."""
    _patch_limiter(monkeypatch)
    client = _make_client()

    call_count = {"n": 0}

    async def _fake_execute(*args, **kwargs):
        call_count["n"] += 1
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(client, "_execute_http_request", _fake_execute)

    async def _fake_ensure_client(timeout=None):
        return MagicMock()

    monkeypatch.setattr(client, "_ensure_client", _fake_ensure_client)

    with pytest.raises(httpx.RequestError):
        await client._request_with_rate_limit_with_headers(
            "GET",
            "https://example.com/x",
            headers={},
            api_name="t",
        )

    # api_rate_limit_retry_429_max = 2 → 3 attempts
    assert call_count["n"] == 3, (
        f"Default should retry ReadTimeout 3 times, got {call_count['n']}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-270 && \
  uv run pytest tests/test_kis_request_error_retry_policy.py -v
```

Expected: `test_request_helper_retries_read_timeout_by_default` PASSES (current code already retries by default — this is the regression guard). If it fails because of fixture/limiter wiring, fix the fixture only — do not change source. Do not proceed until this test passes against the unmodified source.

- [ ] **Step 3: Add the failing ReadTimeout-no-retry test**

Append to `tests/test_kis_request_error_retry_policy.py`:

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_request_helper_does_not_retry_read_timeout_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-270: With retry_request_errors=False, ReadTimeout raises after 1 try."""
    _patch_limiter(monkeypatch)
    client = _make_client()

    call_count = {"n": 0}

    async def _fake_execute(*args, **kwargs):
        call_count["n"] += 1
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(client, "_execute_http_request", _fake_execute)

    async def _fake_ensure_client(timeout=None):
        return MagicMock()

    monkeypatch.setattr(client, "_ensure_client", _fake_ensure_client)

    with pytest.raises(httpx.RequestError):
        await client._request_with_rate_limit_with_headers(
            "GET",
            "https://example.com/x",
            headers={},
            api_name="t",
            retry_request_errors=False,
        )

    assert call_count["n"] == 1, (
        "retry_request_errors=False must short-circuit RequestError retries; "
        f"got {call_count['n']} attempts"
    )
```

- [ ] **Step 4: Run test to verify it fails**

Run:
```bash
uv run pytest tests/test_kis_request_error_retry_policy.py::test_request_helper_does_not_retry_read_timeout_when_disabled -v
```

Expected: FAIL — `_request_with_rate_limit_with_headers` 는 아직 `retry_request_errors` 파라미터를 받지 않으므로 `TypeError: ... unexpected keyword argument 'retry_request_errors'`.

- [ ] **Step 5: Implement `retry_request_errors` option in base.py**

Modify both methods in `app/services/brokers/kis/base.py`.

Replace the `_request_with_rate_limit` signature block (around line 374-385):

```python
    async def _request_with_rate_limit(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 5.0,
        api_name: str = "unknown",
        tr_id: str | None = None,
        retry_request_errors: bool = True,
    ) -> dict[str, Any]:
```

And inside it, when delegating, pass through:

```python
        data, _headers = await self._request_with_rate_limit_with_headers(
            method,
            url,
            headers=headers,
            params=params,
            json_body=json_body,
            timeout=timeout,
            api_name=api_name,
            tr_id=tr_id,
            retry_request_errors=retry_request_errors,
        )
        return data
```

Replace the `_request_with_rate_limit_with_headers` signature (around line 418-429):

```python
    async def _request_with_rate_limit_with_headers(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 5.0,
        api_name: str = "unknown",
        tr_id: str | None = None,
        retry_request_errors: bool = True,
    ) -> tuple[dict[str, Any], dict[str, str]]:
```

Modify the `except httpx.RequestError` block (around line 533-549):

```python
            except httpx.RequestError as e:
                last_error = e
                if retry_request_errors and attempt < max_retries:
                    wait_time = self._calculate_retry_delay(
                        attempt=attempt, retry_after=0
                    )
                    logging.warning(
                        "[%s] Request error for %s: %s, attempt %d/%d, retrying in %.3fs",
                        "kis",
                        api_name,
                        e,
                        attempt + 1,
                        max_retries + 1,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise
```

The 429 branches (`status_code == 429` and `is_rate_limited` heuristic and `HTTPStatusError(429)`) MUST NOT reference `retry_request_errors` — leave them exactly as they are.

- [ ] **Step 6: Run both tests to verify they pass**

Run:
```bash
uv run pytest tests/test_kis_request_error_retry_policy.py -v
```

Expected: both PASS.

- [ ] **Step 7: Add the 429-still-retries-when-disabled test**

Append to `tests/test_kis_request_error_retry_policy.py`:

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_request_helper_retries_429_even_when_request_errors_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-270: 429 retry path is independent of retry_request_errors."""
    _patch_limiter(monkeypatch)
    client = _make_client()

    call_count = {"n": 0}

    async def _fake_execute(*args, **kwargs):
        call_count["n"] += 1
        # Build a fake httpx.Response carrying 429 status
        response = MagicMock()
        response.status_code = 429
        response.headers = {"Retry-After": "0"}
        # When code branches into _parse_kis_response, it won't run because
        # status_code == 429 is handled by the explicit branch first.
        return response

    monkeypatch.setattr(client, "_execute_http_request", _fake_execute)

    async def _fake_ensure_client(timeout=None):
        return MagicMock()

    monkeypatch.setattr(client, "_ensure_client", _fake_ensure_client)

    # 3 attempts then RateLimitExceededError
    from app.core.async_rate_limiter import RateLimitExceededError

    with pytest.raises(RateLimitExceededError):
        await client._request_with_rate_limit_with_headers(
            "GET",
            "https://example.com/x",
            headers={},
            api_name="t",
            retry_request_errors=False,
        )

    assert call_count["n"] == 3, (
        "429 must still retry 3 times regardless of retry_request_errors; "
        f"got {call_count['n']}"
    )
```

- [ ] **Step 8: Run all three tests**

Run:
```bash
uv run pytest tests/test_kis_request_error_retry_policy.py -v
```

Expected: 3 PASS.

- [ ] **Step 9: Commit**

```bash
git add app/services/brokers/kis/base.py tests/test_kis_request_error_retry_policy.py
git commit -m "$(cat <<'EOF'
feat(ROB-270): add retry_request_errors flag to KIS request helper

Adds an optional `retry_request_errors: bool = True` parameter to
`_request_with_rate_limit` and `_request_with_rate_limit_with_headers`.
Default preserves existing behavior (RequestError retries up to
api_rate_limit_retry_429_max). When set False, the httpx.RequestError
branch raises after the first attempt, while the 429/HTTPStatusError
retry logic remains unchanged.

This is the helper-layer building block for ROB-270 KIS mock VTS
balance snapshot, where short 5s timeouts × 3 attempts amplify
production /invest/api/account-panel latency to >15s.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

Expected: commit succeeds; status clean.

---

## Task 2: `fetch_domestic_balance_snapshot` 외부 파라미터화

**Files:**
- Modify: `app/services/brokers/kis/account.py` (lines 186-298)
- Modify: `tests/test_kis_domestic_balance_snapshot.py` (add propagation tests)

**Intent:** `fetch_domestic_balance_snapshot` 가 `timeout`, `retry_request_errors`, `max_pages` 를 받아 helper 로 propagate. 디폴트는 라이브 호환 (`timeout=5.0`, `retry_request_errors=True`, `max_pages=10`).

- [ ] **Step 1: Write failing test — propagation of new params**

Append to `tests/test_kis_domestic_balance_snapshot.py`:

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_propagates_timeout_and_retry_flag_to_request() -> None:
    """ROB-270: fetch_domestic_balance_snapshot honors per-call timeout and
    retry_request_errors when explicitly passed, defaulting to live values
    otherwise."""
    parent = _FakeParent(
        responses=[
            _page(stocks=[], cash={}, tr_cont="D"),
        ]
    )
    # Wrap stub to capture the actually-passed kwargs
    original_stub = parent._request_with_rate_limit_with_headers
    captured: dict[str, Any] = {}

    async def _capturing_stub(*args: Any, **kwargs: Any):
        captured.update(kwargs)
        return await original_stub(*args, **kwargs)

    parent._request_with_rate_limit_with_headers = _capturing_stub
    client = AccountClient(parent)

    await client.fetch_domestic_balance_snapshot(
        is_mock=True,
        timeout=10.0,
        retry_request_errors=False,
    )

    assert captured.get("timeout") == pytest.approx(10.0)
    assert captured.get("retry_request_errors") is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_defaults_preserve_live_request_policy() -> None:
    """ROB-270: Live default — timeout=5, retry_request_errors=True."""
    parent = _FakeParent(
        responses=[
            _page(stocks=[], cash={}, tr_cont="D"),
        ]
    )
    captured: dict[str, Any] = {}
    original = parent._request_with_rate_limit_with_headers

    async def _capturing_stub(*args: Any, **kwargs: Any):
        captured.update(kwargs)
        return await original(*args, **kwargs)

    parent._request_with_rate_limit_with_headers = _capturing_stub
    client = AccountClient(parent)

    await client.fetch_domestic_balance_snapshot()  # all defaults

    assert captured.get("timeout") == pytest.approx(5.0)
    assert captured.get("retry_request_errors", True) is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_honors_explicit_max_pages_cap() -> None:
    """ROB-270: max_pages param can shrink the live default cap for mock UI."""
    # Two continuation pages then end — but with max_pages=1 we stop after 1.
    parent = _FakeParent(
        responses=[
            _page(stocks=[{"pdno": "A", "hldg_qty": "1"}], ctx_nk="X", tr_cont="F"),
            # If max_pages is honored we never reach this response.
            _page(stocks=[{"pdno": "B", "hldg_qty": "1"}], tr_cont="D"),
        ]
    )
    client = AccountClient(parent)

    snapshot = await client.fetch_domestic_balance_snapshot(
        is_mock=True, max_pages=1
    )

    assert snapshot["page_count"] == 1
    assert len(parent.calls) == 1
    assert snapshot["holdings"][0]["pdno"] == "A"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/test_kis_domestic_balance_snapshot.py::test_snapshot_propagates_timeout_and_retry_flag_to_request \
  tests/test_kis_domestic_balance_snapshot.py::test_snapshot_defaults_preserve_live_request_policy \
  tests/test_kis_domestic_balance_snapshot.py::test_snapshot_honors_explicit_max_pages_cap -v
```

Expected: first FAILS (`TypeError: ... 'retry_request_errors'`), third FAILS (`max_pages` param not accepted), second may pass-by-accident — fine, still verify behavior with `max_pages` separately.

- [ ] **Step 3: Implement the param propagation in account.py**

In `app/services/brokers/kis/account.py`, change the `fetch_domestic_balance_snapshot` signature (around line 186-190):

```python
    async def fetch_domestic_balance_snapshot(
        self,
        *,
        is_mock: bool = False,
        timeout: float = 5.0,
        retry_request_errors: bool = True,
        max_pages: int = 10,
    ) -> dict[str, Any]:
```

Replace the `max_pages = 10` hard-coded local at line 223 with use of the param:

```python
        all_stocks: list[dict[str, Any]] = []
        cash: dict[str, Any] = {}
        ctx_area_fk = ""
        ctx_area_nk = ""
        tr_cont_req = ""
        page = 1
        stop_reason = "max_pages"
```

(i.e. delete the `max_pages = 10` line; the loop already references `max_pages`).

Pass new params into the helper call (around line 251-259):

```python
            js, resp_headers = await self._parent._request_with_rate_limit_with_headers(
                "GET",
                self._parent._kis_url(constants.DOMESTIC_BALANCE_URL),
                headers=hdr,
                params=params,
                timeout=timeout,
                api_name="fetch_domestic_balance_snapshot",
                tr_id=tr_id,
                retry_request_errors=retry_request_errors,
            )
```

Leave the docstring substantive — append a short note:

```python
        """Fetch domestic balance as a single snapshot of holdings + cash.

        ...
        Args:
            is_mock: when True, use the mock (VTS) TR id.
            timeout: per-request timeout in seconds (default 5.0, preserves
                live behavior; mock UI reader passes 10.0 — see ROB-270).
            retry_request_errors: when False, httpx RequestError (incl.
                ReadTimeout) raises after a single attempt; defaults True
                to keep live behavior. 429 retry is independent of this flag.
            max_pages: cap on snapshot pagination (default 10, preserves
                live behavior; mock UI reader passes a smaller cap).
        ...
        """
```

(Keep the existing docstring body verbatim; just add the `Args:` block before `Returns:`.)

- [ ] **Step 4: Run new and existing snapshot tests**

Run:
```bash
uv run pytest tests/test_kis_domestic_balance_snapshot.py -v
```

Expected: all PASS (existing 10 + new 3 = 13).

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/kis/account.py tests/test_kis_domestic_balance_snapshot.py
git commit -m "$(cat <<'EOF'
feat(ROB-270): expose timeout/retry/max_pages on domestic balance snapshot

Adds optional `timeout`, `retry_request_errors`, `max_pages` parameters
to AccountClient.fetch_domestic_balance_snapshot. Defaults preserve the
existing live KIS behavior (timeout=5s, retry_request_errors=True,
max_pages=10). Mock UI readers can now opt into a longer timeout, no
ReadTimeout retry, and a smaller pagination cap without changing live
broker call semantics.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 3: KISMockHomeReader 에 mock 전용 timeout 정책 + total wall-time bound

**Files:**
- Modify: `app/services/invest_home_readers.py` (`KISMockHomeReader.fetch`, lines 720-856)
- Modify: `tests/test_invest_home_readers.py` (add ROB-270 KIS mock policy tests)

**Intent:** Mock 호출에 `timeout=10.0`, `retry_request_errors=False`, `max_pages=3` 명시. 전체 호출은 `asyncio.wait_for(..., timeout=12.0)` 으로 감싸 wall-time bound. Timeout 발생 시 기존 fallback (`InvestHomeWarning(source="kis_mock", message=...)`) 으로 degrade. Sentry span 에 `kis_mock.timeout_sec`, `kis_mock.total_timeout_sec`, `kis_mock.retry_request_errors`, `kis_mock.max_pages`, `kis_mock.timed_out`, `kis_mock.attempt_count` 태그/데이터 부여.

- [ ] **Step 1: Write failing test — reader passes the new params**

Append to `tests/test_invest_home_readers.py` (after the existing ROB-238/268 mock blocks):

```python
# ---------------------------------------------------------------------------
# ROB-270: KIS mock UI read path uses bounded single-attempt timeout policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_passes_bounded_single_attempt_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-270: KISMockHomeReader requests timeout=10, retry_request_errors=False,
    and a reduced max_pages cap from fetch_domestic_balance_snapshot."""
    captured: dict[str, Any] = {}

    class _CapturingAccount:
        async def fetch_domestic_balance_snapshot(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "holdings": [],
                "cash": {},
                "page_count": 1,
                "stop_reason": "tr_cont_end",
            }

    class _CapturingClient:
        def __init__(self) -> None:
            self.account = _CapturingAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _CapturingClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    assert result.warning is None or result.warning.source == "kis_mock"
    assert captured.get("is_mock") is True
    assert captured.get("timeout") == pytest.approx(10.0)
    assert captured.get("retry_request_errors") is False
    assert captured.get("max_pages") == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest tests/test_invest_home_readers.py::test_kis_mock_reader_passes_bounded_single_attempt_policy -v
```

Expected: FAIL — current reader passes only `is_mock=True`.

- [ ] **Step 3: Implement the bounded policy in `KISMockHomeReader.fetch`**

In `app/services/invest_home_readers.py`, locate `KISMockHomeReader.fetch` (line 720). Replace the snapshot-fetch block (around lines 732-740) with:

```python
        try:
            client = SafeKISMockClient()
            # ROB-268: single inquire-balance snapshot supplies both holdings
            # (output1) and cash (output2).
            # ROB-270: mock VTS is slow near the 5s boundary; use a single
            # longer attempt (10s) with no ReadTimeout retry and a reduced
            # pagination cap. The whole call is also bounded at 12s wall time
            # by the surrounding asyncio.wait_for so /invest/api/account-panel
            # cannot spend more than ~12s in the mock branch.
            mock_timeout_sec = 10.0
            mock_total_timeout_sec = 12.0
            mock_max_pages = 3
            mock_retry_request_errors = False

            async def _fetch_snapshot() -> dict[str, Any]:
                return await client.account.fetch_domestic_balance_snapshot(
                    is_mock=True,
                    timeout=mock_timeout_sec,
                    retry_request_errors=mock_retry_request_errors,
                    max_pages=mock_max_pages,
                )

            try:
                snapshot = await asyncio.wait_for(
                    _fetch_snapshot(), timeout=mock_total_timeout_sec
                )
                timed_out = False
            except asyncio.TimeoutError:
                timed_out = True
                # Surface the bound on Sentry, then degrade via the outer
                # exception handler.
                span = sentry_sdk.get_current_span()
                if span is not None:
                    span.set_tag("kis_mock.timed_out", True)
                    span.set_data("kis_mock.timeout_sec", mock_timeout_sec)
                    span.set_data(
                        "kis_mock.total_timeout_sec", mock_total_timeout_sec
                    )
                    span.set_data("kis_mock.max_pages", mock_max_pages)
                    span.set_tag(
                        "kis_mock.retry_request_errors",
                        mock_retry_request_errors,
                    )
                logger.warning(
                    "KIS mock fetch wall-time bound exceeded: %.1fs",
                    mock_total_timeout_sec,
                )
                return _SourceFetchResult(
                    accounts=[],
                    holdings=[],
                    warning=InvestHomeWarning(
                        source="kis_mock",
                        message="KIS 모의투자 조회 시간 초과",
                    ),
                )

            stocks_kr = snapshot.get("holdings") or []
            cash_payload = snapshot.get("cash") or {}
```

Also ensure `import asyncio` exists in `invest_home_readers.py` (check at top of file; if missing add it).

In the existing observability block (around lines 818-828), add the new tags:

```python
            # ROB-268: surface snapshot observability on the enclosing
            # `invest.home.kis_mock` span so deploy-time Sentry verification can
            # confirm the duplicate-call regression has been eliminated.
            # ROB-270: also surface the new mock-specific timeout policy.
            # Best-effort: no-op when Sentry has no active span.
            span = sentry_sdk.get_current_span()
            if span is not None:
                page_count = int(snapshot.get("page_count") or 1)
                cash_fallback = cash_krw is None or buying_power_krw is None
                span.set_tag("kis_mock.used_cash_from_snapshot", True)
                span.set_tag("kis_mock.cash_fallback", cash_fallback)
                stop_reason = snapshot.get("stop_reason")
                if stop_reason:
                    span.set_tag("kis_mock.pagination_stop_reason", stop_reason)
                span.set_data("kis_mock.balance_page_count", page_count)
                span.set_data("kis_mock.balance_call_count", page_count)
                # ROB-270 observability
                span.set_data("kis_mock.timeout_sec", mock_timeout_sec)
                span.set_data(
                    "kis_mock.total_timeout_sec", mock_total_timeout_sec
                )
                span.set_data("kis_mock.max_pages", mock_max_pages)
                span.set_tag(
                    "kis_mock.retry_request_errors", mock_retry_request_errors
                )
                span.set_tag("kis_mock.timed_out", timed_out)
                span.set_data("kis_mock.attempt_count", 1)
```

- [ ] **Step 4: Run the new test**

Run:
```bash
uv run pytest tests/test_invest_home_readers.py::test_kis_mock_reader_passes_bounded_single_attempt_policy -v
```

Expected: PASS.

- [ ] **Step 5: Write failing test — wall-time bound degrades to warning**

Append to `tests/test_invest_home_readers.py`:

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_degrades_when_wall_time_bound_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-270: When snapshot exceeds the wall-time bound, the reader returns
    a sanitized warning instead of propagating, so live/manual/Upbit sources
    can still render."""
    import asyncio as _asyncio

    class _SlowAccount:
        async def fetch_domestic_balance_snapshot(self, **kwargs: Any) -> dict[str, Any]:
            await _asyncio.sleep(1.0)  # longer than the patched bound
            return {"holdings": [], "cash": {}, "page_count": 1}

    class _SlowClient:
        def __init__(self) -> None:
            self.account = _SlowAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _SlowClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    # Patch the wait_for timeout to a tiny value by patching asyncio.wait_for
    real_wait_for = _asyncio.wait_for

    async def _short_wait_for(coro, timeout):
        return await real_wait_for(coro, timeout=0.05)

    monkeypatch.setattr(readers.asyncio, "wait_for", _short_wait_for)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    assert result.accounts == []
    assert result.holdings == []
    assert result.warning is not None
    assert result.warning.source == "kis_mock"
    assert "시간" in result.warning.message or "초과" in result.warning.message
```

- [ ] **Step 6: Run test to verify it passes**

Run:
```bash
uv run pytest tests/test_invest_home_readers.py::test_kis_mock_reader_degrades_when_wall_time_bound_exceeded -v
```

Expected: PASS (the implementation in Step 3 already handles `asyncio.TimeoutError`).

If FAIL because `readers.asyncio` is not the same object referenced inside `_fetch`, switch the monkeypatch target to be the actual import path. Acceptable alternative: rewrite the test to use `monkeypatch.setattr(readers, "_kis_mock_total_timeout_sec", 0.05)` after extracting the constant — but only if the simple monkeypatch fails.

- [ ] **Step 7: Run the broader reader test suite to confirm no regression**

Run:
```bash
uv run pytest tests/test_invest_home_readers.py -v
```

Expected: all PASS (ROB-238 + ROB-268 + 2 new ROB-270 tests).

- [ ] **Step 8: Commit**

```bash
git add app/services/invest_home_readers.py tests/test_invest_home_readers.py
git commit -m "$(cat <<'EOF'
feat(ROB-270): bound KIS mock balance snapshot to 10s × 1 attempt + 12s wall-time

KISMockHomeReader now requests fetch_domestic_balance_snapshot with
timeout=10s, retry_request_errors=False, max_pages=3, and wraps the call
in asyncio.wait_for(timeout=12s). On wall-time exceed, returns the
existing kis_mock warning fallback so live/manual/Upbit sources are
not affected.

Adds Sentry span data/tags under the existing kis_mock.* prefix:
- kis_mock.timeout_sec
- kis_mock.total_timeout_sec
- kis_mock.max_pages
- kis_mock.retry_request_errors
- kis_mock.timed_out
- kis_mock.attempt_count

Live KIS path and ROB-268 snapshot coalescing are untouched.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 4: Live broker tests + ruff/format

Goal: confirm no regression to live KIS code paths, run repo-wide lint.

- [ ] **Step 1: Run all KIS-related tests**

Run:
```bash
uv run pytest \
  tests/test_kis_request_error_retry_policy.py \
  tests/test_kis_domestic_balance_snapshot.py \
  tests/test_invest_home_readers.py \
  tests/test_kis_base_rate_limit.py \
  tests/test_kis_domestic_orders_retry.py \
  tests/test_kis_overseas_orders_retry.py \
  tests/test_kis_mock_routing.py \
  tests/test_kis_integrated_margin_mock.py \
  tests/test_kis_domestic_pending_mock.py \
  tests/test_kis_overseas_pending_mock.py \
  tests/test_async_rate_limiter.py \
  -v
```

Expected: all PASS (existing live-broker tests untouched).

- [ ] **Step 2: Run ruff**

Run:
```bash
uv run ruff check app/services/brokers/kis/base.py \
  app/services/brokers/kis/account.py \
  app/services/invest_home_readers.py \
  tests/test_kis_request_error_retry_policy.py \
  tests/test_kis_domestic_balance_snapshot.py \
  tests/test_invest_home_readers.py
```

Expected: no findings. If there are, run `uv run ruff check --fix` and re-commit only the formatting fix.

- [ ] **Step 3: Run ruff format**

Run:
```bash
uv run ruff format app/services/brokers/kis/base.py \
  app/services/brokers/kis/account.py \
  app/services/invest_home_readers.py \
  tests/test_kis_request_error_retry_policy.py \
  tests/test_kis_domestic_balance_snapshot.py \
  tests/test_invest_home_readers.py
```

Expected: 0 files reformatted (or 6 files reformatted with no diff to test logic).

- [ ] **Step 4: Stage and commit formatting if any changed**

```bash
git status
# If formatting reformatted any files:
git add -p   # review carefully — only formatting hunks
git commit -m "$(cat <<'EOF'
chore(ROB-270): ruff format on touched files

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

Otherwise skip.

---

## Task 5: Read-only smoke + verification report

Goal: confirm `/invest/api/account-panel?includePaper=true&paperSources=kis_mock` still returns HTTP 200, ROB-268 coalescing intact, no broker/order mutation introduced.

- [ ] **Step 1: Static check — no broker mutation imports added**

Run:
```bash
git diff main -- app/services/invest_home_readers.py \
  app/services/brokers/kis/account.py \
  app/services/brokers/kis/base.py | \
  grep -E "submit_order|cancel_order|modify_order|create_order|place_order|_post|order_intent" || \
  echo "OK: no mutation-related identifiers added"
```

Expected: `OK: no mutation-related identifiers added`. If any line shows, abort and investigate.

- [ ] **Step 2: Diff summary for live-path verification**

Run:
```bash
git diff main --stat
git diff main -- app/services/brokers/kis/base.py | head -120
git diff main -- app/services/brokers/kis/account.py | head -80
```

Visually confirm:
- `base.py`: only `retry_request_errors` param added, only `except httpx.RequestError` branch gated. 429/HTTPStatusError branches unchanged.
- `account.py`: signature gains 3 params with live-compatible defaults; helper call propagates them; no other behavior change.

- [ ] **Step 3: Run targeted account-panel smoke (read-only, no creds touched)**

Run:
```bash
uv run pytest tests/ -v -k "account_panel or kis_mock_reader" 2>&1 | tail -60
```

Expected: all matching tests PASS.

- [ ] **Step 4: Produce the verification report**

Print to console the following report (do NOT save to a file unless user asks):

```
ROB-270 — Implementation Verification Report

Changed files:
  - app/services/brokers/kis/base.py
  - app/services/brokers/kis/account.py
  - app/services/invest_home_readers.py
  - tests/test_kis_request_error_retry_policy.py (new)
  - tests/test_kis_domestic_balance_snapshot.py
  - tests/test_invest_home_readers.py

Live KIS path behavior change: NONE
  - _request_with_rate_limit{,_with_headers}: new param retry_request_errors
    defaults True; live callers pass nothing → behavior identical.
  - fetch_domestic_balance_snapshot: defaults (timeout=5.0,
    retry_request_errors=True, max_pages=10) match the pre-PR hard-coded
    values; live callers pass nothing → behavior identical.

ReadTimeout no-retry test:
  - tests/test_kis_request_error_retry_policy.py::
      test_request_helper_does_not_retry_read_timeout_when_disabled — PASS

429 retry preserved test:
  - tests/test_kis_request_error_retry_policy.py::
      test_request_helper_retries_429_even_when_request_errors_disabled — PASS

Targeted tests:
  - tests/test_kis_request_error_retry_policy.py — 3/3 PASS
  - tests/test_kis_domestic_balance_snapshot.py — 13/13 PASS
  - tests/test_invest_home_readers.py — N/N PASS (ROB-270 cases 2/2 PASS)

ruff: clean
ruff format: clean

Read-only smoke:
  - Targeted account_panel/kis_mock_reader pytest selection — PASS
  - (Production-env read-only probe out of scope for this CI; deploy-time
    Sentry verification per acceptance criteria.)

Mutation surface:
  - No broker/order/watch/order-intent identifiers added to diff
  - Read-only KIS UI path only
```

- [ ] **Step 5: Final commit (if anything left uncommitted)**

```bash
git status
```

Expected: clean working tree. If anything remains, commit it under a `chore(ROB-270): finalize` message.

- [ ] **Step 6: Push branch (only if user explicitly asks; otherwise skip)**

Per safety guidance, do not push without explicit request.

---

## Self-Review

**1. Spec coverage** (against the user's 8 requirements):

1. ✅ Mock path 전체 wall-time bound (Task 3 — `asyncio.wait_for(..., 12.0)`)
2. ✅ retry_request_errors 좁은 플러밍 (Task 1 — helper param, default True; Task 2 — snapshot param, default True; Task 3 — reader explicit False)
3. ✅ 429 retry vs ReadTimeout no-retry 분리 테스트 (Task 1 Step 7 — `test_request_helper_retries_429_even_when_request_errors_disabled`)
4. ✅ `asyncio.sleep(0.1)` between pages 유지 (account.py 변경에서 line 298 그대로)
5. ✅ Sentry tag 컨벤션: `kis_mock.*` reader 레벨 (Task 3 Step 3 observability block)
6. ✅ 정량 검증 기준 (Plan goal + Task 5 Step 4 report; total_timeout=12s 의도 commit message + plan goal에 명시)
7. ✅ 다른 mock VTS endpoint 범위 밖 (Safety boundaries 명시; account.py 의 다른 helper 변경 없음)
8. ✅ 구현 후 보고 (Task 5 Step 4 — Verification Report 항목별 명시)

**2. Placeholder scan:** No TBD/TODO/"add appropriate"/"similar to" in any task. All code blocks are concrete.

**3. Type consistency:**
- `retry_request_errors: bool` 일관 (helper, snapshot, reader 모두 동일 이름·타입)
- `timeout: float` 일관
- `max_pages: int` 일관
- Sentry tag names: `kis_mock.timeout_sec`, `kis_mock.total_timeout_sec`, `kis_mock.max_pages`, `kis_mock.retry_request_errors`, `kis_mock.timed_out`, `kis_mock.attempt_count` — Task 3 Step 3 의 두 군데 (timeout 분기, 성공 분기) 가 모두 동일한 이름 사용
- Tests use the same param names as production signatures

---

## Acceptance Criteria recap (from ROB-270)

- [x] `/invest/api/account-panel?includePaper=true&paperSources=kis_mock` 에서 KIS mock balance 가 `ReadTimeout` 때문에 5s×3회 반복하지 않는다 — Task 1+3
- [x] KIS mock VTS timeout policy = 10s 단일 attempt + 12s wall-time bound — Task 3
- [x] Sentry 에서 timeout/attempt 정보 확인 가능 — Task 3 Step 3
- [x] read-only smoke 에서 account-panel HTTP 200 유지 — Task 5 Step 3
- [x] targeted tests 통과 — Task 4 Step 1, Task 5 Step 3
