# US Dual-Paper Premarket Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a default-disabled, read-only premarket path that produces a dual-broker (KIS mock US + Alpaca Paper) preview/preflight packet for 1–3 US symbols, with each broker reported independently as `previewed|blocked|unsupported|error` and no submit path reachable.

**Architecture:** Thin orchestrator (`app/services/us_dual_paper/packet.py`) over two per-broker adapters implementing a common `BrokerPreviewAdapter` protocol. The Alpaca adapter wraps the existing side-effect-free `alpaca_paper_preview_order` + `AlpacaPaperBrokerService` reads; the KIS mock adapter is a new pure gate reading the `kis` singleton facade with `is_mock=True` pinned. A capability matrix and a default-disabled smoke CLI + read-only MCP tools surface the result. The live `build_kis_us_account_snapshot` builder is **not** modified.

**Tech Stack:** Python 3.13, Pydantic v2, FastMCP, pytest (async), argparse CLI, `uv` runner.

**Spec:** `docs/superpowers/specs/2026-05-26-rob-326-us-dual-paper-premarket-design.md`

---

## Conventions for every task

- Run tests with: `uv run pytest <path> -v`
- Lint before each commit: `uv run ruff check app/ tests/ scripts/`
- Commit trailer: `Co-Authored-By: Paperclip <noreply@paperclip.ing>`
- Canonical `account_scope` tokens only: `kis_mock`, `alpaca_paper`. **Never** `kis_mock_us`. `market="us"` is a packet field.
- No module under `app/services/us_dual_paper/` may import a broker order/submit/cancel/modify module (enforced by Task 9).

---

## File structure (locked)

**New:**
- `app/schemas/us_dual_paper.py` — packet + result + request schemas
- `app/services/us_dual_paper/__init__.py`
- `app/services/us_dual_paper/capability_matrix.py`
- `app/services/us_dual_paper/adapters/__init__.py`
- `app/services/us_dual_paper/adapters/base.py` — protocol + shared exceptions
- `app/services/us_dual_paper/adapters/alpaca.py`
- `app/services/us_dual_paper/adapters/kis_mock.py`
- `app/services/us_dual_paper/packet.py` — orchestrator (PR2)
- `app/mcp_server/tooling/us_dual_paper.py` — read-only MCP tools
- `scripts/smoke/us_dual_paper_preview_smoke.py`
- `docs/runbooks/us-dual-paper-premarket-preview.md` (PR2)
- `tests/services/us_dual_paper/test_*.py`

**Modified (additive only):**
- `app/core/config.py` — add `us_dual_paper_preview_enabled` field
- `app/mcp_server/tooling/registry.py` — register new tools

**Reused read-only (NOT modified):**
- `app/services/brokers/kis/client.py` (`kis` singleton: `inquire_overseas_margin`, `fetch_my_us_stocks`)
- `app/services/brokers/alpaca/service.py` (`AlpacaPaperBrokerService.get_cash/list_positions`)
- `app/mcp_server/tooling/alpaca_paper_preview.py` (`alpaca_paper_preview_order`)

---

# PR1 — Matrix, schemas, read-only account adapters, preflight CLI

## Task 1: Add `us_dual_paper_preview_enabled` config flag

**Files:**
- Modify: `app/core/config.py` (near the Alpaca paper fields ~line 508-510)
- Test: `tests/services/us_dual_paper/test_config_flag.py`

- [ ] **Step 1: Create the test package + failing test**

Create `tests/services/us_dual_paper/__init__.py` (empty) and `tests/services/us_dual_paper/test_config_flag.py`:

```python
import pytest

from app.core.config import Settings


@pytest.mark.unit
def test_us_dual_paper_preview_disabled_by_default():
    s = Settings(_env_file=None)
    assert s.us_dual_paper_preview_enabled is False


@pytest.mark.unit
def test_us_dual_paper_preview_enabled_from_env(monkeypatch):
    monkeypatch.setenv("US_DUAL_PAPER_PREVIEW_ENABLED", "true")
    s = Settings(_env_file=None)
    assert s.us_dual_paper_preview_enabled is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/us_dual_paper/test_config_flag.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'us_dual_paper_preview_enabled'`

- [ ] **Step 3: Add the field**

In `app/core/config.py`, alongside the other broker feature flags (e.g. near `alpaca_paper_api_key`), add:

```python
    # ROB-326 — US dual-paper premarket preview/preflight path (read-only, default off)
    us_dual_paper_preview_enabled: bool = False
```

(The field name maps to env `US_DUAL_PAPER_PREVIEW_ENABLED` via the existing pydantic-settings config — confirm the model uses case-insensitive env names like the surrounding fields.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/us_dual_paper/test_config_flag.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/services/us_dual_paper/
git commit -m "feat(rob-326): add US_DUAL_PAPER_PREVIEW_ENABLED config flag (default off)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: Packet schemas

**Files:**
- Create: `app/schemas/us_dual_paper.py`
- Test: `tests/services/us_dual_paper/test_schemas.py`

- [ ] **Step 1: Write the failing test**

`tests/services/us_dual_paper/test_schemas.py`:

```python
import pytest

from app.schemas.us_dual_paper import (
    AccountStateSummary,
    BrokerPreviewResult,
    DualBrokerPreviewPacket,
    DualPaperBrokerStatus,
)


@pytest.mark.unit
def test_packet_defaults_are_safe():
    packet = DualBrokerPreviewPacket(
        symbol="NVDA",
        limit_price_source="operator_input",
        notional_cap_usd=50.0,
        brokers={},
    )
    assert packet.market == "us"
    assert packet.side == "buy"
    assert packet.order_type == "limit"
    assert packet.submit_enabled is False


@pytest.mark.unit
def test_broker_result_independent_status():
    ok = BrokerPreviewResult(account_scope="alpaca_paper", status=DualPaperBrokerStatus.PREVIEWED)
    bad = BrokerPreviewResult(
        account_scope="kis_mock",
        status=DualPaperBrokerStatus.ERROR,
        reason="boom",
    )
    packet = DualBrokerPreviewPacket(
        symbol="NVDA",
        limit_price_source="operator_input",
        notional_cap_usd=50.0,
        brokers={"alpaca_paper": ok, "kis_mock": bad},
    )
    assert packet.brokers["alpaca_paper"].status is DualPaperBrokerStatus.PREVIEWED
    assert packet.brokers["kis_mock"].status is DualPaperBrokerStatus.ERROR


@pytest.mark.unit
def test_account_state_summary_is_numbers_only():
    summary = AccountStateSummary(
        cash_usd=100.0, buying_power_usd=100.0, position_count=2, open_order_count=0
    )
    dumped = summary.model_dump()
    assert set(dumped) == {"cash_usd", "buying_power_usd", "position_count", "open_order_count"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/us_dual_paper/test_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.schemas.us_dual_paper'`

- [ ] **Step 3: Implement the schemas**

`app/schemas/us_dual_paper.py`:

```python
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class DualPaperBrokerStatus(StrEnum):
    PREVIEWED = "previewed"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class AccountStateSummary(BaseModel):
    """Read-only account context. Counts/numbers only — never secrets or raw payloads."""

    model_config = {"extra": "forbid"}

    cash_usd: float | None = None
    buying_power_usd: float | None = None
    position_count: int | None = None
    open_order_count: int | None = None


class BrokerPreviewRequest(BaseModel):
    model_config = {"extra": "forbid"}

    symbol: str
    quantity: float
    limit_price_usd: float
    notional_cap_usd: float
    reference_price_usd: float | None = None  # operator/report-supplied; quote fallback later


class BrokerPreviewResult(BaseModel):
    model_config = {"extra": "forbid"}

    account_scope: str  # "kis_mock" | "alpaca_paper"
    status: DualPaperBrokerStatus
    reason: str | None = None
    blocked_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    quantity: float | None = None
    limit_price_usd: float | None = None
    notional_usd: float | None = None
    account_state: AccountStateSummary | None = None
    check_details: dict = Field(default_factory=dict)  # never secrets


class DualBrokerPreviewPacket(BaseModel):
    model_config = {"extra": "forbid"}

    symbol: str
    market: str = "us"
    side: str = "buy"  # long/buy only this issue
    order_type: str = "limit"  # limit only this issue
    limit_price_source: str  # "quote" | "operator_input" | "report_item"
    notional_cap_usd: float
    generated_at: datetime = Field(default_factory=lambda: datetime.now())
    submit_enabled: bool = False  # always False on premarket path
    brokers: dict[str, BrokerPreviewResult] = Field(default_factory=dict)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/us_dual_paper/test_schemas.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/schemas/us_dual_paper.py tests/services/us_dual_paper/test_schemas.py
git commit -m "feat(rob-326): dual-paper preview packet schemas

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: Capability matrix

**Files:**
- Create: `app/services/us_dual_paper/__init__.py` (empty), `app/services/us_dual_paper/capability_matrix.py`
- Test: `tests/services/us_dual_paper/test_capability_matrix.py`

- [ ] **Step 1: Write the failing test (pins the shape)**

`tests/services/us_dual_paper/test_capability_matrix.py`:

```python
import pytest

from app.services.us_dual_paper.capability_matrix import (
    SUPPORTED_ACCOUNT_SCOPES,
    get_capability_matrix,
)


@pytest.mark.unit
def test_matrix_covers_both_brokers():
    matrix = get_capability_matrix()
    assert set(matrix) == {"kis_mock", "alpaca_paper"}
    assert SUPPORTED_ACCOUNT_SCOPES == ("kis_mock", "alpaca_paper")


@pytest.mark.unit
@pytest.mark.parametrize("scope", ["kis_mock", "alpaca_paper"])
def test_matrix_entry_is_long_limit_paper_only(scope):
    entry = get_capability_matrix()[scope]
    assert entry["market"] == "us"
    assert entry["asset_class"] == "us_equity"
    assert entry["supported_sides"] == ["buy"]
    assert entry["supported_order_types"] == ["limit"]
    assert entry["preview_supported"] is True
    assert entry["submit_gate"] == "confirm_only_default_disabled"
    assert entry["account_cash_read"] is True
    assert "no_kis_mock_us_alias" not in entry  # canonical scope token only
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/us_dual_paper/test_capability_matrix.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`app/services/us_dual_paper/__init__.py`: empty file.

`app/services/us_dual_paper/capability_matrix.py`:

```python
"""Declarative, secret-free capability matrix for US dual-paper preview (ROB-326).

Keyed by canonical account_scope tokens (kis_mock, alpaca_paper). market is "us".
This issue supports long/buy + limit only; submit is confirm-only/default-disabled.
"""

from __future__ import annotations

from typing import Any

SUPPORTED_ACCOUNT_SCOPES: tuple[str, ...] = ("kis_mock", "alpaca_paper")


def get_capability_matrix() -> dict[str, dict[str, Any]]:
    common: dict[str, Any] = {
        "market": "us",
        "asset_class": "us_equity",
        "supported_sides": ["buy"],
        "supported_order_types": ["limit"],
        "preview_supported": True,
        "submit_gate": "confirm_only_default_disabled",
        "account_cash_read": True,
        "positions_read": True,
    }
    return {
        "kis_mock": {
            **common,
            "broker": "kis",
            "account_mode": "kis_mock",
            "open_orders_read": "partial",  # mock open-order reader may be unavailable
            "market_session_note": (
                "Mock overseas reads work pre/post regular session; quote freshness "
                "is operator-supplied until a US quote adapter lands."
            ),
            "known_unknown_fields": ["live_quote_state"],
        },
        "alpaca_paper": {
            **common,
            "broker": "alpaca",
            "account_mode": "alpaca_paper",
            "open_orders_read": True,
            "market_session_note": (
                "Paper account/positions readable anytime; limit preview is "
                "qty + limit_price (notional not allowed for equity limit)."
            ),
            "known_unknown_fields": [],
        },
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/us_dual_paper/test_capability_matrix.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/us_dual_paper/__init__.py app/services/us_dual_paper/capability_matrix.py tests/services/us_dual_paper/test_capability_matrix.py
git commit -m "feat(rob-326): US dual-paper capability matrix (kis_mock + alpaca_paper)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 4: Adapter protocol + shared types

**Files:**
- Create: `app/services/us_dual_paper/adapters/__init__.py` (empty), `app/services/us_dual_paper/adapters/base.py`
- Test: `tests/services/us_dual_paper/test_adapter_base.py`

- [ ] **Step 1: Write the failing test**

`tests/services/us_dual_paper/test_adapter_base.py`:

```python
import pytest

from app.schemas.us_dual_paper import AccountStateSummary, BrokerPreviewRequest, BrokerPreviewResult
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter


class _Fake(BrokerPreviewAdapter):
    account_scope = "alpaca_paper"

    def is_enabled(self) -> bool:
        return True

    def missing_env_keys(self) -> list[str]:
        return []

    async def read_account_state(self) -> AccountStateSummary:
        return AccountStateSummary(buying_power_usd=10.0)

    async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult:
        from app.schemas.us_dual_paper import DualPaperBrokerStatus

        return BrokerPreviewResult(account_scope=self.account_scope, status=DualPaperBrokerStatus.PREVIEWED)


@pytest.mark.unit
def test_protocol_conformance():
    adapter = _Fake()
    assert isinstance(adapter, BrokerPreviewAdapter)
    assert adapter.account_scope == "alpaca_paper"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/us_dual_paper/test_adapter_base.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`app/services/us_dual_paper/adapters/__init__.py`: empty file.

`app/services/us_dual_paper/adapters/base.py`:

```python
from __future__ import annotations

from abc import ABC, abstractmethod

from app.schemas.us_dual_paper import (
    AccountStateSummary,
    BrokerPreviewRequest,
    BrokerPreviewResult,
)


class BrokerUnsupportedError(Exception):
    """Raised when a broker cannot be previewed (e.g. creds/flag missing)."""


class BrokerPreviewAdapter(ABC):
    """Read-only, side-effect-free per-broker preview adapter.

    Implementations MUST NOT import or reach any submit/cancel/modify/place path.
    """

    account_scope: str

    @abstractmethod
    def is_enabled(self) -> bool:
        """True when creds/flags allow read-only access to this broker."""

    @abstractmethod
    def missing_env_keys(self) -> list[str]:
        """Names (never values) of env keys required but absent."""

    @abstractmethod
    async def read_account_state(self) -> AccountStateSummary:
        """Read-only cash/buying-power/positions summary."""

    @abstractmethod
    async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult:
        """Pure validation of a buy/limit order. Never submits."""
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/us_dual_paper/test_adapter_base.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/us_dual_paper/adapters/ tests/services/us_dual_paper/test_adapter_base.py
git commit -m "feat(rob-326): broker preview adapter protocol

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 5: Alpaca read-only account-state adapter

**Files:**
- Create: `app/services/us_dual_paper/adapters/alpaca.py`
- Test: `tests/services/us_dual_paper/test_alpaca_adapter.py`

- [ ] **Step 1: Write the failing test (fakes the broker service — no network)**

`tests/services/us_dual_paper/test_alpaca_adapter.py`:

```python
import pytest

from app.services.us_dual_paper.adapters.alpaca import AlpacaPaperAdapter


class _FakeCash:
    cash = "100.50"
    buying_power = "200.00"


class _FakeService:
    async def get_cash(self):
        return _FakeCash()

    async def list_positions(self):
        return [object(), object()]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_account_state_summarizes_numbers_only():
    adapter = AlpacaPaperAdapter(service_factory=lambda: _FakeService())
    summary = await adapter.read_account_state()
    assert summary.cash_usd == pytest.approx(100.50)
    assert summary.buying_power_usd == pytest.approx(200.00)
    assert summary.position_count == 2


@pytest.mark.unit
def test_account_scope_is_canonical():
    adapter = AlpacaPaperAdapter(service_factory=lambda: _FakeService())
    assert adapter.account_scope == "alpaca_paper"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/us_dual_paper/test_alpaca_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement (preview() added in PR2 Task 11; read-only here)**

`app/services/us_dual_paper/adapters/alpaca.py`:

```python
from __future__ import annotations

from collections.abc import Callable

from app.schemas.us_dual_paper import (
    AccountStateSummary,
    BrokerPreviewRequest,
    BrokerPreviewResult,
)
from app.services.brokers.alpaca.config import AlpacaPaperSettings
from app.services.brokers.alpaca.exceptions import AlpacaPaperConfigurationError
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter

ServiceFactory = Callable[[], AlpacaPaperBrokerService]

_ALPACA_ENV_KEYS = ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET")


def _default_factory() -> AlpacaPaperBrokerService:
    return AlpacaPaperBrokerService()


class AlpacaPaperAdapter(BrokerPreviewAdapter):
    account_scope = "alpaca_paper"

    def __init__(self, service_factory: ServiceFactory = _default_factory) -> None:
        self._service_factory = service_factory

    def is_enabled(self) -> bool:
        return not self.missing_env_keys()

    def missing_env_keys(self) -> list[str]:
        try:
            s = AlpacaPaperSettings.from_app_settings()
        except AlpacaPaperConfigurationError:
            return list(_ALPACA_ENV_KEYS)
        missing: list[str] = []
        if not s.api_key:
            missing.append("ALPACA_PAPER_API_KEY")
        if not s.api_secret:
            missing.append("ALPACA_PAPER_API_SECRET")
        return missing

    async def read_account_state(self) -> AccountStateSummary:
        service = self._service_factory()
        cash = await service.get_cash()
        positions = await service.list_positions()
        return AccountStateSummary(
            cash_usd=float(cash.cash),
            buying_power_usd=float(cash.buying_power),
            position_count=len(positions),
            open_order_count=None,
        )

    async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult:  # PR2 Task 11
        raise NotImplementedError("preview() is implemented in PR2")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/us_dual_paper/test_alpaca_adapter.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/us_dual_paper/adapters/alpaca.py tests/services/us_dual_paper/test_alpaca_adapter.py
git commit -m "feat(rob-326): Alpaca paper read-only account-state adapter

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 6: KIS mock read-only account-state adapter

**Files:**
- Create: `app/services/us_dual_paper/adapters/kis_mock.py`
- Test: `tests/services/us_dual_paper/test_kis_mock_adapter.py`

**Key facts (verified):** `kis.inquire_overseas_margin(is_mock=True) -> list[dict]` (USD row: `crcy_cd=="USD"`, `natn_name in {"미국","US","USA"}`, cash `frcr_dncl_amt1`, buying power `frcr_ord_psbl_amt1`). `kis.fetch_my_us_stocks(is_mock=True) -> list[dict]` (one row per holding). KIS mock config fields: `kis_mock_enabled`, `kis_mock_app_key`, `kis_mock_app_secret`, `kis_mock_account_no` → env `KIS_MOCK_ENABLED/KIS_MOCK_APP_KEY/KIS_MOCK_APP_SECRET/KIS_MOCK_ACCOUNT_NO`.

- [ ] **Step 1: Write the failing test (inject a fake kis client — no network, is_mock asserted)**

`tests/services/us_dual_paper/test_kis_mock_adapter.py`:

```python
import pytest

from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter


class _FakeKis:
    def __init__(self):
        self.margin_calls = []
        self.holdings_calls = []

    async def inquire_overseas_margin(self, is_mock=False):
        self.margin_calls.append(is_mock)
        return [
            {"crcy_cd": "KRW", "natn_name": "한국", "frcr_dncl_amt1": 0.0},
            {"crcy_cd": "USD", "natn_name": "미국", "frcr_dncl_amt1": 500.0, "frcr_ord_psbl_amt1": 480.0},
        ]

    async def fetch_my_us_stocks(self, is_mock=False):
        self.holdings_calls.append(is_mock)
        return [{"ovrs_pdno": "AAPL"}, {"ovrs_pdno": "TSLA"}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_account_state_pins_is_mock_true():
    fake = _FakeKis()
    adapter = KisMockUsAdapter(kis_client=fake, enabled=True)
    summary = await adapter.read_account_state()
    assert fake.margin_calls == [True]
    assert fake.holdings_calls == [True]
    assert summary.cash_usd == pytest.approx(500.0)
    assert summary.buying_power_usd == pytest.approx(480.0)
    assert summary.position_count == 2


@pytest.mark.unit
def test_account_scope_is_canonical_kis_mock():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)
    assert adapter.account_scope == "kis_mock"  # NOT kis_mock_us
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/us_dual_paper/test_kis_mock_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement (read-only; preview() added in PR2 Task 12)**

`app/services/us_dual_paper/adapters/kis_mock.py`:

```python
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.config import settings
from app.schemas.us_dual_paper import (
    AccountStateSummary,
    BrokerPreviewRequest,
    BrokerPreviewResult,
)
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter

_USD_NATIONS = {"미국", "US", "USA"}
_KIS_MOCK_ENV_KEYS = (
    "KIS_MOCK_ENABLED",
    "KIS_MOCK_APP_KEY",
    "KIS_MOCK_APP_SECRET",
    "KIS_MOCK_ACCOUNT_NO",
)


def _to_float(value: Any) -> float | None:
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_usd_row(row: Mapping[str, Any]) -> bool:
    crcy = str(row.get("crcy_cd") or "").strip().upper()
    natn = str(row.get("natn_name") or "").strip().upper()
    return crcy == "USD" and (not natn or natn in {n.upper() for n in _USD_NATIONS})


def _default_kis_client() -> Any:
    from app.services.brokers.kis import kis  # local import: never at module scope

    return kis


class KisMockUsAdapter(BrokerPreviewAdapter):
    account_scope = "kis_mock"

    def __init__(self, *, kis_client: Any | None = None, enabled: bool | None = None) -> None:
        self._kis_client = kis_client if kis_client is not None else _default_kis_client()
        self._enabled_override = enabled

    def is_enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return not self.missing_env_keys()

    def missing_env_keys(self) -> list[str]:
        checks = {
            "KIS_MOCK_ENABLED": bool(getattr(settings, "kis_mock_enabled", False)),
            "KIS_MOCK_APP_KEY": bool(getattr(settings, "kis_mock_app_key", None)),
            "KIS_MOCK_APP_SECRET": bool(getattr(settings, "kis_mock_app_secret", None)),
            "KIS_MOCK_ACCOUNT_NO": bool(getattr(settings, "kis_mock_account_no", None)),
        }
        return [name for name, present in checks.items() if not present]

    async def read_account_state(self) -> AccountStateSummary:
        rows = await self._kis_client.inquire_overseas_margin(is_mock=True)
        cash_usd: float | None = None
        buying_power_usd: float | None = None
        for row in rows or []:
            if isinstance(row, Mapping) and _is_usd_row(row):
                cash_usd = _to_float(row.get("frcr_dncl_amt1"))
                buying_power_usd = _to_float(row.get("frcr_ord_psbl_amt1"))
                break
        holdings = await self._kis_client.fetch_my_us_stocks(is_mock=True)
        return AccountStateSummary(
            cash_usd=cash_usd,
            buying_power_usd=buying_power_usd,
            position_count=len(holdings or []),
            open_order_count=None,
        )

    async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult:  # PR2 Task 12
        raise NotImplementedError("preview() is implemented in PR2")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/us_dual_paper/test_kis_mock_adapter.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/us_dual_paper/adapters/kis_mock.py tests/services/us_dual_paper/test_kis_mock_adapter.py
git commit -m "feat(rob-326): KIS mock US read-only account-state adapter (is_mock pinned)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 7: Read-only MCP tools (capability matrix + account states)

**Files:**
- Create: `app/mcp_server/tooling/us_dual_paper.py`
- Modify: `app/mcp_server/tooling/registry.py` (import block ~line 26-35; call in always-on read-only group ~line 111-114)
- Test: `tests/services/us_dual_paper/test_mcp_tools.py`

- [ ] **Step 1: Write the failing test**

`tests/services/us_dual_paper/test_mcp_tools.py`:

```python
import pytest

from app.mcp_server.tooling.us_dual_paper import (
    US_DUAL_PAPER_TOOL_NAMES,
    us_dual_paper_capability_matrix,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_capability_matrix_tool_returns_both_scopes():
    result = await us_dual_paper_capability_matrix()
    assert set(result["matrix"]) == {"kis_mock", "alpaca_paper"}
    assert result["submit_enabled"] is False


@pytest.mark.unit
def test_tool_names_pinned():
    assert "us_dual_paper_capability_matrix" in US_DUAL_PAPER_TOOL_NAMES
    assert "us_dual_paper_account_states" in US_DUAL_PAPER_TOOL_NAMES
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/us_dual_paper/test_mcp_tools.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`app/mcp_server/tooling/us_dual_paper.py`:

```python
"""Read-only US dual-paper MCP tools (ROB-326). No submit/cancel/modify surface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.schemas.us_dual_paper import DualPaperBrokerStatus
from app.services.us_dual_paper.adapters.alpaca import AlpacaPaperAdapter
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter
from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter
from app.services.us_dual_paper.capability_matrix import get_capability_matrix

if TYPE_CHECKING:
    from fastmcp import FastMCP

US_DUAL_PAPER_TOOL_NAMES: set[str] = {
    "us_dual_paper_capability_matrix",
    "us_dual_paper_account_states",
}


def _adapters() -> list[BrokerPreviewAdapter]:
    return [KisMockUsAdapter(), AlpacaPaperAdapter()]


async def us_dual_paper_capability_matrix() -> dict[str, Any]:
    """Return the read-only capability matrix for kis_mock + alpaca_paper (US)."""
    return {"submit_enabled": False, "matrix": get_capability_matrix()}


async def us_dual_paper_account_states() -> dict[str, Any]:
    """Read-only account states for both paper brokers. Counts/numbers only — no secrets."""
    out: dict[str, Any] = {"submit_enabled": False, "brokers": {}}
    for adapter in _adapters():
        scope = adapter.account_scope
        if not adapter.is_enabled():
            out["brokers"][scope] = {
                "status": DualPaperBrokerStatus.UNSUPPORTED.value,
                "missing_env_keys": adapter.missing_env_keys(),
            }
            continue
        try:
            summary = await adapter.read_account_state()
            out["brokers"][scope] = {
                "status": "ok",
                "account_state": summary.model_dump(),
            }
        except Exception as exc:  # isolate per broker
            out["brokers"][scope] = {
                "status": DualPaperBrokerStatus.ERROR.value,
                "reason": type(exc).__name__,
            }
    return out


def register_us_dual_paper_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="us_dual_paper_capability_matrix",
        description="Read-only US dual-paper (kis_mock + alpaca_paper) capability matrix. No submit.",
    )(us_dual_paper_capability_matrix)
    _ = mcp.tool(
        name="us_dual_paper_account_states",
        description=(
            "Read-only account states (cash/buying-power/position counts) for KIS mock US "
            "and Alpaca Paper. Counts/numbers only, no secrets. No submit/cancel/modify."
        ),
    )(us_dual_paper_account_states)


__all__ = [
    "US_DUAL_PAPER_TOOL_NAMES",
    "register_us_dual_paper_tools",
    "us_dual_paper_account_states",
    "us_dual_paper_capability_matrix",
]
```

- [ ] **Step 4: Wire into the registry**

In `app/mcp_server/tooling/registry.py`, add to the import block (near the `alpaca_paper_preview` import ~line 26-35):

```python
from app.mcp_server.tooling.us_dual_paper import register_us_dual_paper_tools
```

And inside `register_all_tools`, in the always-on read-only group next to `register_alpaca_paper_preview_tools(mcp)` (~line 111-114):

```python
    register_us_dual_paper_tools(mcp)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/services/us_dual_paper/test_mcp_tools.py -v`
Expected: PASS (2 passed)

Also confirm the registry still imports cleanly:
Run: `uv run python -c "from app.mcp_server.tooling.registry import register_all_tools; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/us_dual_paper.py app/mcp_server/tooling/registry.py tests/services/us_dual_paper/test_mcp_tools.py
git commit -m "feat(rob-326): read-only US dual-paper MCP tools (matrix + account states)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 8: Preflight smoke CLI (`--mode preflight`)

**Files:**
- Create: `scripts/smoke/us_dual_paper_preview_smoke.py`
- Test: `tests/services/us_dual_paper/test_smoke_cli_preflight.py`

Mirrors `scripts/binance_spot_demo_smoke.py` / `scripts/kiwoom_mock_smoke.py`: argparse `--mode`, env-gate `US_DUAL_PAPER_PREVIEW_ENABLED`, exit codes 0/1/2, missing-env reported by **name only**.

- [ ] **Step 1: Write the failing test**

`tests/services/us_dual_paper/test_smoke_cli_preflight.py`:

```python
import pytest

from scripts.smoke import us_dual_paper_preview_smoke as smoke


@pytest.mark.unit
def test_disabled_is_noop_exit_zero(monkeypatch, capsys):
    monkeypatch.delenv("US_DUAL_PAPER_PREVIEW_ENABLED", raising=False)
    rc = smoke.main(["--mode", "preflight"])
    assert rc == 0
    assert "US_DUAL_PAPER_PREVIEW_ENABLED" in capsys.readouterr().out


@pytest.mark.unit
def test_preflight_reports_missing_env_names_only(monkeypatch, capsys):
    monkeypatch.setenv("US_DUAL_PAPER_PREVIEW_ENABLED", "true")
    # Force both brokers to look disabled by clearing creds
    for key in ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET",
                "KIS_MOCK_ENABLED", "KIS_MOCK_APP_KEY", "KIS_MOCK_APP_SECRET", "KIS_MOCK_ACCOUNT_NO"):
        monkeypatch.delenv(key, raising=False)
    rc = smoke.main(["--mode", "preflight"])
    out = capsys.readouterr().out
    # exit 1 = config/credential problem; names present, no secret values
    assert rc in (0, 1)
    assert "missing_env_keys" in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/us_dual_paper/test_smoke_cli_preflight.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.smoke.us_dual_paper_preview_smoke`

- [ ] **Step 3: Implement**

`scripts/smoke/us_dual_paper_preview_smoke.py`:

```python
"""US dual-paper premarket preview smoke (ROB-326). Default-disabled, read-only.

Never prints secret values — only env key NAMES on missing creds.
Exit codes: 0 success / disabled no-op; 1 config or credential problem;
2 operational/runtime failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from app.services.us_dual_paper.adapters.alpaca import AlpacaPaperAdapter
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter
from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter
from app.services.us_dual_paper.capability_matrix import get_capability_matrix


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True))


def _adapters() -> list[BrokerPreviewAdapter]:
    return [KisMockUsAdapter(), AlpacaPaperAdapter()]


async def _run_preflight() -> int:
    _emit({"step": "capability_matrix", "matrix": get_capability_matrix()})
    any_missing = False
    for adapter in _adapters():
        missing = adapter.missing_env_keys()
        enabled = adapter.is_enabled()
        any_missing = any_missing or not enabled
        _emit({
            "step": "broker_preflight",
            "account_scope": adapter.account_scope,
            "enabled": enabled,
            "missing_env_keys": missing,  # NAMES only, never values
        })
    return 1 if any_missing else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="US dual-paper premarket preview smoke")
    parser.add_argument("--mode", required=True, choices=["preflight"])  # 'preview' added in PR2
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not _truthy(os.environ.get("US_DUAL_PAPER_PREVIEW_ENABLED")):
        _emit({"step": "disabled", "hint": "set US_DUAL_PAPER_PREVIEW_ENABLED=true to opt in"})
        return 0
    try:
        if args.mode == "preflight":
            return asyncio.run(_run_preflight())
        return 2
    except Exception as exc:  # noqa: BLE001
        _emit({"step": "error", "error_type": type(exc).__name__})
        return 2


if __name__ == "__main__":
    sys.exit(main())
```

Also create `scripts/smoke/__init__.py` if it does not exist (check first: `ls scripts/smoke/__init__.py`).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/us_dual_paper/test_smoke_cli_preflight.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke/us_dual_paper_preview_smoke.py tests/services/us_dual_paper/test_smoke_cli_preflight.py
git commit -m "feat(rob-326): preflight smoke CLI (default-disabled, names-only)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 9: Import guard — no broker mutation / live-KIS imports

**Files:**
- Test: `tests/services/us_dual_paper/test_no_mutation_imports.py`

Mirrors `tests/services/brokers/binance/demo/test_no_testnet_imports.py` (AST prefix-match).

- [ ] **Step 1: Write the guard test**

`tests/services/us_dual_paper/test_no_mutation_imports.py`:

```python
import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GUARDED = REPO_ROOT / "app" / "services" / "us_dual_paper"

# Modules that imply order mutation or live-KIS routing must never be imported here.
_BANNED_PREFIXES = (
    "app.mcp_server.tooling.order_execution",
    "app.services.brokers.kis.overseas_orders",
    "app.services.brokers.kis.domestic_orders",
    "app.services.kis_trading_service",
    "app.services.brokers.alpaca.orders",
    "app.mcp_server.tooling.alpaca_paper_orders",
)
# Symbols that must never appear as ImportFrom names.
_BANNED_NAMES = {"submit_order", "place_order", "cancel_order", "modify_order", "_place_order_impl"}


def _is_banned_module(module: str) -> bool:
    return any(module == p or module.startswith(p + ".") for p in _BANNED_PREFIXES)


def _py_files():
    return sorted(GUARDED.rglob("*.py"))


def test_guarded_dir_exists():
    assert GUARDED.is_dir()
    assert _py_files()


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: str(p))
def test_no_banned_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if _is_banned_module(a.name)]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if _is_banned_module(mod):
                offenders.append(mod)
            offenders += [a.name for a in node.names if a.name in _BANNED_NAMES]
    assert not offenders, f"{path} imports forbidden mutation/live surfaces: {offenders}"
```

- [ ] **Step 2: Run to verify it passes (the package is already clean)**

Run: `uv run pytest tests/services/us_dual_paper/test_no_mutation_imports.py -v`
Expected: PASS (all files clean — guard is green from the start and stays green)

- [ ] **Step 3: Commit**

```bash
git add tests/services/us_dual_paper/test_no_mutation_imports.py
git commit -m "test(rob-326): import guard — no mutation/live-KIS imports in us_dual_paper

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 10: PR1 verification + push

- [ ] **Step 1: Full suite for the package + lint + import guards**

```bash
uv run pytest tests/services/us_dual_paper/ -v
uv run ruff check app/ tests/ scripts/
uv run python -c "from app.mcp_server.tooling.registry import register_all_tools; print('registry ok')"
```
Expected: all green; `registry ok`.

- [ ] **Step 2: Open PR1**

```bash
git push -u origin rob-326
gh pr create --base main --title "feat(rob-326): US dual-paper premarket — matrix + read-only account adapters + preflight (PR1)" --body "$(cat <<'EOF'
Implements ROB-326 PR1 (read-only, default-disabled).

- `US_DUAL_PAPER_PREVIEW_ENABLED` config flag (default off)
- packet schemas (`app/schemas/us_dual_paper.py`)
- capability matrix (`kis_mock` + `alpaca_paper`, canonical scopes, no `kis_mock_us`)
- read-only account-state adapters (Alpaca paper + KIS mock US, `is_mock` pinned)
- read-only MCP tools: `us_dual_paper_capability_matrix`, `us_dual_paper_account_states`
- preflight smoke CLI (names-only, exit 0/1/2)
- import guard: no mutation/live-KIS imports

No submit/cancel/modify path. No scheduler/Prefect/frontend.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

> Per the user's pre-merge gate: confirm the Test workflow is green AND `ruff check` clean before any merge. Do not `--auto` merge a red main.

---

# PR2 — Preview gates, dual packet, preview MCP tool, full CLI, runbook

## Task 11: Alpaca preview adapter method

**Files:**
- Modify: `app/services/us_dual_paper/adapters/alpaca.py` (implement `preview`)
- Test: `tests/services/us_dual_paper/test_alpaca_preview.py`

Wraps the existing `alpaca_paper_preview_order` (pure validator). For US equity limit: pass `qty` + `limit_price` (notional is rejected for equity limit). Map its dict → `BrokerPreviewResult`.

- [ ] **Step 1: Write the failing test**

`tests/services/us_dual_paper/test_alpaca_preview.py`:

```python
import pytest

from app.schemas.us_dual_paper import BrokerPreviewRequest, DualPaperBrokerStatus
from app.services.us_dual_paper.adapters import alpaca as alpaca_mod
from app.services.us_dual_paper.adapters.alpaca import AlpacaPaperAdapter


@pytest.fixture
def _stub_preview(monkeypatch):
    async def _fake(symbol, side, type, qty=None, notional=None, limit_price=None, **kw):  # noqa: A002
        cost = float(qty) * float(limit_price)
        return {
            "success": True,
            "account_mode": "alpaca_paper",
            "preview": True,
            "submitted": False,
            "estimated_cost": str(cost),
            "account_context": {"cash": "1000", "buying_power": "1000"},
            "would_exceed_buying_power": cost > 1000,
            "warnings": [],
        }

    monkeypatch.setattr(alpaca_mod, "alpaca_paper_preview_order", _fake)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_within_cap_is_previewed(_stub_preview):
    adapter = AlpacaPaperAdapter()
    res = await adapter.preview(
        BrokerPreviewRequest(symbol="NVDA", quantity=1, limit_price_usd=10.0, notional_cap_usd=50.0)
    )
    assert res.account_scope == "alpaca_paper"
    assert res.status is DualPaperBrokerStatus.PREVIEWED
    assert res.notional_usd == pytest.approx(10.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_over_cap_is_blocked(_stub_preview):
    adapter = AlpacaPaperAdapter()
    res = await adapter.preview(
        BrokerPreviewRequest(symbol="NVDA", quantity=10, limit_price_usd=10.0, notional_cap_usd=50.0)
    )
    assert res.status is DualPaperBrokerStatus.BLOCKED
    assert "notional_exceeds_cap" in res.blocked_reasons
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/us_dual_paper/test_alpaca_preview.py -v`
Expected: FAIL — `NotImplementedError` (preview stub from PR1)

- [ ] **Step 3: Implement `preview` (replace the PR1 stub)**

In `app/services/us_dual_paper/adapters/alpaca.py`, add the import at top:

```python
from app.mcp_server.tooling.alpaca_paper_preview import alpaca_paper_preview_order
from app.schemas.us_dual_paper import DualPaperBrokerStatus
```

> Note: `alpaca_paper_preview_order` is a pure validator/echo — it never calls `POST /v2/orders`. The import-guard banned list (Task 9) covers `alpaca_paper_orders` (the submit tool), NOT `alpaca_paper_preview`.

Replace the stub `preview` with:

```python
    async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult:
        notional = req.quantity * req.limit_price_usd
        blocked: list[str] = []
        warnings: list[str] = []
        if req.quantity <= 0:
            blocked.append("quantity_must_be_positive")
        if req.limit_price_usd <= 0:
            blocked.append("limit_price_must_be_positive")
        if notional > req.notional_cap_usd:
            blocked.append("notional_exceeds_cap")

        buying_power: float | None = None
        if not blocked:
            try:
                echo = await alpaca_paper_preview_order(
                    symbol=req.symbol,
                    side="buy",
                    type="limit",
                    qty=req.quantity,
                    limit_price=req.limit_price_usd,
                    asset_class="us_equity",
                )
                ctx = echo.get("account_context") or {}
                if ctx.get("buying_power") is not None:
                    buying_power = float(ctx["buying_power"])
                if echo.get("would_exceed_buying_power") is True:
                    blocked.append("would_exceed_buying_power")
                warnings.extend(echo.get("warnings") or [])
            except Exception as exc:  # surfaced to orchestrator as error
                raise exc

        status = DualPaperBrokerStatus.BLOCKED if blocked else DualPaperBrokerStatus.PREVIEWED
        return BrokerPreviewResult(
            account_scope=self.account_scope,
            status=status,
            blocked_reasons=blocked,
            warnings=warnings,
            quantity=req.quantity,
            limit_price_usd=req.limit_price_usd,
            notional_usd=round(notional, 2),
            account_state=AccountStateSummary(buying_power_usd=buying_power),
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/us_dual_paper/test_alpaca_preview.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Re-run the import guard (preview import must stay allowed)**

Run: `uv run pytest tests/services/us_dual_paper/test_no_mutation_imports.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/us_dual_paper/adapters/alpaca.py tests/services/us_dual_paper/test_alpaca_preview.py
git commit -m "feat(rob-326): Alpaca paper preview adapter (buy/limit, cap + buying-power)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 12: KIS mock preview gate method

**Files:**
- Modify: `app/services/us_dual_paper/adapters/kis_mock.py` (implement `preview`)
- Test: `tests/services/us_dual_paper/test_kis_mock_preview.py`

Pure gate modeled on `app/services/action_report/us/order_preview.py::preview_kis_us_live_order` but for `kis_mock`. Buy/limit only. Journal fields are out of scope here (warnings belong to the report layer; submit is disabled). Uses `read_account_state()` for buying-power sufficiency. Limit-deviation check runs only when `reference_price_usd` is supplied (operator/report); otherwise a warning.

- [ ] **Step 1: Write the failing test**

`tests/services/us_dual_paper/test_kis_mock_preview.py`:

```python
import pytest

from app.schemas.us_dual_paper import BrokerPreviewRequest, DualPaperBrokerStatus
from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter


class _FakeKis:
    async def inquire_overseas_margin(self, is_mock=False):
        return [{"crcy_cd": "USD", "natn_name": "미국", "frcr_dncl_amt1": 500.0, "frcr_ord_psbl_amt1": 40.0}]

    async def fetch_my_us_stocks(self, is_mock=False):
        return []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_within_cap_and_buying_power_is_previewed():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)
    res = await adapter.preview(
        BrokerPreviewRequest(symbol="NVDA", quantity=1, limit_price_usd=10.0, notional_cap_usd=50.0)
    )
    assert res.status is DualPaperBrokerStatus.PREVIEWED
    assert res.notional_usd == pytest.approx(10.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_over_cap_is_blocked():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)
    res = await adapter.preview(
        BrokerPreviewRequest(symbol="NVDA", quantity=10, limit_price_usd=10.0, notional_cap_usd=50.0)
    )
    assert res.status is DualPaperBrokerStatus.BLOCKED
    assert "notional_exceeds_cap" in res.blocked_reasons


@pytest.mark.unit
@pytest.mark.asyncio
async def test_insufficient_buying_power_is_blocked():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)  # buying power = 40
    res = await adapter.preview(
        BrokerPreviewRequest(symbol="NVDA", quantity=1, limit_price_usd=45.0, notional_cap_usd=50.0)
    )
    assert res.status is DualPaperBrokerStatus.BLOCKED
    assert "insufficient_buying_power" in res.blocked_reasons


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_reference_price_warns_not_blocks():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)
    res = await adapter.preview(
        BrokerPreviewRequest(symbol="NVDA", quantity=1, limit_price_usd=10.0, notional_cap_usd=50.0)
    )
    assert "reference_price_missing_for_limit_sanity" in res.warnings
    assert res.status is DualPaperBrokerStatus.PREVIEWED
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/us_dual_paper/test_kis_mock_preview.py -v`
Expected: FAIL — `NotImplementedError`

- [ ] **Step 3: Implement `preview` (replace the PR1 stub)**

In `app/services/us_dual_paper/adapters/kis_mock.py` add at top:

```python
from app.schemas.us_dual_paper import DualPaperBrokerStatus

_MAX_LIMIT_DEVIATION_PCT = 10.0
```

Replace the stub `preview` with:

```python
    async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult:
        blocked: list[str] = []
        warnings: list[str] = []
        notional = req.quantity * req.limit_price_usd

        if req.quantity <= 0:
            blocked.append("quantity_must_be_positive")
        if req.limit_price_usd <= 0:
            blocked.append("limit_price_must_be_positive")
        if notional > req.notional_cap_usd:
            blocked.append("notional_exceeds_cap")

        summary = await self.read_account_state()
        if summary.buying_power_usd is None:
            warnings.append("buying_power_unavailable")
        elif notional > summary.buying_power_usd:
            blocked.append("insufficient_buying_power")

        if req.reference_price_usd is None or req.reference_price_usd <= 0:
            warnings.append("reference_price_missing_for_limit_sanity")
        else:
            deviation = abs(req.limit_price_usd - req.reference_price_usd) / req.reference_price_usd * 100.0
            if deviation > _MAX_LIMIT_DEVIATION_PCT:
                blocked.append("limit_price_deviation_exceeds_bound")

        status = DualPaperBrokerStatus.BLOCKED if blocked else DualPaperBrokerStatus.PREVIEWED
        return BrokerPreviewResult(
            account_scope=self.account_scope,
            status=status,
            blocked_reasons=blocked,
            warnings=warnings,
            quantity=req.quantity,
            limit_price_usd=req.limit_price_usd,
            notional_usd=round(notional, 2),
            account_state=summary,
            check_details={"account_mode": "kis_mock", "broker_mutation": "disabled"},
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/us_dual_paper/test_kis_mock_preview.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/us_dual_paper/adapters/kis_mock.py tests/services/us_dual_paper/test_kis_mock_preview.py
git commit -m "feat(rob-326): KIS mock US preview gate (buy/limit, cap + buying-power + deviation)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 13: Dual packet orchestrator (per-broker isolation)

**Files:**
- Create: `app/services/us_dual_paper/packet.py`
- Test: `tests/services/us_dual_paper/test_packet_orchestrator.py`

This is the key AC: one broker's failure must NEVER change the other's status.

- [ ] **Step 1: Write the failing test (isolation is the core case)**

`tests/services/us_dual_paper/test_packet_orchestrator.py`:

```python
import pytest

from app.schemas.us_dual_paper import (
    AccountStateSummary,
    BrokerPreviewRequest,
    BrokerPreviewResult,
    DualPaperBrokerStatus,
)
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter
from app.services.us_dual_paper.packet import build_packet


class _Ok(BrokerPreviewAdapter):
    account_scope = "alpaca_paper"

    def is_enabled(self):
        return True

    def missing_env_keys(self):
        return []

    async def read_account_state(self):
        return AccountStateSummary(buying_power_usd=1000.0)

    async def preview(self, req):
        return BrokerPreviewResult(account_scope=self.account_scope, status=DualPaperBrokerStatus.PREVIEWED)


class _Boom(BrokerPreviewAdapter):
    account_scope = "kis_mock"

    def is_enabled(self):
        return True

    def missing_env_keys(self):
        return []

    async def read_account_state(self):
        raise RuntimeError("kis down")

    async def preview(self, req):
        raise RuntimeError("kis down")


class _Disabled(BrokerPreviewAdapter):
    account_scope = "kis_mock"

    def is_enabled(self):
        return False

    def missing_env_keys(self):
        return ["KIS_MOCK_ENABLED"]

    async def read_account_state(self):
        raise AssertionError("must not be called when disabled")

    async def preview(self, req):
        raise AssertionError("must not be called when disabled")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_broker_error_does_not_collapse_the_other():
    packet = await build_packet(
        symbol="NVDA",
        quantity=1,
        limit_price_usd=10.0,
        notional_cap_usd=50.0,
        limit_price_source="operator_input",
        adapters=[_Ok(), _Boom()],
    )
    assert packet.submit_enabled is False
    assert packet.brokers["alpaca_paper"].status is DualPaperBrokerStatus.PREVIEWED
    assert packet.brokers["kis_mock"].status is DualPaperBrokerStatus.ERROR
    assert packet.brokers["kis_mock"].reason == "RuntimeError"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disabled_broker_is_unsupported_other_previewed():
    packet = await build_packet(
        symbol="NVDA",
        quantity=1,
        limit_price_usd=10.0,
        notional_cap_usd=50.0,
        limit_price_source="operator_input",
        adapters=[_Ok(), _Disabled()],
    )
    assert packet.brokers["alpaca_paper"].status is DualPaperBrokerStatus.PREVIEWED
    kis = packet.brokers["kis_mock"]
    assert kis.status is DualPaperBrokerStatus.UNSUPPORTED
    assert kis.blocked_reasons == [] and "KIS_MOCK_ENABLED" in (kis.reason or "")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/us_dual_paper/test_packet_orchestrator.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`app/services/us_dual_paper/packet.py`:

```python
"""Dual-broker preview orchestrator (ROB-326). Each broker isolated; submit disabled."""

from __future__ import annotations

from app.schemas.us_dual_paper import (
    BrokerPreviewRequest,
    BrokerPreviewResult,
    DualBrokerPreviewPacket,
    DualPaperBrokerStatus,
)
from app.services.us_dual_paper.adapters.alpaca import AlpacaPaperAdapter
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter
from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter


def default_adapters() -> list[BrokerPreviewAdapter]:
    return [KisMockUsAdapter(), AlpacaPaperAdapter()]


async def _preview_one(adapter: BrokerPreviewAdapter, req: BrokerPreviewRequest) -> BrokerPreviewResult:
    if not adapter.is_enabled():
        return BrokerPreviewResult(
            account_scope=adapter.account_scope,
            status=DualPaperBrokerStatus.UNSUPPORTED,
            reason="missing_env_keys: " + ", ".join(adapter.missing_env_keys()),
        )
    try:
        return await adapter.preview(req)
    except Exception as exc:  # isolation boundary — never propagates to other brokers
        return BrokerPreviewResult(
            account_scope=adapter.account_scope,
            status=DualPaperBrokerStatus.ERROR,
            reason=type(exc).__name__,
        )


async def build_packet(
    *,
    symbol: str,
    quantity: float,
    limit_price_usd: float,
    notional_cap_usd: float,
    limit_price_source: str,
    reference_price_usd: float | None = None,
    adapters: list[BrokerPreviewAdapter] | None = None,
) -> DualBrokerPreviewPacket:
    adapters = adapters if adapters is not None else default_adapters()
    req = BrokerPreviewRequest(
        symbol=symbol,
        quantity=quantity,
        limit_price_usd=limit_price_usd,
        notional_cap_usd=notional_cap_usd,
        reference_price_usd=reference_price_usd,
    )
    brokers: dict[str, BrokerPreviewResult] = {}
    for adapter in adapters:  # sequential; each fully isolated
        brokers[adapter.account_scope] = await _preview_one(adapter, req)
    return DualBrokerPreviewPacket(
        symbol=symbol,
        limit_price_source=limit_price_source,
        notional_cap_usd=notional_cap_usd,
        submit_enabled=False,
        brokers=brokers,
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/us_dual_paper/test_packet_orchestrator.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/us_dual_paper/packet.py tests/services/us_dual_paper/test_packet_orchestrator.py
git commit -m "feat(rob-326): dual-broker preview orchestrator with per-broker isolation

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 14: Preview MCP tool

**Files:**
- Modify: `app/mcp_server/tooling/us_dual_paper.py` (add `us_dual_paper_preview`)
- Test: `tests/services/us_dual_paper/test_mcp_preview_tool.py`

- [ ] **Step 1: Write the failing test**

`tests/services/us_dual_paper/test_mcp_preview_tool.py`:

```python
import pytest

from app.mcp_server.tooling import us_dual_paper as tool_mod
from app.mcp_server.tooling.us_dual_paper import US_DUAL_PAPER_TOOL_NAMES, us_dual_paper_preview


@pytest.mark.unit
def test_preview_tool_name_registered():
    assert "us_dual_paper_preview" in US_DUAL_PAPER_TOOL_NAMES


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_tool_returns_per_broker_packet(monkeypatch):
    from app.schemas.us_dual_paper import (
        BrokerPreviewResult,
        DualBrokerPreviewPacket,
        DualPaperBrokerStatus,
    )

    async def _fake_build(**kwargs):
        return DualBrokerPreviewPacket(
            symbol=kwargs["symbol"],
            limit_price_source=kwargs["limit_price_source"],
            notional_cap_usd=kwargs["notional_cap_usd"],
            brokers={
                "alpaca_paper": BrokerPreviewResult(
                    account_scope="alpaca_paper", status=DualPaperBrokerStatus.PREVIEWED
                ),
                "kis_mock": BrokerPreviewResult(
                    account_scope="kis_mock", status=DualPaperBrokerStatus.BLOCKED
                ),
            },
        )

    monkeypatch.setattr(tool_mod, "build_packet", _fake_build)
    out = await us_dual_paper_preview(symbol="NVDA", quantity=1, limit_price_usd=10.0, notional_cap_usd=50.0)
    assert out["submit_enabled"] is False
    assert out["brokers"]["alpaca_paper"]["status"] == "previewed"
    assert out["brokers"]["kis_mock"]["status"] == "blocked"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/us_dual_paper/test_mcp_preview_tool.py -v`
Expected: FAIL — `ImportError: cannot import name 'us_dual_paper_preview'`

- [ ] **Step 3: Implement**

In `app/mcp_server/tooling/us_dual_paper.py`:
- add `from app.services.us_dual_paper.packet import build_packet`
- add `"us_dual_paper_preview"` to `US_DUAL_PAPER_TOOL_NAMES`
- add the function + register it:

```python
async def us_dual_paper_preview(
    symbol: str,
    quantity: float,
    limit_price_usd: float,
    notional_cap_usd: float = 50.0,
    reference_price_usd: float | None = None,
    limit_price_source: str = "operator_input",
) -> dict[str, Any]:
    """Generate a dual-broker (kis_mock + alpaca_paper) BUY/LIMIT preview packet.

    Read-only. submit_enabled is always False. Each broker reported independently
    as previewed/blocked/unsupported/error. Never submits, cancels, or modifies.
    """
    packet = await build_packet(
        symbol=symbol,
        quantity=quantity,
        limit_price_usd=limit_price_usd,
        notional_cap_usd=notional_cap_usd,
        limit_price_source=limit_price_source,
        reference_price_usd=reference_price_usd,
    )
    return packet.model_dump(mode="json")
```

And in `register_us_dual_paper_tools`:

```python
    _ = mcp.tool(
        name="us_dual_paper_preview",
        description=(
            "Dual-broker BUY/LIMIT preview packet for KIS mock US + Alpaca Paper. "
            "Read-only, submit_enabled always False; each broker reported independently "
            "(previewed/blocked/unsupported/error). No submit/cancel/modify."
        ),
    )(us_dual_paper_preview)
```

Add `us_dual_paper_preview` to `__all__`.

- [ ] **Step 4: Run tests + registry import**

Run: `uv run pytest tests/services/us_dual_paper/test_mcp_preview_tool.py -v`
Expected: PASS (2 passed)
Run: `uv run python -c "from app.mcp_server.tooling.registry import register_all_tools; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/us_dual_paper.py tests/services/us_dual_paper/test_mcp_preview_tool.py
git commit -m "feat(rob-326): us_dual_paper_preview MCP tool (read-only dual packet)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 15: Full smoke CLI (`--mode preview`)

**Files:**
- Modify: `scripts/smoke/us_dual_paper_preview_smoke.py` (add `preview` mode)
- Test: `tests/services/us_dual_paper/test_smoke_cli_preview.py`

- [ ] **Step 1: Write the failing test**

`tests/services/us_dual_paper/test_smoke_cli_preview.py`:

```python
import pytest

from scripts.smoke import us_dual_paper_preview_smoke as smoke


@pytest.mark.unit
def test_preview_mode_emits_packet(monkeypatch, capsys):
    monkeypatch.setenv("US_DUAL_PAPER_PREVIEW_ENABLED", "true")

    async def _fake_build(**kwargs):
        from app.schemas.us_dual_paper import (
            BrokerPreviewResult,
            DualBrokerPreviewPacket,
            DualPaperBrokerStatus,
        )

        return DualBrokerPreviewPacket(
            symbol=kwargs["symbol"],
            limit_price_source=kwargs["limit_price_source"],
            notional_cap_usd=kwargs["notional_cap_usd"],
            brokers={
                "alpaca_paper": BrokerPreviewResult(
                    account_scope="alpaca_paper", status=DualPaperBrokerStatus.PREVIEWED
                ),
            },
        )

    monkeypatch.setattr(smoke, "build_packet", _fake_build)
    rc = smoke.main(["--mode", "preview", "--symbol", "NVDA", "--quantity", "1",
                     "--limit-price", "10.0", "--notional-cap", "50"])
    assert rc == 0
    assert '"submit_enabled": false' in capsys.readouterr().out.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/us_dual_paper/test_smoke_cli_preview.py -v`
Expected: FAIL — argparse rejects `preview` choice / no `build_packet` symbol

- [ ] **Step 3: Implement**

In `scripts/smoke/us_dual_paper_preview_smoke.py`:
- add `from app.services.us_dual_paper.packet import build_packet`
- extend `build_parser`: `choices=["preflight", "preview"]`, add `--symbol`, `--quantity` (float), `--limit-price` (float), `--notional-cap` (float, default 50.0), `--reference-price` (float, optional), `--limit-price-source` (default "operator_input").
- add an async `_run_preview(args)`:

```python
async def _run_preview(args) -> int:
    packet = await build_packet(
        symbol=args.symbol,
        quantity=args.quantity,
        limit_price_usd=args.limit_price,
        notional_cap_usd=args.notional_cap,
        limit_price_source=args.limit_price_source,
        reference_price_usd=args.reference_price,
    )
    _emit(packet.model_dump(mode="json"))
    return 0
```

- in `main`, dispatch: `if args.mode == "preview": return asyncio.run(_run_preview(args))`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/us_dual_paper/test_smoke_cli_preview.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke/us_dual_paper_preview_smoke.py tests/services/us_dual_paper/test_smoke_cli_preview.py
git commit -m "feat(rob-326): smoke CLI preview mode (dual packet, default-disabled)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 16: Runbook (incl. 22:30 KST confirm-gated handoff)

**Files:**
- Create: `docs/runbooks/us-dual-paper-premarket-preview.md`

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/us-dual-paper-premarket-preview.md` covering:

1. **Purpose & safety** — paper/mock only; **not a live-trading recommendation**; submit is default-disabled and not implemented in this path.
2. **Enablement** — `US_DUAL_PAPER_PREVIEW_ENABLED=true`; required creds per broker (`ALPACA_PAPER_API_KEY/SECRET`, `KIS_MOCK_ENABLED/APP_KEY/APP_SECRET/ACCOUNT_NO`) — names only.
3. **Preflight**:
   ```bash
   US_DUAL_PAPER_PREVIEW_ENABLED=true uv run python -m scripts.smoke.us_dual_paper_preview_smoke --mode preflight
   ```
   Interpret exit codes (0 ok / 1 creds / 2 runtime) and the `missing_env_keys` lines.
4. **Preview** for 1–3 symbols:
   ```bash
   US_DUAL_PAPER_PREVIEW_ENABLED=true uv run python -m scripts.smoke.us_dual_paper_preview_smoke \
     --mode preview --symbol NVDA --quantity 1 --limit-price 100 --notional-cap 50
   ```
   Explain the per-broker `previewed/blocked/unsupported/error` semantics and that `submit_enabled` is always `false`.
5. **MCP equivalent** — `us_dual_paper_capability_matrix`, `us_dual_paper_account_states`, `us_dual_paper_preview` (read-only).
6. **Manual operator review checklist** — confirm limit price source, notional cap ≤ intended, both brokers' buying power.
7. **22:30 KST handoff (NOT implemented in this issue)** — explicitly state: the first regular-session paper smoke uses the existing **separate, confirm-gated** broker submit tools (Alpaca `alpaca_paper_submit_order`; KIS mock executor) — they are NOT wired into this preview path. Document, as a manual operator procedure: required confirm flags, a single small order, how to read the fill, and **rollback/cancel** steps (Alpaca `alpaca_paper_cancel_order`; KIS mock cancel). Block submit if quote/session data is stale or unavailable.
8. **Stale-quote rule** — if no fresh reference price, preview emits `reference_price_missing_for_limit_sanity` and submit must remain blocked.

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/us-dual-paper-premarket-preview.md
git commit -m "docs(rob-326): US dual-paper premarket preview runbook + 22:30 handoff

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 17: PR2 verification + push

- [ ] **Step 1: Full package suite + lint + import guard + registry**

```bash
uv run pytest tests/services/us_dual_paper/ -v
uv run ruff check app/ tests/ scripts/
uv run pytest tests/services/us_dual_paper/test_no_mutation_imports.py -v
uv run python -c "from app.mcp_server.tooling.registry import register_all_tools; print('registry ok')"
```
Expected: all green.

- [ ] **Step 2: Open PR2** (base = PR1 branch if stacked, else main after PR1 merges)

```bash
gh pr create --base main --title "feat(rob-326): US dual-paper premarket — preview gates + dual packet + runbook (PR2)" --body "$(cat <<'EOF'
Implements ROB-326 PR2 (read-only, default-disabled).

- Alpaca paper preview adapter (buy/limit, cap + buying-power)
- KIS mock US preview gate (buy/limit, cap + buying-power + deviation; is_mock pinned)
- dual-broker orchestrator with strict per-broker isolation (one failure never collapses the other)
- `us_dual_paper_preview` MCP tool
- smoke CLI `--mode preview`
- runbook incl. 22:30 KST confirm-gated handoff + stale-quote block

No submit/cancel/modify path. No scheduler/Prefect/frontend.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

> Confirm Test workflow green + `ruff` clean before merge (pre-merge full-CI gate).

---

## Acceptance criteria → task map

| ROB-326 AC | Task |
|---|---|
| Read both broker account states, no secrets | 5, 6, 7, 8 |
| Dual-broker preview/preflight for 1–3 symbols | 11, 12, 13, 14, 15 |
| Independent `previewed/blocked/unsupported/error` + reason | 13 (isolation test), 2 |
| Position/open-order checks where available | 5, 6 (`AccountStateSummary`) |
| Default path before 22:30 = preview only, cannot submit | 1, 9 (guard), `submit_enabled=False` everywhere |
| Regular-session handoff runbook (confirm flags, rollback/cancel) | 16 |
| Tests cover normalization/fail-closed without broker mutation | 9, 13, all fakes |
| Docs state paper/mock only, no live recommendation | 16 |

## Out of scope (deferred, documented in runbook)
- Confirm-gated submit code, scheduler/Prefect/TaskIQ, frontend, KIS live, live US quote auto-fetch.
