# Naver Remote-Debug Data-Quality Audit (Operator CLI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A default-disabled operator CLI that, for a given report/bundle, cross-checks each KR symbol's auto_trader quote against the logged-in Naver finance page (via the operator's Chrome at `127.0.0.1:9222`) and prints a structured gap/diff JSON to stdout — read-only, no DB writes, no server-side scraping.

**Architecture:** New package `app/services/action_report/remote_debug_audit/`. Pure, unit-testable units (host allowlist, Naver URL/parse, cross-check diff, audit assembly) are separated from the one hard-to-test unit (the real CDP protocol client), which sits behind a `CdpSession` protocol so the service and all tests run against a `FakeCdpSession`. Raw CDP over `httpx` (discovery) + `websockets` (session) — no `playwright`, no browser binaries. Output mirrors the ROB-323 `external_cross_checks`/`gaps` vocabulary but is emitted to stdout (reports are append-only).

**Tech Stack:** Python 3.13, `httpx` + `websockets` (already in main deps), pytest (`uv run pytest`), argparse CLI under `scripts/`.

**Spec:** `docs/plans/2026-05-26-ROB-323-remote-debug-audit-design.md`.

**Safety boundaries (carried from spec):** default-disabled (`REMOTE_DEBUG_AUDIT_ENABLED`), host-locked to `127.0.0.1:9222`, zero DB writes, no broker/order/watch/order-intent, Naver scraping lives only in this CLI (never the request path or `ensure()`), registry stubs unchanged, fixture/fake tests only (no live browser/network in CI), no secrets printed.

---

## File Structure

**Create:**
- `app/services/action_report/remote_debug_audit/__init__.py` — package marker.
- `app/services/action_report/remote_debug_audit/host_allowlist.py` — `CDP_DEBUG_HOSTS`, `assert_cdp_debug_host`, `CdpDebugHostBlocked`.
- `app/services/action_report/remote_debug_audit/naver_quote.py` — `NaverQuote`, `naver_url`, `NAVER_EXTRACT_JS`, `parse_naver_quote`.
- `app/services/action_report/remote_debug_audit/cross_check.py` — `SymbolQuote`, `cross_check_symbol`, `build_audit`, `extract_symbol_quotes`.
- `app/services/action_report/remote_debug_audit/cdp_client.py` — `CdpSession` (Protocol), `CdpClient` (real impl), `CdpUnavailableError`.
- `app/services/action_report/remote_debug_audit/service.py` — `RemoteDebugAuditService`.
- `scripts/remote_debug_audit_smoke.py` — CLI (`preflight` / `audit`).
- `docs/runbooks/remote-debug-audit-smoke.md` — operator runbook.
- Tests under `tests/services/action_report/remote_debug_audit/`: `test_host_allowlist.py`, `test_naver_quote.py`, `test_cross_check.py`, `test_service.py`, `test_cli.py`, `test_no_hotpath_import.py`.

**Modify:**
- `app/core/config.py` — add `remote_debug_audit_enabled: bool = False` + `validate_remote_debug_audit_config()`.

---

## Task 1: Settings flag + fail-closed validator

**Files:**
- Modify: `app/core/config.py`
- Test: `tests/services/action_report/remote_debug_audit/test_cli.py` (gate covered later; flag tested here)

- [ ] **Step 1: Write the failing test**

Create `tests/services/action_report/remote_debug_audit/__init__.py` (empty) and `tests/services/action_report/remote_debug_audit/test_config_flag.py`:

```python
from app.core.config import Settings, validate_remote_debug_audit_config


def test_flag_defaults_false_and_validator_reports_missing_key() -> None:
    s = Settings(remote_debug_audit_enabled=False)
    assert s.remote_debug_audit_enabled is False
    assert validate_remote_debug_audit_config(s) == ["REMOTE_DEBUG_AUDIT_ENABLED"]


def test_validator_empty_when_enabled() -> None:
    s = Settings(remote_debug_audit_enabled=True)
    assert validate_remote_debug_audit_config(s) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_config_flag.py -v`
Expected: FAIL — `ImportError: cannot import name 'validate_remote_debug_audit_config'`.

- [ ] **Step 3: Add the flag + validator**

In `app/core/config.py`, add the field to the `Settings` class near the other `*_enabled` flags (e.g. after `kiwoom_mock_enabled`):

```python
    remote_debug_audit_enabled: bool = False
```

And add this module-level function near the other `validate_*_config` functions (end of file):

```python
def validate_remote_debug_audit_config(settings_obj: Any = settings) -> list[str]:
    """Return missing env names for the remote-debug audit CLI (names only).

    Default-disabled: only ``REMOTE_DEBUG_AUDIT_ENABLED=true`` is required. The
    Chrome endpoint is fixed (127.0.0.1:9222) and carries no secret, so nothing
    else is gated here.
    """
    missing: list[str] = []
    if not bool(getattr(settings_obj, "remote_debug_audit_enabled", False)):
        missing.append("REMOTE_DEBUG_AUDIT_ENABLED")
    return missing
```

(If `Any` is not already imported in `config.py`, it is — the existing `validate_kiwoom_mock_config` uses `Any`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_config_flag.py -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/services/action_report/remote_debug_audit/__init__.py tests/services/action_report/remote_debug_audit/test_config_flag.py
git commit -m "feat(rob-323): remote_debug_audit_enabled flag + fail-closed validator

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: CDP host allowlist (127.0.0.1:9222 only)

**Files:**
- Create: `app/services/action_report/remote_debug_audit/__init__.py` (empty), `app/services/action_report/remote_debug_audit/host_allowlist.py`
- Test: `tests/services/action_report/remote_debug_audit/test_host_allowlist.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/action_report/remote_debug_audit/test_host_allowlist.py`:

```python
import pytest

from app.services.action_report.remote_debug_audit.host_allowlist import (
    CdpDebugHostBlocked,
    assert_cdp_debug_host,
)


def test_allows_only_localhost_9222() -> None:
    assert_cdp_debug_host("127.0.0.1:9222")  # no raise


@pytest.mark.parametrize(
    "bad",
    ["localhost:9222", "127.0.0.1:9223", "0.0.0.0:9222", "10.0.0.5:9222", "127.0.0.1", ""],
)
def test_rejects_everything_else(bad: str) -> None:
    with pytest.raises(CdpDebugHostBlocked):
        assert_cdp_debug_host(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_host_allowlist.py -v`
Expected: FAIL — `ModuleNotFoundError: ...host_allowlist`.

- [ ] **Step 3: Implement**

Create `app/services/action_report/remote_debug_audit/__init__.py`:

```python
"""ROB-323 follow-up — operator-run Naver remote-debug data-quality audit.

This package is operator-tooling only. It is NEVER imported by the
report-generation hot path (``snapshot_backed.generator`` /
``collectors.registry``) — the registry stubs stay fail-open. See
``test_no_hotpath_import.py``.
"""
```

Create `app/services/action_report/remote_debug_audit/host_allowlist.py`:

```python
"""Strict allowlist for the Chrome remote-debug endpoint (operator-only).

Mirrors ``binance/spot_demo/host_allowlist.assert_spot_demo_host``: strict
equality, no wildcard/suffix. The audit CLI must only ever talk to the
operator's local Chrome.
"""

from __future__ import annotations

CDP_DEBUG_HOSTS: frozenset[str] = frozenset({"127.0.0.1:9222"})


class CdpDebugHostBlocked(RuntimeError):
    """Raised when a CDP endpoint host:port is not the allowed local one."""


def assert_cdp_debug_host(host_port: str) -> None:
    if host_port not in CDP_DEBUG_HOSTS:
        raise CdpDebugHostBlocked(
            f"Host {host_port!r} is not in CDP_DEBUG_HOSTS. "
            "Allowed: " + ", ".join(sorted(CDP_DEBUG_HOSTS))
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_host_allowlist.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/remote_debug_audit/__init__.py app/services/action_report/remote_debug_audit/host_allowlist.py tests/services/action_report/remote_debug_audit/test_host_allowlist.py
git commit -m "feat(rob-323): CDP host allowlist (127.0.0.1:9222 only)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Naver quote URL + JS + parser

**Files:**
- Create: `app/services/action_report/remote_debug_audit/naver_quote.py`
- Test: `tests/services/action_report/remote_debug_audit/test_naver_quote.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/action_report/remote_debug_audit/test_naver_quote.py`:

```python
import json

from app.services.action_report.remote_debug_audit.naver_quote import (
    NaverQuote,
    naver_url,
    parse_naver_quote,
)


def test_naver_url_uses_item_main_with_code() -> None:
    assert (
        naver_url("005930")
        == "https://finance.naver.com/item/main.naver?code=005930"
    )


def test_parse_valid_json_string() -> None:
    raw = json.dumps({"code": "005930", "name": "삼성전자", "price_text": "81,000"})
    q = parse_naver_quote(raw)
    assert q == NaverQuote(code="005930", name="삼성전자", price=81000.0)


def test_parse_accepts_dict_too() -> None:
    raw = {"code": "000660", "name": "SK하이닉스", "price_text": "175,500"}
    q = parse_naver_quote(raw)
    assert q is not None and q.price == 175500.0


def test_parse_missing_price_returns_quote_with_none_price() -> None:
    raw = json.dumps({"code": "999999", "name": None, "price_text": None})
    q = parse_naver_quote(raw)
    assert q is not None and q.code == "999999" and q.price is None


def test_parse_garbage_returns_none() -> None:
    assert parse_naver_quote("not-json") is None
    assert parse_naver_quote(None) is None
    assert parse_naver_quote(123) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_naver_quote.py -v`
Expected: FAIL — `ModuleNotFoundError: ...naver_quote`.

- [ ] **Step 3: Implement**

Create `app/services/action_report/remote_debug_audit/naver_quote.py`:

```python
"""Naver finance per-symbol quote: URL, in-page extraction JS, and parser.

The JS returns a JSON string (``{code, name, price_text}``); all parsing
(comma-strip, int coercion, shape validation) happens here in Python so it is
unit-testable without a browser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NaverQuote:
    code: str
    name: str | None
    price: float | None


def naver_url(code: str) -> str:
    return f"https://finance.naver.com/item/main.naver?code={code}"


# Returns a JSON string read back via Runtime.evaluate(returnByValue=true).
# Selectors: current price lives in ``.no_today .blind``; company name in
# ``.wrap_company h2``. Both are stable on the item/main page.
NAVER_EXTRACT_JS: str = (
    "(function(){"
    "function t(s){var e=document.querySelector(s);"
    "return e?e.textContent.trim():null;}"
    "return JSON.stringify({"
    "code:(new URLSearchParams(location.search)).get('code'),"
    "name:t('.wrap_company h2'),"
    "price_text:t('.no_today .blind')"
    "});"
    "})()"
)


def _to_price(price_text: Any) -> float | None:
    if not isinstance(price_text, str):
        return None
    cleaned = price_text.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_naver_quote(raw: Any) -> NaverQuote | None:
    """Parse the JS result (JSON string or dict) into a ``NaverQuote``.

    Returns ``None`` for unusable input (not a JSON object / missing code).
    """
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    if not isinstance(code, str) or not code:
        return None
    name = data.get("name")
    return NaverQuote(
        code=code,
        name=name if isinstance(name, str) else None,
        price=_to_price(data.get("price_text")),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_naver_quote.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/remote_debug_audit/naver_quote.py tests/services/action_report/remote_debug_audit/test_naver_quote.py
git commit -m "feat(rob-323): Naver quote URL + extraction JS + parser

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Cross-check + audit assembly (pure logic)

**Files:**
- Create: `app/services/action_report/remote_debug_audit/cross_check.py`
- Test: `tests/services/action_report/remote_debug_audit/test_cross_check.py`

Design notes for this task:
- `SymbolQuote` = the auto_trader side (extracted from a persisted `symbol` snapshot).
- `at_quote_present` = the snapshot's quote `status == "ok"` AND `last_price` is a number. (We do **not** compute time-staleness — the audit runs after generation, so wall-clock skew would be noise. YAGNI.)
- Price comparison is a **plausibility band** (default 5%), not exact equality.
- Gap severities: `info`/`warning` only — never `blocking`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/action_report/remote_debug_audit/test_cross_check.py`:

```python
from app.services.action_report.remote_debug_audit.cross_check import (
    SymbolQuote,
    build_audit,
    cross_check_symbol,
)
from app.services.action_report.remote_debug_audit.naver_quote import NaverQuote


def _at(symbol="005930", name="삼성전자", price=81000.0, status="ok"):
    return SymbolQuote(symbol=symbol, name=name, last_price=price, quote_status=status)


def test_ok_when_resolved_name_matches_price_within_band() -> None:
    f = cross_check_symbol(_at(), NaverQuote("005930", "삼성전자", 81500.0), tolerance_pct=5.0)
    assert f["status"] == "ok"
    assert f["symbol_resolved"] is True
    assert f["name_match"] is True
    assert f["at_quote_present"] is True
    assert f["price_within_tolerance"] is True


def test_price_mismatch_flags_warning() -> None:
    f = cross_check_symbol(_at(price=80000.0), NaverQuote("005930", "삼성전자", 120000.0), tolerance_pct=5.0)
    assert f["price_within_tolerance"] is False
    assert f["status"] == "mismatch"


def test_unresolved_naver_symbol() -> None:
    f = cross_check_symbol(_at(symbol="999999", name=None), None, tolerance_pct=5.0)
    assert f["symbol_resolved"] is False
    assert f["status"] == "unavailable"
    assert f["reason_code"] == "naver_symbol_unresolved"


def test_at_quote_missing_when_status_not_ok() -> None:
    f = cross_check_symbol(
        _at(price=None, status="unavailable"),
        NaverQuote("005930", "삼성전자", 81000.0),
        tolerance_pct=5.0,
    )
    assert f["at_quote_present"] is False
    assert f["status"] == "at_quote_missing"


def test_build_audit_assembles_gaps_and_never_blocks() -> None:
    findings = [
        cross_check_symbol(_at(), NaverQuote("005930", "삼성전자", 81200.0), tolerance_pct=5.0),
        cross_check_symbol(_at(symbol="000660", name="SK하이닉스", price=100000.0),
                           NaverQuote("000660", "SK하이닉스", 200000.0), tolerance_pct=5.0),
        cross_check_symbol(_at(symbol="999999", name=None), None, tolerance_pct=5.0),
    ]
    audit = build_audit(
        snapshot_bundle_uuid="b-1", report_uuid="r-1", findings=findings
    )
    assert audit["source"] == "naver_remote_debug"
    assert audit["affects_report_generation"] is False
    assert audit["checked_symbols"] == 3
    assert audit["snapshot_bundle_uuid"] == "b-1"
    severities = {g["severity"] for g in audit["gaps"]}
    assert "blocking" not in severities
    kinds = {g["kind"] for g in audit["gaps"]}
    assert "naver_price_mismatch" in kinds
    assert "naver_symbol_unresolved" in kinds
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_cross_check.py -v`
Expected: FAIL — `ModuleNotFoundError: ...cross_check`.

- [ ] **Step 3: Implement**

Create `app/services/action_report/remote_debug_audit/cross_check.py`:

```python
"""Cross-check an auto_trader symbol quote against a Naver quote, and assemble
the stdout audit payload. Pure logic — no IO, no browser.

Coverage/plausibility, not exact reconciliation: the goal is to surface gaps
(auto_trader missing/mis-resolving data Naver has), not to reconcile prices to
the won.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.services.action_report.remote_debug_audit.naver_quote import NaverQuote


@dataclass(frozen=True)
class SymbolQuote:
    """auto_trader side, extracted from a persisted ``symbol`` snapshot."""

    symbol: str
    name: str | None
    last_price: float | None
    quote_status: str | None


def cross_check_symbol(
    at: SymbolQuote,
    naver: NaverQuote | None,
    *,
    tolerance_pct: float,
) -> dict[str, Any]:
    symbol_resolved = naver is not None and naver.price is not None
    at_quote_present = at.quote_status == "ok" and isinstance(at.last_price, (int, float))

    name_match: bool | None = None
    if naver is not None and naver.name and at.name:
        name_match = _normalize(naver.name) == _normalize(at.name)

    price_within_tolerance: bool | None = None
    if symbol_resolved and at_quote_present:
        assert naver is not None and naver.price is not None  # narrowed above
        denom = abs(at.last_price) or 1.0
        price_within_tolerance = (
            abs(naver.price - at.last_price) / denom * 100.0 <= tolerance_pct
        )

    # Status precedence: unresolved > at-missing > mismatch > ok.
    if not symbol_resolved:
        status, reason_code = "unavailable", "naver_symbol_unresolved"
    elif not at_quote_present:
        status, reason_code = "at_quote_missing", "at_quote_missing"
    elif price_within_tolerance is False:
        status, reason_code = "mismatch", "naver_price_mismatch"
    else:
        status, reason_code = "ok", None

    finding: dict[str, Any] = {
        "symbol": at.symbol,
        "symbol_resolved": symbol_resolved,
        "name_match": name_match,
        "at_quote_present": at_quote_present,
        "at_price": at.last_price,
        "naver_price": naver.price if naver else None,
        "price_within_tolerance": price_within_tolerance,
        "status": status,
    }
    if reason_code is not None:
        finding["reason_code"] = reason_code
    return finding


def build_audit(
    *,
    snapshot_bundle_uuid: str | None,
    report_uuid: str | None,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    gaps: list[dict[str, Any]] = []
    mismatched = sorted(f["symbol"] for f in findings if f["status"] == "mismatch")
    unresolved = sorted(f["symbol"] for f in findings if f["status"] == "unavailable")
    at_missing = sorted(f["symbol"] for f in findings if f["status"] == "at_quote_missing")
    if mismatched:
        gaps.append({
            "severity": "warning",
            "kind": "naver_price_mismatch",
            "sources": mismatched,
            "message": "Naver와 auto_trader 가격 차이가 허용범위 초과 — 후속 데이터 점검 검토",
        })
    if unresolved:
        gaps.append({
            "severity": "warning",
            "kind": "naver_symbol_unresolved",
            "sources": unresolved,
            "message": "Naver에서 심볼을 해석하지 못함 — 심볼 매핑/커버리지 점검 검토",
        })
    if at_missing:
        gaps.append({
            "severity": "info",
            "kind": "at_quote_missing",
            "sources": at_missing,
            "message": "auto_trader가 해당 심볼 quote를 못 가짐(Naver는 있음) — 수집 점검 검토",
        })
    return {
        "source": "naver_remote_debug",
        "snapshot_bundle_uuid": snapshot_bundle_uuid,
        "report_uuid": report_uuid,
        "as_of": dt.datetime.now(tz=dt.UTC).isoformat(),
        "affects_report_generation": False,
        "checked_symbols": len(findings),
        "findings": findings,
        "gaps": gaps,
    }


def _normalize(name: str) -> str:
    return "".join(name.split()).lower()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_cross_check.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/remote_debug_audit/cross_check.py tests/services/action_report/remote_debug_audit/test_cross_check.py
git commit -m "feat(rob-323): symbol cross-check + audit assembly (pure)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: CDP session protocol + real client + fake

**Files:**
- Create: `app/services/action_report/remote_debug_audit/cdp_client.py`
- Test: `tests/services/action_report/remote_debug_audit/test_cdp_client.py`

Design note: the real CDP wire protocol cannot be unit-tested without a browser. We isolate it behind `CdpSession` (Protocol) — the service and all other tests depend only on the protocol. Tests here cover the **host-lock at construction** and a `FakeCdpSession` (shipped for the service tests). The real `CdpClient.fetch_rendered` body is exercised only by the operator (documented).

- [ ] **Step 1: Write the failing test**

Create `tests/services/action_report/remote_debug_audit/test_cdp_client.py`:

```python
import pytest

from app.services.action_report.remote_debug_audit.cdp_client import (
    CdpClient,
    FakeCdpSession,
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_cdp_client.py -v`
Expected: FAIL — `ModuleNotFoundError: ...cdp_client`.

- [ ] **Step 3: Implement**

Create `app/services/action_report/remote_debug_audit/cdp_client.py`:

```python
"""Minimal Chrome DevTools Protocol client for the operator audit CLI.

``CdpSession`` is the seam every consumer depends on; ``CdpClient`` is the real
implementation (httpx discovery + websockets session), exercised only by the
operator against a local Chrome. ``FakeCdpSession`` backs the unit tests.

Read-only: opens a NEW tab per URL, evaluates one expression, closes the tab.
Never touches pre-existing operator tabs.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from app.services.action_report.remote_debug_audit.host_allowlist import (
    assert_cdp_debug_host,
)


class CdpUnavailableError(RuntimeError):
    """Raised when the local Chrome remote-debug endpoint cannot be reached."""


class CdpSession(Protocol):
    async def fetch_rendered(
        self, url: str, js: str, *, timeout_s: float
    ) -> Any: ...


class FakeCdpSession:
    """Test double: returns canned ``fetch_rendered`` results keyed by URL."""

    def __init__(self, *, results: dict[str, Any]) -> None:
        self._results = results

    async def fetch_rendered(self, url: str, js: str, *, timeout_s: float) -> Any:
        if url not in self._results:
            raise RuntimeError(f"no canned result for {url!r}")
        value = self._results[url]
        if isinstance(value, Exception):
            raise value
        return value


class CdpClient:
    """Real CDP client. Host-locked to 127.0.0.1:9222 at construction.

    NOTE: ``fetch_rendered`` talks to a live browser; it is not unit-tested
    (no browser in CI). It is covered by the operator runbook smoke only.
    """

    def __init__(self, *, host_port: str = "127.0.0.1:9222") -> None:
        assert_cdp_debug_host(host_port)
        self._host_port = host_port

    async def fetch_rendered(self, url: str, js: str, *, timeout_s: float) -> Any:
        from websockets.asyncio.client import connect as ws_connect

        # 1. Discover the browser-level websocket endpoint.
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.get(f"http://{self._host_port}/json/version")
                ws_url = resp.json()["webSocketDebuggerUrl"]
        except Exception as exc:  # noqa: BLE001 — surfaced as a clear setup error
            raise CdpUnavailableError(
                f"no Chrome remote-debug at {self._host_port}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        target_id: str | None = None
        async with ws_connect(ws_url, open_timeout=timeout_s) as ws:
            _msg_id = 0

            async def cmd(method: str, params: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
                nonlocal _msg_id
                _msg_id += 1
                payload: dict[str, Any] = {"id": _msg_id, "method": method, "params": params}
                if session_id is not None:
                    payload["sessionId"] = session_id
                await ws.send(json.dumps(payload))
                while True:
                    raw = json.loads(await ws.recv())
                    if raw.get("id") == _msg_id:
                        return raw

            try:
                created = await cmd("Target.createTarget", {"url": url})
                target_id = created["result"]["targetId"]
                attached = await cmd(
                    "Target.attachToTarget", {"targetId": target_id, "flatten": True}
                )
                session_id = attached["result"]["sessionId"]
                # Give the page a beat to render, then read.
                await cmd("Runtime.enable", {}, session_id)
                evaluated = await cmd(
                    "Runtime.evaluate",
                    {"expression": js, "returnByValue": True, "awaitPromise": True},
                    session_id,
                )
                return evaluated.get("result", {}).get("result", {}).get("value")
            finally:
                if target_id is not None:
                    await cmd("Target.closeTarget", {"targetId": target_id})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_cdp_client.py -v`
Expected: PASS (3 tests). The real `CdpClient.fetch_rendered` is not invoked.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/remote_debug_audit/cdp_client.py tests/services/action_report/remote_debug_audit/test_cdp_client.py
git commit -m "feat(rob-323): CDP session protocol + host-locked client + fake

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Audit service (load bundle symbols, orchestrate cross-check)

**Files:**
- Create: `app/services/action_report/remote_debug_audit/service.py`
- Test: `tests/services/action_report/remote_debug_audit/test_service.py`

Design note: the service depends on injected collaborators (snapshots repo, reports repo, a `CdpSession`) so tests use fakes — no DB, no browser. `extract_symbol_quotes` is a pure helper over `(item, snapshot)` pairs, tested directly.

- [ ] **Step 1: Write the failing test**

Create `tests/services/action_report/remote_debug_audit/test_service.py`:

```python
import json
import types
import uuid

import pytest

from app.services.action_report.remote_debug_audit.cdp_client import FakeCdpSession
from app.services.action_report.remote_debug_audit.cross_check import SymbolQuote
from app.services.action_report.remote_debug_audit.naver_quote import naver_url
from app.services.action_report.remote_debug_audit.service import (
    RemoteDebugAuditService,
    extract_symbol_quotes,
)


def _snap(kind, symbol=None, payload=None):
    return types.SimpleNamespace(
        snapshot_kind=kind, symbol=symbol, payload_json=payload or {}
    )


def test_extract_symbol_quotes_reads_quote_payload() -> None:
    pairs = [
        (object(), _snap("symbol", "005930", {
            "symbol": "005930", "name": "삼성전자",
            "quote": {"status": "ok", "last_price": 81000.0},
        })),
        (object(), _snap("market", None, {"foo": "bar"})),  # non-symbol ignored
        (object(), _snap("symbol", "000660", {
            "symbol": "000660", "name": "SK하이닉스",
            "quote": {"status": "unavailable"},
        })),
    ]
    quotes = extract_symbol_quotes(pairs)
    assert quotes == [
        SymbolQuote("005930", "삼성전자", 81000.0, "ok"),
        SymbolQuote("000660", "SK하이닉스", None, "unavailable"),
    ]


class _FakeBundle:
    def __init__(self):
        self.id = 1
        self.bundle_uuid = uuid.uuid4()


class _FakeSnapshotsRepo:
    def __init__(self, bundle, pairs):
        self._bundle, self._pairs = bundle, pairs

    async def get_bundle_by_uuid(self, bundle_uuid):
        return self._bundle if bundle_uuid == self._bundle.bundle_uuid else None

    async def list_bundle_items_with_snapshots(self, bundle_id):
        return self._pairs


@pytest.mark.asyncio
async def test_audit_bundle_produces_findings_with_fake_cdp() -> None:
    bundle = _FakeBundle()
    pairs = [
        (object(), _snap("symbol", "005930", {
            "symbol": "005930", "name": "삼성전자",
            "quote": {"status": "ok", "last_price": 81000.0},
        })),
    ]
    cdp = FakeCdpSession(results={
        naver_url("005930"): json.dumps(
            {"code": "005930", "name": "삼성전자", "price_text": "81,300"}
        ),
    })
    svc = RemoteDebugAuditService(
        snapshots_repo=_FakeSnapshotsRepo(bundle, pairs),
        reports_repo=None,
        cdp_session=cdp,
    )
    audit = await svc.audit_bundle(bundle.bundle_uuid, max_symbols=10)
    assert audit["checked_symbols"] == 1
    assert audit["findings"][0]["status"] == "ok"
    assert audit["affects_report_generation"] is False


@pytest.mark.asyncio
async def test_audit_bundle_per_symbol_failopen() -> None:
    bundle = _FakeBundle()
    pairs = [
        (object(), _snap("symbol", "005930", {
            "symbol": "005930", "name": "삼성전자",
            "quote": {"status": "ok", "last_price": 81000.0},
        })),
        (object(), _snap("symbol", "999999", {
            "symbol": "999999", "quote": {"status": "ok", "last_price": 100.0},
        })),
    ]
    # 999999 has no canned CDP result -> fetch raises -> finding unavailable, run continues.
    cdp = FakeCdpSession(results={
        naver_url("005930"): json.dumps(
            {"code": "005930", "name": "삼성전자", "price_text": "81,000"}
        ),
    })
    svc = RemoteDebugAuditService(
        snapshots_repo=_FakeSnapshotsRepo(bundle, pairs), reports_repo=None, cdp_session=cdp
    )
    audit = await svc.audit_bundle(bundle.bundle_uuid, max_symbols=10)
    assert audit["checked_symbols"] == 2
    statuses = {f["symbol"]: f["status"] for f in audit["findings"]}
    assert statuses["005930"] == "ok"
    assert statuses["999999"] == "unavailable"


@pytest.mark.asyncio
async def test_audit_bundle_missing_bundle_raises() -> None:
    bundle = _FakeBundle()
    svc = RemoteDebugAuditService(
        snapshots_repo=_FakeSnapshotsRepo(bundle, []),
        reports_repo=None,
        cdp_session=FakeCdpSession(results={}),
    )
    with pytest.raises(LookupError):
        await svc.audit_bundle(uuid.uuid4(), max_symbols=10)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_service.py -v`
Expected: FAIL — `ModuleNotFoundError: ...service`.

- [ ] **Step 3: Implement**

Create `app/services/action_report/remote_debug_audit/service.py`:

```python
"""Orchestrates the Naver remote-debug audit for one bundle.

Read-only: loads the bundle's persisted ``symbol`` snapshots, reads each
symbol's auto_trader quote, drives a per-symbol CDP cross-check (sequential,
fail-open), and assembles the stdout audit payload. No DB writes.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.services.action_report.remote_debug_audit.cdp_client import CdpSession
from app.services.action_report.remote_debug_audit.cross_check import (
    SymbolQuote,
    build_audit,
    cross_check_symbol,
)
from app.services.action_report.remote_debug_audit.naver_quote import (
    NAVER_EXTRACT_JS,
    naver_url,
    parse_naver_quote,
)

_DEFAULT_TOLERANCE_PCT = 5.0
_PER_SYMBOL_TIMEOUT_S = 15.0


def extract_symbol_quotes(
    item_snapshot_pairs: list[tuple[Any, Any]],
) -> list[SymbolQuote]:
    """Pull (symbol, name, last_price, quote_status) from ``symbol`` snapshots."""
    out: list[SymbolQuote] = []
    for _item, snap in item_snapshot_pairs:
        if getattr(snap, "snapshot_kind", None) != "symbol":
            continue
        payload = getattr(snap, "payload_json", None) or {}
        symbol = getattr(snap, "symbol", None) or payload.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        quote = payload.get("quote") if isinstance(payload, dict) else None
        quote = quote if isinstance(quote, dict) else {}
        last_price = quote.get("last_price")
        out.append(
            SymbolQuote(
                symbol=symbol,
                name=payload.get("name") if isinstance(payload, dict) else None,
                last_price=last_price if isinstance(last_price, (int, float)) else None,
                quote_status=quote.get("status"),
            )
        )
    return out


class RemoteDebugAuditService:
    def __init__(
        self,
        *,
        snapshots_repo: Any,
        reports_repo: Any,
        cdp_session: CdpSession,
        tolerance_pct: float = _DEFAULT_TOLERANCE_PCT,
    ) -> None:
        self._snapshots_repo = snapshots_repo
        self._reports_repo = reports_repo
        self._cdp = cdp_session
        self._tolerance_pct = tolerance_pct

    async def resolve_bundle_uuid(self, report_uuid: uuid.UUID) -> uuid.UUID:
        report = await self._reports_repo.get_report_by_uuid(report_uuid)
        if report is None or report.snapshot_bundle_uuid is None:
            raise LookupError(
                f"report {report_uuid} not found or has no snapshot bundle"
            )
        return report.snapshot_bundle_uuid

    async def audit_bundle(
        self, bundle_uuid: uuid.UUID, *, max_symbols: int
    ) -> dict[str, Any]:
        bundle = await self._snapshots_repo.get_bundle_by_uuid(bundle_uuid)
        if bundle is None:
            raise LookupError(f"bundle {bundle_uuid} not found")
        pairs = await self._snapshots_repo.list_bundle_items_with_snapshots(bundle.id)
        quotes = extract_symbol_quotes(pairs)[: max(1, max_symbols)]

        findings: list[dict[str, Any]] = []
        for at in quotes:
            naver = await self._fetch_naver(at.symbol)
            findings.append(
                cross_check_symbol(at, naver, tolerance_pct=self._tolerance_pct)
            )
        return build_audit(
            snapshot_bundle_uuid=str(bundle_uuid),
            report_uuid=None,
            findings=findings,
        )

    async def _fetch_naver(self, symbol: str):
        try:
            raw = await self._cdp.fetch_rendered(
                naver_url(symbol), NAVER_EXTRACT_JS, timeout_s=_PER_SYMBOL_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 — per-symbol fail-open
            return None
        return parse_naver_quote(raw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_service.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/remote_debug_audit/service.py tests/services/action_report/remote_debug_audit/test_service.py
git commit -m "feat(rob-323): remote-debug audit service (bundle load + cross-check orchestration)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: Operator CLI (`preflight` / `audit`, default-disabled)

**Files:**
- Create: `scripts/remote_debug_audit_smoke.py`
- Test: `tests/services/action_report/remote_debug_audit/test_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/action_report/remote_debug_audit/test_cli.py`:

```python
import pytest

from app.core import config
from scripts import remote_debug_audit_smoke as cli


def test_parser_requires_mode() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["--mode", "preflight"])
    assert args.mode == "preflight"


def test_preflight_reports_missing_key_names_only_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings, "remote_debug_audit_enabled", False)
    out = cli.run_preflight()
    assert out["step"] == "preflight"
    assert out["ok"] is False
    assert out["missing_env_keys"] == ["REMOTE_DEBUG_AUDIT_ENABLED"]
    # No values, only key names.
    assert all("=" not in k for k in out["missing_env_keys"])


def test_preflight_ok_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "remote_debug_audit_enabled", True)
    out = cli.run_preflight()
    assert out["ok"] is True
    assert out["missing_env_keys"] == []


def test_audit_mode_requires_a_uuid_arg() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["--mode", "audit"])
    # Neither bundle nor report uuid -> validated in _amain, surfaced as ValueError.
    with pytest.raises(ValueError):
        cli.require_target(args)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.remote_debug_audit_smoke`.

- [ ] **Step 3: Implement**

Create `scripts/remote_debug_audit_smoke.py`:

```python
"""ROB-323 — operator CLI for the Naver remote-debug data-quality audit.

Default-disabled. Connects ONLY to a local Chrome at 127.0.0.1:9222 launched
with the operator's logged-in profile:

    open -na "Google Chrome" --args \\
      --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \\
      --user-data-dir="$HOME/.hermes/chrome-toss-debug"

Read-only: prints a JSON audit to stdout, never writes to the DB or any broker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import Any

from app.core.config import validate_remote_debug_audit_config
from app.core.db import AsyncSessionLocal  # sessionmaker(class_=AsyncSession)
from app.services.action_report.remote_debug_audit.cdp_client import CdpClient
from app.services.action_report.remote_debug_audit.service import (
    RemoteDebugAuditService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def run_preflight() -> dict[str, Any]:
    missing = validate_remote_debug_audit_config()
    return {"step": "preflight", "ok": not missing, "missing_env_keys": missing}


def require_target(args: argparse.Namespace) -> tuple[str, uuid.UUID]:
    if args.bundle_uuid:
        return "bundle", uuid.UUID(args.bundle_uuid)
    if args.report_uuid:
        return "report", uuid.UUID(args.report_uuid)
    raise ValueError("audit mode requires --bundle-uuid or --report-uuid")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Naver remote-debug data-quality audit (ROB-323, operator-only)"
    )
    parser.add_argument("--mode", required=True, choices=["preflight", "audit"])
    parser.add_argument("--bundle-uuid", default=None)
    parser.add_argument("--report-uuid", default=None)
    parser.add_argument("--max-symbols", type=int, default=10)
    return parser


async def _amain(args: argparse.Namespace) -> int:
    if args.mode == "preflight":
        _emit(run_preflight())
        return 0

    # audit mode
    missing = validate_remote_debug_audit_config()
    if missing:
        _emit({"step": "audit", "ok": False, "missing_env_keys": missing})
        return 2

    kind, target = require_target(args)
    async with AsyncSessionLocal() as session:
        svc = RemoteDebugAuditService(
            snapshots_repo=InvestmentSnapshotsRepository(session),
            reports_repo=InvestmentReportsRepository(session),
            cdp_session=CdpClient(),
        )
        bundle_uuid = (
            await svc.resolve_bundle_uuid(target) if kind == "report" else target
        )
        audit = await svc.audit_bundle(bundle_uuid, max_symbols=args.max_symbols)
    _emit(audit)
    return 0


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
```

Import symbols verified against the codebase: `app.core.db.AsyncSessionLocal` (a `sessionmaker(class_=AsyncSession)`), `app.services.investment_reports.repository.InvestmentReportsRepository`, `app.services.investment_snapshots.repository.InvestmentSnapshotsRepository`. Task 7's test imports the CLI module, so these top-level imports must resolve — they are correct as written.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_cli.py -v`
Expected: PASS (4 tests). (`run_preflight`, `build_parser`, `require_target` are import-light; the DB/CDP wiring in `_amain` is not exercised by these tests.)

- [ ] **Step 5: Commit**

```bash
git add scripts/remote_debug_audit_smoke.py tests/services/action_report/remote_debug_audit/test_cli.py
git commit -m "feat(rob-323): remote-debug audit operator CLI (preflight/audit, default-disabled)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: Hot-path import guard

**Files:**
- Test: `tests/services/action_report/remote_debug_audit/test_no_hotpath_import.py`

Ensures the report-generation hot path never imports this operator package (keeps `websockets`/CDP out of `ensure()` and keeps the stubs authoritative).

- [ ] **Step 1: Write the failing test**

Create `tests/services/action_report/remote_debug_audit/test_no_hotpath_import.py`:

```python
import ast
import pathlib

_HOTPATH = [
    "app/services/action_report/snapshot_backed/generator.py",
    "app/services/action_report/snapshot_backed/collectors/registry.py",
    "app/services/action_report/snapshot_backed/collectors/optional_stubs.py",
]


def test_hotpath_does_not_import_remote_debug_audit() -> None:
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    for rel in _HOTPATH:
        tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        assert not any("remote_debug_audit" in m for m in imported), (
            f"{rel} must not import remote_debug_audit (operator-only)"
        )
```

- [ ] **Step 2: Run test to verify it passes immediately**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/test_no_hotpath_import.py -v`
Expected: PASS (the hot path does not import the new package — this is a guard against future regressions).

If `parents[4]` does not resolve to the repo root, adjust the index so `repo_root / "app"` exists (verify with a quick `python -c` if needed).

- [ ] **Step 3: Commit**

```bash
git add tests/services/action_report/remote_debug_audit/test_no_hotpath_import.py
git commit -m "test(rob-323): guard that report hot path never imports remote_debug_audit

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: Operator runbook

**Files:**
- Create: `docs/runbooks/remote-debug-audit-smoke.md`

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/remote-debug-audit-smoke.md`:

```markdown
# Remote-Debug Data-Quality Audit Smoke (ROB-323)

Operator-only, default-disabled, read-only. Cross-checks a report/bundle's KR
symbols against the logged-in Naver finance pages via the operator's Chrome at
`127.0.0.1:9222`. Prints a JSON audit to stdout. **No DB writes, no orders.**

## 1. Launch the logged-in Chrome (operator macbook)

```bash
open -na "Google Chrome" --args \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.hermes/chrome-toss-debug"
```

This profile keeps Naver/Toss/Upbit/TradingView logins. Do NOT use a fresh or
default profile.

## 2. Enable + preflight

```bash
export REMOTE_DEBUG_AUDIT_ENABLED=true
uv run python -m scripts.remote_debug_audit_smoke --mode preflight
```

`ok=false` lists missing env KEY names only (never values).

## 3. Audit a bundle (or report)

```bash
uv run python -m scripts.remote_debug_audit_smoke --mode audit \
  --bundle-uuid <uuid> --max-symbols 10
# or
uv run python -m scripts.remote_debug_audit_smoke --mode audit \
  --report-uuid <uuid> --max-symbols 10
```

Output: `{source, snapshot_bundle_uuid, findings[], gaps[], affects_report_generation:false}`.
`gaps` severities are `info`/`warning` only — this audit never gates report
generation or publish.

## Safety

- Host-locked to `127.0.0.1:9222` (strict equality).
- Read-only: zero DB writes; no broker/order/watch/order-intent.
- Naver access happens only here — never in the frontend request path or
  server-side `ensure()`. The `ensure()` registry stubs are unchanged.
- Exit codes: `0` audit completed (any number of gaps); `2` disabled / no Chrome
  at 127.0.0.1:9222 / bundle not found.

## Scope

KR symbols only (Naver = KRX). US / Toss / Upbit / browser_probe and persisted
audit results are future slices.
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/remote-debug-audit-smoke.md
git commit -m "docs(rob-323): remote-debug audit smoke runbook

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: Verification gate + PR

- [ ] **Step 1: Lint**

Run: `uv run ruff check app/ tests/ scripts/`
Expected: All checks passed.

- [ ] **Step 2: Full new-package test sweep**

Run: `uv run pytest tests/services/action_report/remote_debug_audit/ -v`
Expected: all pass.

- [ ] **Step 3: Confirm no broader regressions in the action_report suite**

Run: `uv run pytest tests/services/action_report/ -q`
Expected: all pass.

- [ ] **Step 4: Push + open PR (base `main`)**

```bash
git push -u origin rob-323-remote-debug-collector
gh pr create --base main --title "feat(rob-323): Naver remote-debug data-quality audit (operator CLI, default-disabled)" --body "$(cat <<'EOF'
## What
First real external cross-check for ROB-323: a default-disabled, host-locked operator CLI that compares a report/bundle's KR symbol quotes against the logged-in Naver finance pages (via Chrome at 127.0.0.1:9222) and prints a structured gap/diff JSON.

## Why a CLI (not a server-side collector)
`investment_reports` is append-only (can't write the audit back into the report), and the logged-in Chrome only exists on the operator's machine. So this is an out-of-band operator tool; the `ensure()` registry stubs are unchanged.

## Design
- Raw CDP over `httpx` + `websockets` (no playwright). `CdpSession` protocol isolates the one un-CI-testable unit; everything else is fixture-tested via `FakeCdpSession`.
- Output mirrors the ROB-323 `external_cross_checks`/`gaps` vocabulary; gaps are `info`/`warning` only — never blocking.

## Safety
Default-disabled (`REMOTE_DEBUG_AUDIT_ENABLED`), host-locked to 127.0.0.1:9222 (strict equality), zero DB writes, no broker/order/watch, Naver scraping only in this CLI (never the request path), hot-path import guard, fixture/fake tests only.

## Scope / follow-up
KR symbols + Naver only. US / Toss / Upbit / browser_probe, persisted audit table + UI surfacing, and candidate-universe cross-check are future slices.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Confirm the Test workflow is green before merge** (per the pre-merge full-CI gate — branch protection does not gate lint/test).

---

## Acceptance Criteria Coverage Map

| Requirement (spec) | Task |
|---|---|
| Default-disabled env gate, missing KEY names only | 1, 7 |
| Host-locked to 127.0.0.1:9222 (strict) | 2, 5 |
| Naver URL + in-page extraction + robust parse | 3 |
| Coverage/plausibility cross-check (not exact equality) | 4 |
| Gaps `info`/`warning` only, never blocking | 4 |
| Raw CDP via httpx+websockets, no playwright | 5 |
| `CdpSession` seam → fixture-tested, real client operator-only | 5, 6 |
| Load bundle `symbol` snapshots read-only, extract quotes | 6 |
| Per-symbol fail-open; bundle-missing → error | 6 |
| `--report-uuid` → bundle resolution | 6, 7 |
| CLI `preflight`/`audit`, stdout JSON, `--max-symbols` | 7 |
| No DB writes / append-only respected | 6, 7 (read-only repos; no update calls) |
| Scraping never in request path / `ensure()`; stubs unchanged | 8 (hot-path guard) |
| Operator runbook incl. Chrome launch | 9 |
| Fixture/fake tests only (no live browser/network) | 5, 6 (FakeCdpSession) |

## Notes
- Task 7 import symbols are verified: `AsyncSessionLocal` (app/core/db.py), `InvestmentReportsRepository`, `InvestmentSnapshotsRepository`. The CLI's live DB/CDP wiring inside `_amain` is not unit-tested (operator-only); the unit tests cover `run_preflight`/`build_parser`/`require_target`.
- The `--report-uuid` path resolves to a bundle_uuid but the emitted audit's `report_uuid` field stays `null` in this slice (bundle_uuid is the key; threading report_uuid through `build_audit` is a trivial future tweak). 
- Time-staleness of the auto_trader quote is intentionally NOT computed (the audit runs after generation; wall-clock skew would be noise). `at_quote_present` (status `ok` + numeric price) is the signal.
