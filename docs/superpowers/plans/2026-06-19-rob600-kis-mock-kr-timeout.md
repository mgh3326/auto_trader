# ROB-600 kis_mock KR Timeout / Empty-Error Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** kis_mock KR-domestic 주문/잔고의 간헐적 타임아웃이 진단 불가능한 빈 에러("")로 표면화되는 것을 고친다 — 구체 사유 노출 + mock read 타임아웃 상향 + 잔고 조회실패를 정직하게 표시.

**Architecture:** (a) 공용 `describe_exception` 헬퍼로 빈 예외 문자열을 클래스명으로 폴백(`order_execution`, `portfolio_cash`, `kis/base` 호출부). (b) `inquire_domestic_cash_balance`의 read 타임아웃을 mock일 때 5→10초. (c) `get_cash_balance_impl`/`get_available_capital_impl`의 `summary`에 `unavailable_sources` dict를 additive로 추가(placeholder row 미사용 → live precheck 회귀 방지). DB 마이그레이션 0, MCP 계약 하위호환.

**Tech Stack:** Python 3.13, pytest / pytest-asyncio, httpx, unittest.mock. uv 런너.

## Global Constraints

- **주문 전송(place/cancel/modify)에는 타임아웃 재시도를 추가하지 않는다** (double-submit 위험; ROB-585 스코프). 이 플랜은 `domestic_orders.py`/`overseas_orders.py`를 건드리지 않는다.
- **DB 마이그레이션 0** — 스키마/모델 변경 없음.
- **MCP 계약은 additive only** — `summary.unavailable_sources`(신규 optional `dict[str,str]`), error 문자열이 빈→비-빈으로만 변경.
- `unavailable_sources` shape = `dict[str, str]` (`{account_key: reason}`), 코드베이스 기존 관용구와 동일.
- live/US read 타임아웃, 기존 ~10곳 `str(exc) or __class__.__name__` 일괄 마이그레이션은 **스코프 밖**.
- 모든 테스트는 DB/실네트워크 없이 모킹으로 구성. 명령은 `uv run pytest ...`.
- 커밋 메시지 footer:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01QTE5SngqNmNPp8Dx9hdwUU
  ```

---

### Task 1: `describe_exception` 공용 헬퍼

**Files:**
- Create: `app/core/exceptions.py`
- Test: `tests/core/test_exceptions.py`

**Interfaces:**
- Produces: `describe_exception(exc: BaseException) -> str` — 비어있지 않은 구체 사유(메시지 없으면 클래스명).

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_exceptions.py`:

```python
import httpx

from app.core.exceptions import describe_exception


def test_empty_message_falls_back_to_class_name():
    assert describe_exception(httpx.ReadTimeout("")) == "ReadTimeout"
    assert describe_exception(httpx.ConnectTimeout("")) == "ConnectTimeout"


def test_whitespace_only_message_falls_back_to_class_name():
    assert describe_exception(ValueError("   ")) == "ValueError"


def test_nonempty_message_is_preserved():
    assert describe_exception(RuntimeError("EGW00201 초당 거래건수 초과")) == (
        "EGW00201 초당 거래건수 초과"
    )
    assert describe_exception(httpx.ReadTimeout("Read timed out")) == "Read timed out"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_exceptions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.exceptions'`

- [ ] **Step 3: Write minimal implementation**

Create `app/core/exceptions.py`:

```python
"""Shared exception helpers."""

from __future__ import annotations


def describe_exception(exc: BaseException) -> str:
    """Return a non-empty, concrete reason string for an exception.

    httpx timeout exceptions (ReadTimeout / ConnectTimeout / PoolTimeout, ...) are
    frequently constructed with no message, so ``str(exc)`` yields ``""``. Surfacing
    that empty string as a user-facing ``error`` makes timeouts undiagnosable
    (ROB-600). When the message is empty/whitespace, fall back to the exception class
    name so e.g. ``ReadTimeout`` is shown instead of ``""``.

    Consolidates the ``str(exc) or exc.__class__.__name__`` idiom scattered across the
    codebase.
    """
    return str(exc).strip() or type(exc).__name__
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/core/test_exceptions.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/core/exceptions.py tests/core/test_exceptions.py
git commit -m "feat(ROB-600): describe_exception helper — non-empty reason for empty-str exceptions

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QTE5SngqNmNPp8Dx9hdwUU"
```

---

### Task 2: (a) order-send 빈에러 → 구체사유 (`order_execution.py`)

**Files:**
- Modify: `app/mcp_server/tooling/order_execution.py` (import; `:1128` `error=`; `:1138` `_order_error`)
- Test: `tests/test_mcp_place_order.py` (append)

**Interfaces:**
- Consumes: `describe_exception` (Task 1).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_place_order.py`:

```python
@pytest.mark.asyncio
async def test_place_order_readtimeout_surfaces_class_name(monkeypatch):
    """ROB-600: a ReadTimeout during execution must surface 'ReadTimeout', not ''."""
    import httpx

    from app.mcp_server.tooling import order_execution

    recorded = AsyncMock()
    monkeypatch.setattr(
        order_execution,
        "_resolve_market_type",
        lambda symbol, market: ("equity_kr", "005930"),
    )
    monkeypatch.setattr(order_execution, "_record_order_history", recorded)
    monkeypatch.setattr(
        order_execution,
        "_fetch_current_price",
        AsyncMock(side_effect=httpx.ReadTimeout("")),
    )

    result = await order_execution._place_order_impl(
        symbol="005930",
        side="sell",
        order_type="limit",
        quantity=1,
        price=370000.0,
        dry_run=False,
        is_mock=True,
    )

    assert result["success"] is False
    assert result["error"] == "ReadTimeout"
    assert result["source"] == "kis"
    # :1128 — order-history record also gets the concrete reason, not ""
    assert recorded.await_args.kwargs["error"] == "ReadTimeout"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_place_order.py::test_place_order_readtimeout_surfaces_class_name -v`
Expected: FAIL — `assert '' == 'ReadTimeout'` (current code uses bare `str(exc)`)

- [ ] **Step 3: Write minimal implementation**

In `app/mcp_server/tooling/order_execution.py`, add the import next to the existing `from app.mcp_server.tooling.shared import ...` block (around line 41):

```python
from app.core.exceptions import describe_exception
```

Change the `_record_order_history` call inside the outer `except Exception as exc:` block (line ~1128) from:

```python
            error=str(exc),
```
to:
```python
            error=describe_exception(exc),
```

Change the final return (line ~1138) from:

```python
        return _order_error(str(exc))
```
to:
```python
        return _order_error(describe_exception(exc))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_place_order.py::test_place_order_readtimeout_surfaces_class_name -v`
Expected: PASS

- [ ] **Step 5: Run the surrounding suite to confirm no regression**

Run: `uv run pytest tests/test_mcp_place_order.py -q`
Expected: PASS (all existing tests still green)

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/order_execution.py tests/test_mcp_place_order.py
git commit -m "fix(ROB-600): order-send timeout surfaces concrete reason, not empty error

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QTE5SngqNmNPp8Dx9hdwUU"
```

---

### Task 3: (b) mock read 타임아웃 5→10 (`account.py`)

**Files:**
- Modify: `app/services/brokers/kis/account.py:565` (`inquire_domestic_cash_balance` timeout)
- Test: `tests/services/brokers/kis/test_account_cash_timeout.py` (create)

**Interfaces:**
- `AccountClient(parent).inquire_domestic_cash_balance(is_mock: bool) -> dict` — request timeout is `10` when `is_mock=True`, else `5`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/brokers/kis/test_account_cash_timeout.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.brokers.kis.account import AccountClient


class _Settings:
    kis_account_no = "12345678-01"
    kis_access_token = "tok"


def _make_account_client():
    parent = MagicMock()
    parent._settings = _Settings()
    parent._ensure_token = AsyncMock()
    parent._hdr_base = {}
    parent._kis_url = lambda path: f"https://host{path}"
    parent._request_with_rate_limit = AsyncMock(
        return_value={"rt_cd": "0", "output2": [{}]}
    )
    return AccountClient(parent), parent


@pytest.mark.asyncio
async def test_inquire_domestic_cash_balance_mock_uses_10s_timeout():
    """ROB-600: mock VTS is slow near the 5s boundary; mock read uses 10s."""
    client, parent = _make_account_client()
    await client.inquire_domestic_cash_balance(is_mock=True)
    assert parent._request_with_rate_limit.call_args.kwargs["timeout"] == 10


@pytest.mark.asyncio
async def test_inquire_domestic_cash_balance_live_keeps_5s_timeout():
    client, parent = _make_account_client()
    await client.inquire_domestic_cash_balance(is_mock=False)
    assert parent._request_with_rate_limit.call_args.kwargs["timeout"] == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/brokers/kis/test_account_cash_timeout.py -v`
Expected: FAIL on the mock case — `assert 5 == 10` (current code hardcodes `timeout=5`)

- [ ] **Step 3: Write minimal implementation**

In `app/services/brokers/kis/account.py`, inside `inquire_domestic_cash_balance` (the `_request_with_rate_limit` call near line 560-568), change:

```python
            timeout=5,
```
to:
```python
            # ROB-600: mock VTS(openapivts) responds slowly near the 5s boundary →
            # intermittent ReadTimeout. Mock read uses 10s (mirrors ROB-270); live
            # host stays at 5s. No order-send timeout change (double-submit risk).
            timeout=10 if is_mock else 5,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/brokers/kis/test_account_cash_timeout.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/kis/account.py tests/services/brokers/kis/test_account_cash_timeout.py
git commit -m "fix(ROB-600): mock domestic cash read timeout 5->10s (slow openapivts boundary)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QTE5SngqNmNPp8Dx9hdwUU"
```

---

### Task 4: (a)+(c) `get_cash_balance_impl` — 구체사유 + `unavailable_sources`

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_cash.py` (import; `unavailable_sources` 누적 at toss/upbit/kis_kr/kis_us; summary 추가; paper-path summary)
- Test: `tests/test_portfolio_cash_kis_mock.py` (append)

**Interfaces:**
- Consumes: `describe_exception` (Task 1).
- Produces: `get_cash_balance_impl(...)["summary"]["unavailable_sources"] : dict[str, str]` — `{account_key: reason}`, 실패 없으면 `{}`. `accounts[]`엔 실패 source row를 추가하지 않는다.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_portfolio_cash_kis_mock.py` (top already imports `AsyncMock, MagicMock, pytest, portfolio_cash`; add `import httpx` at the top of the file):

```python
@pytest.mark.asyncio
async def test_cash_balance_mock_kis_timeout_surfaces_reason_and_marks_unavailable(
    monkeypatch,
):
    """ROB-600: a KIS read timeout must (1) surface 'ReadTimeout' (not ''),
    (2) appear in summary.unavailable_sources, (3) NOT add a kis_domestic row,
    (4) leave total_krw excluding KIS."""
    fake_kis = MagicMock()
    fake_kis.inquire_domestic_cash_balance = AsyncMock(side_effect=httpx.ReadTimeout(""))

    monkeypatch.setattr(
        portfolio_cash, "_create_kis_client", lambda *, is_mock: fake_kis
    )
    monkeypatch.setattr(
        portfolio_cash.upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 0.0, "orderable": 0.0}),
    )

    result = await portfolio_cash.get_cash_balance_impl(is_mock=True)

    # (1) concrete reason, not empty
    kis_kr_err = next(
        e for e in result["errors"] if e["source"] == "kis" and e["market"] == "kr"
    )
    assert kis_kr_err["error"] == "ReadTimeout"
    # (2) machine-readable unavailable flag
    assert result["summary"]["unavailable_sources"]["kis_domestic"] == "ReadTimeout"
    # (3) no placeholder row injected
    assert "kis_domestic" not in {a["account"] for a in result["accounts"]}
    # (4) KIS cash not silently summed as a number
    assert result["summary"]["total_krw"] == pytest.approx(0.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_portfolio_cash_kis_mock.py::test_cash_balance_mock_kis_timeout_surfaces_reason_and_marks_unavailable -v`
Expected: FAIL — `KeyError: 'unavailable_sources'` (summary has no such key yet) and the error would be `""` not `"ReadTimeout"`.

- [ ] **Step 3: Write minimal implementation**

In `app/mcp_server/tooling/portfolio_cash.py`:

3a. Add the import near the top (with the other `from app...` imports):

```python
from app.core.exceptions import describe_exception
```

3b. Initialize the accumulator next to `errors` (the non-paper branch, around line 124-127). Change:

```python
    accounts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    total_krw = 0.0
    total_usd = 0.0
```
to:
```python
    accounts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    unavailable_sources: dict[str, str] = {}
    total_krw = 0.0
    total_usd = 0.0
```

3c. Toss except (around line 169-176). Change:

```python
            except Exception as exc:
                if strict_mode:
                    raise RuntimeError(
                        f"Toss cash balance query failed: {exc}"
                    ) from exc
                errors.append(
                    {"source": "toss_api", "market": "cash", "error": str(exc)}
                )
```
to:
```python
            except Exception as exc:
                if strict_mode:
                    raise RuntimeError(
                        f"Toss cash balance query failed: {exc}"
                    ) from exc
                reason = describe_exception(exc)
                errors.append(
                    {"source": "toss_api", "market": "cash", "error": reason}
                )
                unavailable_sources["toss"] = reason
```

3d. Upbit except (around line 195-196). Change:

```python
        except Exception as exc:
            errors.append({"source": "upbit", "market": "crypto", "error": str(exc)})
```
to:
```python
        except Exception as exc:
            reason = describe_exception(exc)
            errors.append({"source": "upbit", "market": "crypto", "error": reason})
            unavailable_sources["upbit"] = reason
```

3e. KIS domestic except (around line 247-252). Change:

```python
            except Exception as exc:
                if strict_mode:
                    raise RuntimeError(
                        f"KIS domestic cash balance query failed: {exc}"
                    ) from exc
                errors.append({"source": "kis", "market": "kr", "error": str(exc)})
```
to:
```python
            except Exception as exc:
                if strict_mode:
                    raise RuntimeError(
                        f"KIS domestic cash balance query failed: {exc}"
                    ) from exc
                reason = describe_exception(exc)
                errors.append({"source": "kis", "market": "kr", "error": reason})
                unavailable_sources["kis_domestic"] = reason
```

3f. KIS overseas mock_unsupported (around line 255-262). Change:

```python
            if is_mock:
                errors.append(
                    {
                        "source": "kis",
                        "market": "us",
                        "error": "mock_unsupported: KIS overseas margin is not available in mock mode",
                    }
                )
```
to:
```python
            if is_mock:
                reason = (
                    "mock_unsupported: KIS overseas margin is not available in mock mode"
                )
                errors.append({"source": "kis", "market": "us", "error": reason})
                unavailable_sources["kis_overseas"] = reason
```

3g. KIS overseas except (around line 298-303). Change:

```python
                except Exception as exc:
                    if strict_mode:
                        raise RuntimeError(
                            f"KIS overseas cash balance query failed: {exc}"
                        ) from exc
                    errors.append({"source": "kis", "market": "us", "error": str(exc)})
```
to:
```python
                except Exception as exc:
                    if strict_mode:
                        raise RuntimeError(
                            f"KIS overseas cash balance query failed: {exc}"
                        ) from exc
                    reason = describe_exception(exc)
                    errors.append({"source": "kis", "market": "us", "error": reason})
                    unavailable_sources["kis_overseas"] = reason
```

3h. Non-paper return summary (around line 305-312). Change:

```python
    return {
        "accounts": accounts,
        "summary": {
            "total_krw": total_krw,
            "total_usd": total_usd,
        },
        "errors": errors,
    }
```
to:
```python
    return {
        "accounts": accounts,
        "summary": {
            "total_krw": total_krw,
            "total_usd": total_usd,
            "unavailable_sources": unavailable_sources,
        },
        "errors": errors,
    }
```

3i. Paper-account early return (around line 118-122) — keep the contract uniform. Change:

```python
        return {
            "accounts": rows,
            "summary": {"total_krw": total_krw, "total_usd": total_usd},
            "errors": errors,
        }
```
to:
```python
        return {
            "accounts": rows,
            "summary": {
                "total_krw": total_krw,
                "total_usd": total_usd,
                "unavailable_sources": {},
            },
            "errors": errors,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_portfolio_cash_kis_mock.py::test_cash_balance_mock_kis_timeout_surfaces_reason_and_marks_unavailable -v`
Expected: PASS

- [ ] **Step 5: Run the file to confirm existing tests still pass**

Run: `uv run pytest tests/test_portfolio_cash_kis_mock.py -q`
Expected: PASS (existing 2 + new 1)

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/portfolio_cash.py tests/test_portfolio_cash_kis_mock.py
git commit -m "fix(ROB-600): cash balance surfaces concrete reason + summary.unavailable_sources

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QTE5SngqNmNPp8Dx9hdwUU"
```

---

### Task 5: (c) `get_available_capital_impl` 전파 + live-precheck 회귀가드

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_cash.py` (`get_available_capital_impl` summary에 `unavailable_sources` 전파, around line 433-442)
- Test: `tests/test_portfolio_cash_kis_mock.py` (append)

**Interfaces:**
- Consumes: `get_cash_balance_impl(...)["summary"]["unavailable_sources"]` (Task 4).
- Produces: `get_available_capital_impl(...)["summary"]["unavailable_sources"] : dict[str, str]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_portfolio_cash_kis_mock.py`:

```python
@pytest.mark.asyncio
async def test_available_capital_propagates_unavailable_sources(monkeypatch):
    """ROB-600: capital summary carries unavailable_sources so KIS failure is not
    mistaken for 0 orderable cash."""
    fake_kis = MagicMock()
    fake_kis.inquire_domestic_cash_balance = AsyncMock(side_effect=httpx.ReadTimeout(""))

    monkeypatch.setattr(
        portfolio_cash, "_create_kis_client", lambda *, is_mock: fake_kis
    )
    monkeypatch.setattr(
        portfolio_cash.upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 0.0, "orderable": 0.0}),
    )
    monkeypatch.setattr(
        portfolio_cash, "get_account_costs_setting", AsyncMock(return_value=None)
    )

    result = await portfolio_cash.get_available_capital_impl(
        include_manual=False, is_mock=True
    )

    assert result["summary"]["unavailable_sources"]["kis_domestic"] == "ReadTimeout"
    assert result["summary"]["total_orderable_krw"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_live_kis_orderable_raises_when_row_missing(monkeypatch):
    """ROB-600 regression guard: with NO placeholder row added, the live precheck
    source still RAISES on a missing kis row instead of silently reading 0."""
    from app.mcp_server.tooling import order_validation

    monkeypatch.setattr(
        order_validation,
        "get_cash_balance_impl",
        AsyncMock(
            return_value={
                "accounts": [],
                "summary": {
                    "total_krw": 0.0,
                    "total_usd": 0.0,
                    "unavailable_sources": {"kis_domestic": "ReadTimeout"},
                },
                "errors": [{"source": "kis", "market": "kr", "error": "ReadTimeout"}],
            }
        ),
    )

    with pytest.raises(RuntimeError, match="orderable not found"):
        await order_validation._live_kis_orderable("kis_domestic")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_portfolio_cash_kis_mock.py::test_available_capital_propagates_unavailable_sources tests/test_portfolio_cash_kis_mock.py::test_live_kis_orderable_raises_when_row_missing -v`
Expected: `test_available_capital_propagates_unavailable_sources` FAILS — `KeyError: 'unavailable_sources'` (capital summary doesn't carry it yet). `test_live_kis_orderable_raises_when_row_missing` should already PASS (it locks in pre-existing behavior — confirms no placeholder regression). If it fails, a placeholder row was wrongly added.

- [ ] **Step 3: Write minimal implementation**

In `app/mcp_server/tooling/portfolio_cash.py`, `get_available_capital_impl`'s return (around line 433-442). Change:

```python
    return {
        "accounts": processed_accounts,
        "manual_cash": manual_cash_result,
        "summary": {
            "total_orderable_krw": total_orderable_krw,
            "manual_cash_excluded_krw": manual_cash_excluded_krw,
            "exchange_rate_usd_krw": exchange_rate,
            "as_of": now_kst().isoformat(),
        },
        "errors": errors,
    }
```
to:
```python
    return {
        "accounts": processed_accounts,
        "manual_cash": manual_cash_result,
        "summary": {
            "total_orderable_krw": total_orderable_krw,
            "manual_cash_excluded_krw": manual_cash_excluded_krw,
            "exchange_rate_usd_krw": exchange_rate,
            "as_of": now_kst().isoformat(),
            # ROB-600: propagate per-source lookup failures so a failed KIS read is
            # not mistaken for 0 orderable cash.
            "unavailable_sources": cash_result.get("summary", {}).get(
                "unavailable_sources", {}
            ),
        },
        "errors": errors,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_portfolio_cash_kis_mock.py::test_available_capital_propagates_unavailable_sources tests/test_portfolio_cash_kis_mock.py::test_live_kis_orderable_raises_when_row_missing -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/portfolio_cash.py tests/test_portfolio_cash_kis_mock.py
git commit -m "fix(ROB-600): propagate unavailable_sources into available_capital summary

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QTE5SngqNmNPp8Dx9hdwUU"
```

---

### Task 6: (a) `kis/base.py` 재시도 로그 구체화

**Files:**
- Modify: `app/services/brokers/kis/base.py` (import; `:544` retry log; `:557` RateLimitExceededError 방어 guard)
- Test: `tests/test_kis_base_rate_limit.py` (append)

**Interfaces:**
- Consumes: `describe_exception` (Task 1).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_kis_base_rate_limit.py` (top adds `import logging`, `import httpx`, and `from unittest.mock import AsyncMock`; `MagicMock` already imported):

```python
class _FastRetrySettings:
    kis_app_key = "key"
    kis_app_secret = "secret"
    kis_access_token = "token"
    api_rate_limit_retry_429_max = 1
    api_rate_limit_retry_429_base_delay = 0.0
    kis_rate_limit_rate = 19
    kis_rate_limit_period = 1.0


class _FastRetryClient(BaseKISClient):
    def __init__(self) -> None:  # type: ignore[override]
        self._unmapped_rate_limit_keys_logged: set = set()
        type(self)._shared_client_lock = None

    @property  # type: ignore[override]
    def _settings(self):  # type: ignore[override]
        return _FastRetrySettings()


@pytest.mark.asyncio
async def test_request_error_retry_log_names_the_exception(monkeypatch, caplog):
    """ROB-600: a ReadTimeout('') retry must log 'ReadTimeout', not a blank reason.
    The exception itself re-raises (bare raise); the empty str() is handled at the
    call sites via describe_exception."""
    client = _FastRetryClient()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    monkeypatch.setattr(client, "_get_limiter", AsyncMock(return_value=limiter))
    monkeypatch.setattr(client, "_ensure_client", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(
        client,
        "_execute_http_request",
        AsyncMock(side_effect=httpx.ReadTimeout("")),
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(httpx.ReadTimeout):
            await client._request_with_rate_limit_with_headers(
                "GET",
                "https://host/path",
                headers={},
                retry_request_errors=True,
                api_name="inquire_domestic_cash_balance",
            )

    assert any("ReadTimeout" in r.getMessage() for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kis_base_rate_limit.py::test_request_error_retry_log_names_the_exception -v`
Expected: FAIL — no log record contains "ReadTimeout" (current log formats the empty `e` → blank reason).

- [ ] **Step 3: Write minimal implementation**

In `app/services/brokers/kis/base.py`, add the import near the top (with the other `from app...`/stdlib imports, after `import httpx`):

```python
from app.core.exceptions import describe_exception
```

In the `except httpx.RequestError as e:` retry branch (around line 537-554), change the warning call argument from `e` to `describe_exception(e)`:

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
                        describe_exception(e),
                        attempt + 1,
                        max_retries + 1,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise
```

In the final `RateLimitExceededError` raise (around line 556-558) — defensive guard so the 429-heuristic-exhaustion message is never blank (note `last_error` may be `None` on that path):

```python
        raise RateLimitExceededError(
            f"KIS rate limit retries exhausted for {api_name}: "
            f"{describe_exception(last_error) if last_error is not None else 'rate limited'}"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kis_base_rate_limit.py::test_request_error_retry_log_names_the_exception -v`
Expected: PASS

- [ ] **Step 5: Run the file to confirm no regression**

Run: `uv run pytest tests/test_kis_base_rate_limit.py -q`
Expected: PASS (existing TestCalculateRetryDelay / TestParseKisResponse + new test)

- [ ] **Step 6: Commit**

```bash
git add app/services/brokers/kis/base.py tests/test_kis_base_rate_limit.py
git commit -m "fix(ROB-600): KIS retry log + rate-limit-exhausted message name the exception

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QTE5SngqNmNPp8Dx9hdwUU"
```

---

### Task 7: 전체 검증 + lint/typecheck

**Files:** (none — verification only)

- [ ] **Step 1: Run the full set of touched tests**

Run:
```bash
uv run pytest tests/core/test_exceptions.py \
  tests/test_mcp_place_order.py \
  tests/services/brokers/kis/test_account_cash_timeout.py \
  tests/test_portfolio_cash_kis_mock.py \
  tests/test_kis_base_rate_limit.py -q
```
Expected: all PASS.

- [ ] **Step 2: Lint + typecheck (CI gate — app/ AND tests/)**

Run: `uv run ruff format app/ tests/ && uv run ruff check app/ tests/ && uv run ty check app/`
Expected: clean (no format diffs left uncommitted, no lint errors, no type errors). If `ruff format` changes files, re-add and amend the relevant commit.

- [ ] **Step 3: Broader regression sweep (modules importing the touched code)**

Run: `uv run pytest tests/ -q -k "portfolio or order_execution or kis_base or account or capital or cash"`
Expected: PASS. Investigate any shared-DB ordering flakes by re-running the failing test in isolation.

- [ ] **Step 4: Commit any format-only fixups (if Step 2 changed files)**

```bash
git add -A && git commit -m "style(ROB-600): ruff format

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QTE5SngqNmNPp8Dx9hdwUU"
```

---

## Self-Review (작성자 체크)

**Spec coverage:**
- (a) 빈에러→구체사유: Task 2 (order_execution :1128/:1138), Task 4 (portfolio_cash kis :252/:303, +toss/upbit), Task 6 (base.py :544/:557), 헬퍼 Task 1. ✅
- (b) mock read 타임아웃 5→10: Task 3. ✅
- (c) unavailable_sources + placeholder 금지: Task 4 (get_cash_balance_impl + 회귀단언 "no row"), Task 5 (get_available_capital_impl 전파 + _live_kis_orderable raise 가드). ✅
- 제약(주문 재시도 미추가, migration 0, additive 계약): Global Constraints + 어떤 task도 domestic_orders/overseas_orders/스키마 미변경. ✅

**Placeholder scan:** 모든 step에 실제 코드/명령/기대출력 존재. TODO/TBD 없음. ✅

**Type consistency:** `describe_exception(exc: BaseException) -> str` (Task 1) → Task 2/4/6에서 동일 사용. `unavailable_sources: dict[str, str]` (Task 4 생성) → Task 5에서 `cash_result["summary"]["unavailable_sources"]` 동일 키로 소비. `inquire_domestic_cash_balance(is_mock)` timeout 단언(Task 3)이 호출부 시그니처와 일치. ✅
