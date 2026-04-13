# Paper Trading 다중 전략 + Trade Journal 연동 구현 플랜

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paper 매매 시 투자 논거(thesis)를 Trade Journal에 자동 기록하고, 전략 간 성과 비교 및 실전 전환 추천 도구를 제공한다.

**Architecture:** 기존 `review.trade_journals` 테이블에 `account_type`/`paper_trade_id` 컬럼을 추가하여 paper journal을 통합 저장한다. 새 `paper_journal_bridge.py` 모듈이 paper 주문↔journal 연동, 전략 비교, 실전 전환 추천을 담당한다. 기존 MCP 도구 이름은 유지하고 파라미터만 확장한다.

**Tech Stack:** Python 3.13, SQLAlchemy (async), Alembic, FastMCP, pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-04-13-paper-strategy-journal-integration-design.md`

---

## 파일 구조

| 파일 | 유형 | 책임 |
|------|------|------|
| `app/models/trade_journal.py` | 수정 | `account_type`, `paper_trade_id` 컬럼, constraints |
| `alembic/versions/xxxx_add_paper_journal_fields.py` | 신규 | 마이그레이션 |
| `app/mcp_server/tooling/paper_journal_bridge.py` | 신규 | create/close/compare/recommend 4개 함수 |
| `app/mcp_server/tooling/paper_journal_registration.py` | 신규 | compare_strategies, recommend_go_live MCP 등록 |
| `app/mcp_server/tooling/paper_account_registration.py` | 수정 | strategy_name 파라미터 노출 |
| `app/services/paper_trading_service.py` | 수정 | list_accounts에 strategy_name 필터 |
| `app/mcp_server/tooling/paper_order_handler.py` | 수정 | thesis/strategy 파라미터 + bridge 호출 |
| `app/mcp_server/tooling/orders_registration.py` | 수정 | paper 경로에 journal 파라미터 전달 |
| `app/mcp_server/tooling/trade_journal_tools.py` | 수정 | account_type, paper_trade_id 지원 |
| `app/mcp_server/tooling/trade_journal_registration.py` | 수정 | MCP description 업데이트 |
| `app/mcp_server/tooling/registry.py` | 수정 | paper_journal 등록 추가 |
| `tests/test_paper_journal_bridge.py` | 신규 | bridge 단위 테스트 |
| `tests/test_paper_strategy_mcp.py` | 신규 | MCP 도구 변경 테스트 |

---

## Task 1: TradeJournal 모델에 account_type, paper_trade_id 추가

**Files:**
- Modify: `app/models/trade_journal.py`
- Test: `tests/test_trade_journal_model.py`

- [ ] **Step 1: 기존 모델 테스트 읽기**

Run: `uv run pytest tests/test_trade_journal_model.py -v`
Expected: 기존 테스트 모두 PASS (baseline)

- [ ] **Step 2: account_type/paper_trade_id 모델 테스트 작성**

`tests/test_trade_journal_model.py`에 추가:

```python
class TestAccountTypeField:
    """account_type 및 paper_trade_id 필드 테스트."""

    def test_default_account_type_is_live(self):
        journal = TradeJournal(
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            thesis="Test thesis",
        )
        assert journal.account_type == "live"

    def test_paper_account_type(self):
        journal = TradeJournal(
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            thesis="Test thesis",
            account_type="paper",
            paper_trade_id=42,
            account="paper-momentum",
        )
        assert journal.account_type == "paper"
        assert journal.paper_trade_id == 42

    def test_live_journal_paper_trade_id_is_none(self):
        journal = TradeJournal(
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            thesis="Test thesis",
        )
        assert journal.paper_trade_id is None
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `uv run pytest tests/test_trade_journal_model.py::TestAccountTypeField -v`
Expected: FAIL — `account_type` attribute 없음

- [ ] **Step 4: TradeJournal 모델에 컬럼 추가**

`app/models/trade_journal.py` 수정 — `__table_args__`에 constraints 추가, 클래스 body에 컬럼 추가:

```python
# __table_args__에 추가:
CheckConstraint(
    "account_type IN ('live','paper')",
    name="trade_journals_account_type",
),
CheckConstraint(
    "NOT (account_type = 'live' AND paper_trade_id IS NOT NULL)",
    name="trade_journals_no_paper_trade_on_live",
),
Index("ix_trade_journals_account_type", "account_type"),

# 클래스 body에 추가 (account 필드 아래):
account_type: Mapped[str] = mapped_column(
    Text, nullable=False, default="live", server_default="live"
)
paper_trade_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
```

`__init__`에 기본값 추가:

```python
def __init__(self, **kwargs: Any) -> None:
    kwargs.setdefault("side", "buy")
    kwargs.setdefault("status", "draft")
    kwargs.setdefault("account_type", "live")
    super().__init__(**kwargs)
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `uv run pytest tests/test_trade_journal_model.py -v`
Expected: 기존 테스트 + 새 테스트 모두 PASS

- [ ] **Step 6: 커밋**

```bash
git add app/models/trade_journal.py tests/test_trade_journal_model.py
git commit -m "feat(journal): add account_type and paper_trade_id columns to TradeJournal model"
```

---

## Task 2: Alembic 마이그레이션 생성 및 적용

**Files:**
- Create: `alembic/versions/xxxx_add_paper_journal_fields.py`

- [ ] **Step 1: 마이그레이션 자동 생성**

Run: `uv run alembic revision --autogenerate -m "add account_type and paper_trade_id to trade_journals"`

- [ ] **Step 2: 생성된 마이그레이션 파일 검토**

생성된 파일에 아래 내용이 포함되어야 함:

```python
def upgrade() -> None:
    op.add_column(
        "trade_journals",
        sa.Column(
            "account_type",
            sa.Text(),
            nullable=False,
            server_default="live",
        ),
        schema="review",
    )
    op.add_column(
        "trade_journals",
        sa.Column("paper_trade_id", sa.BigInteger(), nullable=True),
        schema="review",
    )
    op.create_check_constraint(
        "trade_journals_account_type",
        "trade_journals",
        "account_type IN ('live','paper')",
        schema="review",
    )
    op.create_check_constraint(
        "trade_journals_no_paper_trade_on_live",
        "trade_journals",
        "NOT (account_type = 'live' AND paper_trade_id IS NOT NULL)",
        schema="review",
    )
    op.create_index(
        "ix_trade_journals_account_type",
        "trade_journals",
        ["account_type"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_journals_account_type",
        table_name="trade_journals",
        schema="review",
    )
    op.drop_constraint(
        "trade_journals_no_paper_trade_on_live",
        "trade_journals",
        schema="review",
    )
    op.drop_constraint(
        "trade_journals_account_type",
        "trade_journals",
        schema="review",
    )
    op.drop_column("trade_journals", "paper_trade_id", schema="review")
    op.drop_column("trade_journals", "account_type", schema="review")
```

autogenerate가 constraint/index를 누락하면 수동으로 추가한다.

- [ ] **Step 3: 마이그레이션 적용 (로컬)**

Run: `uv run alembic upgrade head`
Expected: 성공, 새 revision이 current로 표시

- [ ] **Step 4: 커밋**

```bash
git add alembic/versions/
git commit -m "migration: add account_type and paper_trade_id to trade_journals"
```

---

## Task 3: _serialize_journal 및 trade_journal_tools에 account_type 지원 추가

**Files:**
- Modify: `app/mcp_server/tooling/trade_journal_tools.py`
- Test: `tests/test_mcp_trade_journal.py`

- [ ] **Step 1: serialize 테스트 작성**

`tests/test_mcp_trade_journal.py`에 추가:

```python
class TestSerializeJournalNewFields:
    """account_type, paper_trade_id 직렬화 테스트."""

    def test_serialize_live_journal(self):
        journal = TradeJournal(
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            thesis="Test",
            account_type="live",
        )
        journal.id = 1
        journal.created_at = now_kst()
        journal.updated_at = now_kst()
        result = _serialize_journal(journal)
        assert result["account_type"] == "live"
        assert result["paper_trade_id"] is None

    def test_serialize_paper_journal(self):
        journal = TradeJournal(
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            thesis="Test",
            account_type="paper",
            paper_trade_id=42,
            account="paper-momentum",
        )
        journal.id = 2
        journal.created_at = now_kst()
        journal.updated_at = now_kst()
        result = _serialize_journal(journal)
        assert result["account_type"] == "paper"
        assert result["paper_trade_id"] == 42
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_mcp_trade_journal.py::TestSerializeJournalNewFields -v`
Expected: FAIL — `account_type` key 없음

- [ ] **Step 3: _serialize_journal에 새 필드 추가**

`app/mcp_server/tooling/trade_journal_tools.py`의 `_serialize_journal()` 함수에 추가:

```python
"account_type": j.account_type,
"paper_trade_id": j.paper_trade_id,
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_mcp_trade_journal.py::TestSerializeJournalNewFields -v`
Expected: PASS

- [ ] **Step 5: save_trade_journal 검증 테스트 작성**

`tests/test_mcp_trade_journal.py`에 추가:

```python
class TestSaveTradeJournalAccountType:
    """save_trade_journal account_type 검증 테스트."""

    @pytest.mark.asyncio
    async def test_save_with_default_account_type(self, monkeypatch):
        """기본 account_type은 'live'."""
        saved_journal = None

        async def _capture_add(session):
            nonlocal saved_journal
            original_add = session.add

            def capturing_add(obj):
                nonlocal saved_journal
                if isinstance(obj, TradeJournal):
                    saved_journal = obj
                return original_add(obj)

            session.add = capturing_add
            return session

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        factory = MagicMock(return_value=cm)
        monkeypatch.setattr(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            lambda: factory,
        )

        result = await save_trade_journal(
            symbol="005930",
            thesis="Test thesis",
        )
        assert result["success"] is True
        added_obj = mock_session.add.call_args[0][0]
        assert added_obj.account_type == "live"

    @pytest.mark.asyncio
    async def test_save_live_with_paper_trade_id_errors(self, monkeypatch):
        """account_type='live' + paper_trade_id 지정 시 오류."""
        result = await save_trade_journal(
            symbol="005930",
            thesis="Test thesis",
            account_type="live",
            paper_trade_id=42,
        )
        assert result["success"] is False
        assert "paper_trade_id" in result["error"]

    @pytest.mark.asyncio
    async def test_save_paper_without_account_errors(self, monkeypatch):
        """account_type='paper' + account 비어있으면 오류."""
        result = await save_trade_journal(
            symbol="005930",
            thesis="Test thesis",
            account_type="paper",
        )
        assert result["success"] is False
        assert "account" in result["error"]
```

- [ ] **Step 6: 테스트 실패 확인**

Run: `uv run pytest tests/test_mcp_trade_journal.py::TestSaveTradeJournalAccountType -v`
Expected: FAIL

- [ ] **Step 7: save_trade_journal에 account_type/paper_trade_id 파라미터 및 검증 추가**

`app/mcp_server/tooling/trade_journal_tools.py`의 `save_trade_journal()` 시그니처에 추가:

```python
async def save_trade_journal(
    symbol: str,
    thesis: str,
    side: str = "buy",
    entry_price: float | None = None,
    quantity: float | None = None,
    amount: float | None = None,
    strategy: str | None = None,
    target_price: float | None = None,
    stop_loss: float | None = None,
    min_hold_days: int | None = None,
    indicators_snapshot: dict | None = None,
    account: str | None = None,
    notes: str | None = None,
    status: str = "draft",
    account_type: str = "live",           # 추가
    paper_trade_id: int | None = None,    # 추가
) -> dict[str, Any]:
```

함수 body 시작 부분에 검증 추가:

```python
# account_type 검증
if account_type not in ("live", "paper"):
    return {"success": False, "error": f"Invalid account_type: {account_type}"}
if account_type == "live" and paper_trade_id is not None:
    return {
        "success": False,
        "error": "paper_trade_id cannot be set for live account_type",
    }
if account_type == "paper" and not account:
    return {
        "success": False,
        "error": "account is required for paper account_type",
    }
```

TradeJournal 생성 시 새 필드 전달:

```python
journal = TradeJournal(
    # ... 기존 필드 ...
    account=account,
    account_type=account_type,
    paper_trade_id=paper_trade_id,
    notes=notes,
)
```

- [ ] **Step 8: get_trade_journal 테스트 작성**

`tests/test_mcp_trade_journal.py`에 추가:

```python
class TestGetTradeJournalAccountType:
    """get_trade_journal account_type 필터 테스트."""

    @pytest.mark.asyncio
    async def test_default_returns_live_only(self, monkeypatch):
        """기본 account_type='live' — 기존 동작 유지."""
        live_journal = TradeJournal(
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            thesis="Live",
            account_type="live",
            status="active",
        )
        live_journal.id = 1
        live_journal.created_at = now_kst()
        live_journal.updated_at = now_kst()

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [live_journal]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        factory = MagicMock(return_value=cm)
        monkeypatch.setattr(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            lambda: factory,
        )

        result = await get_trade_journal()
        assert result["success"] is True
        assert len(result["entries"]) == 1
        assert result["entries"][0]["account_type"] == "live"
```

- [ ] **Step 9: get_trade_journal에 account_type 필터 추가**

`app/mcp_server/tooling/trade_journal_tools.py`의 `get_trade_journal()` 시그니처에 추가:

```python
async def get_trade_journal(
    symbol: str | None = None,
    status: str | None = None,
    market: str | None = None,
    strategy: str | None = None,
    days: int | None = None,
    include_closed: bool = False,
    limit: int = 50,
    account_type: str | None = "live",    # 추가: 기본 live만 반환
) -> dict[str, Any]:
```

filters 구성 부분에 추가:

```python
if account_type is not None:
    filters.append(TradeJournal.account_type == account_type)
```

- [ ] **Step 10: 전체 테스트 통과 확인**

Run: `uv run pytest tests/test_mcp_trade_journal.py -v`
Expected: 기존 + 새 테스트 모두 PASS

- [ ] **Step 11: 커밋**

```bash
git add app/mcp_server/tooling/trade_journal_tools.py tests/test_mcp_trade_journal.py
git commit -m "feat(journal): add account_type/paper_trade_id to save/get_trade_journal"
```

---

## Task 4: trade_journal_registration MCP description 업데이트

**Files:**
- Modify: `app/mcp_server/tooling/trade_journal_registration.py`

- [ ] **Step 1: MCP description 업데이트**

`app/mcp_server/tooling/trade_journal_registration.py` 수정:

`save_trade_journal` description에 추가:
```python
description=(
    "Save a trade journal entry with investment thesis and strategy metadata. "
    "Call this when recommending a buy/sell to record WHY. "
    "symbol auto-detects instrument_type. min_hold_days sets hold_until. "
    "status defaults to 'draft' — set to 'active' after fill confirmation. "
    "account_type='paper' for paper trading journals (requires account name). "
    "paper_trade_id links to the paper trade record."
),
```

`get_trade_journal` description에 추가:
```python
description=(
    "Query trade journals. MUST call before any sell recommendation to check "
    "thesis, hold period, target/stop prices. "
    "Returns active journals by default. "
    "Each entry includes hold_remaining_days and hold_expired. "
    "account_type defaults to 'live'; set to 'paper' for paper journals, "
    "or None to query both."
),
```

- [ ] **Step 2: 커밋**

```bash
git add app/mcp_server/tooling/trade_journal_registration.py
git commit -m "docs(journal): update MCP descriptions for account_type support"
```

---

## Task 5: PaperTradingService — strategy_name 필터 추가

**Files:**
- Modify: `app/services/paper_trading_service.py`
- Test: `tests/test_paper_trading_service.py`

- [ ] **Step 1: strategy_name 필터 테스트 작성**

`tests/test_paper_trading_service.py`에 추가:

```python
class TestListAccountsStrategyFilter:
    """list_accounts strategy_name 필터 테스트."""

    @pytest.mark.asyncio
    async def test_filter_by_strategy_name(self, mock_db):
        service = PaperTradingService(mock_db)
        momentum_account = PaperAccount(
            name="paper-momentum",
            initial_capital=Decimal("100000000"),
            cash_krw=Decimal("100000000"),
            strategy_name="momentum",
            is_active=True,
        )

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [momentum_account]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute = AsyncMock(return_value=mock_result)

        accounts = await service.list_accounts(
            is_active=True, strategy_name="momentum"
        )
        assert len(accounts) == 1
        assert accounts[0].strategy_name == "momentum"

    @pytest.mark.asyncio
    async def test_no_filter_returns_all(self, mock_db):
        service = PaperTradingService(mock_db)
        accounts_data = [
            PaperAccount(
                name="a",
                initial_capital=Decimal("100000000"),
                cash_krw=Decimal("100000000"),
                strategy_name="momentum",
                is_active=True,
            ),
            PaperAccount(
                name="b",
                initial_capital=Decimal("100000000"),
                cash_krw=Decimal("100000000"),
                strategy_name=None,
                is_active=True,
            ),
        ]

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = accounts_data
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute = AsyncMock(return_value=mock_result)

        accounts = await service.list_accounts(is_active=True)
        assert len(accounts) == 2
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_paper_trading_service.py::TestListAccountsStrategyFilter -v`
Expected: FAIL — `list_accounts()` got unexpected keyword argument `strategy_name`

- [ ] **Step 3: list_accounts에 strategy_name 파라미터 추가**

`app/services/paper_trading_service.py`의 `list_accounts()` 수정:

```python
async def list_accounts(
    self,
    is_active: bool | None = True,
    strategy_name: str | None = None,
) -> list[PaperAccount]:
    stmt = select(PaperAccount)
    if is_active is not None:
        stmt = stmt.where(PaperAccount.is_active == is_active)
    if strategy_name is not None:
        stmt = stmt.where(PaperAccount.strategy_name == strategy_name)
    stmt = stmt.order_by(PaperAccount.created_at.desc())
    result = await self.db.execute(stmt)
    return list(result.scalars().all())
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_paper_trading_service.py -v`
Expected: 모두 PASS

- [ ] **Step 5: 커밋**

```bash
git add app/services/paper_trading_service.py tests/test_paper_trading_service.py
git commit -m "feat(paper): add strategy_name filter to list_accounts"
```

---

## Task 6: create_paper_account에 strategy_name 노출 + list_paper_accounts 필터

**Files:**
- Modify: `app/mcp_server/tooling/paper_account_registration.py`
- Test: `tests/test_paper_account_tools.py`

- [ ] **Step 1: strategy_name 파라미터 테스트 작성**

`tests/test_paper_account_tools.py`에 추가:

```python
class TestCreatePaperAccountStrategy:
    """create_paper_account strategy_name 테스트."""

    @pytest.mark.asyncio
    async def test_create_with_strategy_name(self, monkeypatch):
        account = PaperAccount(
            name="paper-momentum",
            initial_capital=Decimal("100000000"),
            cash_krw=Decimal("100000000"),
            strategy_name="momentum",
            is_active=True,
        )
        account.id = 1
        account.created_at = now_kst()
        account.updated_at = now_kst()

        mock_service = AsyncMock()
        mock_service.create_account = AsyncMock(return_value=account)

        mock_session = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        factory = MagicMock(return_value=cm)
        monkeypatch.setattr(
            "app.mcp_server.tooling.paper_account_registration._session_factory",
            lambda: factory,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.paper_account_registration.PaperTradingService",
            lambda db: mock_service,
        )

        result = await create_paper_account(
            name="paper-momentum",
            strategy_name="momentum",
        )
        assert result["success"] is True
        assert result["account"]["strategy_name"] == "momentum"
        mock_service.create_account.assert_awaited_once()
        call_kwargs = mock_service.create_account.call_args.kwargs
        assert call_kwargs["strategy_name"] == "momentum"


class TestListPaperAccountsStrategyFilter:
    """list_paper_accounts strategy_name 필터 테스트."""

    @pytest.mark.asyncio
    async def test_filter_by_strategy(self, monkeypatch):
        account = PaperAccount(
            name="paper-momentum",
            initial_capital=Decimal("100000000"),
            cash_krw=Decimal("100000000"),
            strategy_name="momentum",
            is_active=True,
        )
        account.id = 1
        account.created_at = now_kst()
        account.updated_at = now_kst()

        mock_service = AsyncMock()
        mock_service.list_accounts = AsyncMock(return_value=[account])
        mock_service.get_portfolio_summary = AsyncMock(
            return_value={
                "positions_count": 0,
                "total_evaluated": Decimal("0"),
                "total_pnl_pct": None,
            }
        )

        mock_session = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        factory = MagicMock(return_value=cm)
        monkeypatch.setattr(
            "app.mcp_server.tooling.paper_account_registration._session_factory",
            lambda: factory,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.paper_account_registration.PaperTradingService",
            lambda db: mock_service,
        )

        result = await list_paper_accounts(strategy_name="momentum")
        assert result["success"] is True
        mock_service.list_accounts.assert_awaited_once()
        call_kwargs = mock_service.list_accounts.call_args.kwargs
        assert call_kwargs["strategy_name"] == "momentum"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_paper_account_tools.py::TestCreatePaperAccountStrategy -v`
Expected: FAIL

- [ ] **Step 3: create_paper_account에 strategy_name 추가**

`app/mcp_server/tooling/paper_account_registration.py` 수정:

`create_paper_account` 함수:
```python
@mcp.tool(
    name="create_paper_account",
    description=(
        "Create a new paper trading (모의투자) account. "
        "initial_capital is the KRW opening balance (default 100,000,000 KRW = 1억). "
        "initial_capital_usd adds a separate USD cash balance for US equity simulation. "
        "Account name must be unique. "
        "strategy_name tags the account with a strategy (e.g. daytrading, swing, ai-signal)."
    ),
)
async def create_paper_account(
    name: str,
    initial_capital: float = 100_000_000.0,
    initial_capital_usd: float = 0.0,
    description: str | None = None,
    strategy_name: str | None = None,
) -> dict[str, Any]:
    try:
        async with _session_factory()() as db:
            service = PaperTradingService(db)
            account = await service.create_account(
                name=name,
                initial_capital_krw=Decimal(str(initial_capital)),
                initial_capital_usd=Decimal(str(initial_capital_usd)),
                description=description,
                strategy_name=strategy_name,
            )
            return {"success": True, "account": _serialize_account(account)}
    except IntegrityError:
        return {
            "success": False,
            "error": f"Paper account '{name}' already exists",
        }
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
```

`list_paper_accounts` 함수:
```python
@mcp.tool(
    name="list_paper_accounts",
    description=(
        "List paper trading accounts with per-account summary "
        "(positions_count, total_evaluated_krw, total_pnl_pct). "
        "Note: total_evaluated_krw sums KRW and USD position values verbatim "
        "— it does not convert USD to KRW. "
        "is_active=True (default) filters to active accounts only. "
        "strategy_name filters by strategy tag."
    ),
)
async def list_paper_accounts(
    is_active: bool = True,
    strategy_name: str | None = None,
) -> dict[str, Any]:
    async with _session_factory()() as db:
        service = PaperTradingService(db)
        accounts = await service.list_accounts(
            is_active=is_active,
            strategy_name=strategy_name,
        )
        # ... rest unchanged
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_paper_account_tools.py -v`
Expected: 모두 PASS

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/paper_account_registration.py tests/test_paper_account_tools.py
git commit -m "feat(paper): expose strategy_name in create/list paper account MCP tools"
```

---

## Task 7: paper_journal_bridge — create_paper_journal 구현

**Files:**
- Create: `app/mcp_server/tooling/paper_journal_bridge.py`
- Create: `tests/test_paper_journal_bridge.py`

- [ ] **Step 1: create_paper_journal 테스트 작성**

`tests/test_paper_journal_bridge.py` 생성:

```python
"""Paper Journal Bridge unit tests."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.timezone import now_kst
from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType


class TestCreatePaperJournal:
    """create_paper_journal 단위 테스트."""

    @pytest.mark.asyncio
    async def test_creates_journal_with_paper_fields(self, monkeypatch):
        from app.mcp_server.tooling import paper_journal_bridge

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()
        # No existing active journal
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        factory = MagicMock(return_value=cm)
        monkeypatch.setattr(
            paper_journal_bridge, "_session_factory", lambda: factory
        )

        result = await paper_journal_bridge.create_paper_journal(
            symbol="005930",
            instrument_type="equity_kr",
            entry_price=Decimal("72000"),
            quantity=Decimal("10"),
            amount=Decimal("720000"),
            paper_trade_id=42,
            paper_account_name="paper-momentum",
            thesis="AI signal buy",
            strategy="momentum",
            target_price=Decimal("80000"),
            stop_loss=Decimal("65000"),
            min_hold_days=7,
            notes="Test note",
        )

        assert result["success"] is True
        added_obj = mock_session.add.call_args[0][0]
        assert isinstance(added_obj, TradeJournal)
        assert added_obj.account_type == "paper"
        assert added_obj.paper_trade_id == 42
        assert added_obj.account == "paper-momentum"
        assert added_obj.status == "active"
        assert added_obj.side == "buy"
        assert added_obj.thesis == "AI signal buy"
        assert added_obj.strategy == "momentum"
        assert added_obj.notes == "Test note"
        assert added_obj.hold_until is not None

    @pytest.mark.asyncio
    async def test_thesis_required(self, monkeypatch):
        from app.mcp_server.tooling import paper_journal_bridge

        result = await paper_journal_bridge.create_paper_journal(
            symbol="005930",
            instrument_type="equity_kr",
            entry_price=Decimal("72000"),
            quantity=Decimal("10"),
            amount=Decimal("720000"),
            paper_trade_id=42,
            paper_account_name="paper-momentum",
            thesis="",
        )
        assert result["success"] is False
        assert "thesis" in result["error"]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py::TestCreatePaperJournal -v`
Expected: FAIL — module not found

- [ ] **Step 3: paper_journal_bridge.py 생성 — create_paper_journal 구현**

`app/mcp_server/tooling/paper_journal_bridge.py` 생성:

```python
"""Paper Journal Bridge — paper trading ↔ trade journal 연동.

Paper 주문 결과를 journal 도메인에 반영하는 어댑터 계층.
주문 체결은 order handler/service가 담당하고,
이 모듈은 journal create/close/compare/recommend만 책임진다.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType

logger = logging.getLogger(__name__)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


async def create_paper_journal(
    *,
    symbol: str,
    instrument_type: str,
    entry_price: Decimal,
    quantity: Decimal,
    amount: Decimal,
    paper_trade_id: int,
    paper_account_name: str,
    thesis: str,
    strategy: str | None = None,
    target_price: Decimal | None = None,
    stop_loss: Decimal | None = None,
    min_hold_days: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Create a TradeJournal entry for a paper buy execution.

    Called automatically after a paper buy order with thesis.
    Status is set to 'active' (paper orders are instant fills — skip draft).
    """
    thesis = (thesis or "").strip()
    if not thesis:
        return {"success": False, "error": "thesis is required"}

    hold_until = None
    if min_hold_days is not None and min_hold_days > 0:
        hold_until = now_kst() + timedelta(days=min_hold_days)

    try:
        async with _session_factory()() as db:
            journal = TradeJournal(
                symbol=symbol,
                instrument_type=InstrumentType(instrument_type),
                side="buy",
                entry_price=entry_price,
                quantity=quantity,
                amount=amount,
                thesis=thesis,
                strategy=strategy,
                target_price=target_price,
                stop_loss=stop_loss,
                min_hold_days=min_hold_days,
                hold_until=hold_until,
                status=JournalStatus.active,
                account_type="paper",
                paper_trade_id=paper_trade_id,
                account=paper_account_name,
                notes=notes,
            )
            db.add(journal)
            await db.commit()
            await db.refresh(journal)

            return {
                "success": True,
                "action": "created",
                "journal_id": journal.id,
            }
    except Exception as exc:
        logger.exception("create_paper_journal failed")
        return {"success": False, "error": f"create_paper_journal failed: {exc}"}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py::TestCreatePaperJournal -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/paper_journal_bridge.py tests/test_paper_journal_bridge.py
git commit -m "feat(paper): implement create_paper_journal in paper_journal_bridge"
```

---

## Task 8: paper_journal_bridge — close_paper_journal (FIFO) 구현

**Files:**
- Modify: `app/mcp_server/tooling/paper_journal_bridge.py`
- Modify: `tests/test_paper_journal_bridge.py`

- [ ] **Step 1: close_paper_journal 테스트 작성**

`tests/test_paper_journal_bridge.py`에 추가:

```python
class TestClosePaperJournal:
    """close_paper_journal FIFO 정책 테스트."""

    @pytest.mark.asyncio
    async def test_closes_active_journal(self, monkeypatch):
        from app.mcp_server.tooling import paper_journal_bridge

        journal = TradeJournal(
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            thesis="Test",
            entry_price=Decimal("72000"),
            account_type="paper",
            account="paper-momentum",
            status="active",
        )
        journal.id = 1
        journal.created_at = now_kst()
        journal.updated_at = now_kst()

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = journal
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        factory = MagicMock(return_value=cm)
        monkeypatch.setattr(
            paper_journal_bridge, "_session_factory", lambda: factory
        )

        result = await paper_journal_bridge.close_paper_journal(
            symbol="005930",
            exit_price=Decimal("80000"),
            exit_reason="Target reached",
            paper_account_name="paper-momentum",
        )

        assert result is not None
        assert result["success"] is True
        assert journal.status == "closed"
        assert journal.exit_price == Decimal("80000")
        assert journal.exit_reason == "Target reached"
        assert journal.exit_date is not None
        # pnl_pct: (80000/72000 - 1) * 100 ≈ 11.11%
        assert journal.pnl_pct is not None
        assert float(journal.pnl_pct) > 11.0

    @pytest.mark.asyncio
    async def test_returns_none_when_no_journal(self, monkeypatch):
        from app.mcp_server.tooling import paper_journal_bridge

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        factory = MagicMock(return_value=cm)
        monkeypatch.setattr(
            paper_journal_bridge, "_session_factory", lambda: factory
        )

        result = await paper_journal_bridge.close_paper_journal(
            symbol="005930",
            exit_price=Decimal("80000"),
            paper_account_name="paper-momentum",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_fifo_closes_oldest_first(self, monkeypatch):
        """FIFO: created_at ASC 기준 가장 오래된 journal을 close."""
        from app.mcp_server.tooling import paper_journal_bridge

        old_journal = TradeJournal(
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            thesis="First buy",
            entry_price=Decimal("70000"),
            account_type="paper",
            account="paper-momentum",
            status="active",
        )
        old_journal.id = 1
        old_journal.created_at = now_kst()
        old_journal.updated_at = now_kst()

        # Query returns oldest first (order_by created_at ASC, limit 1)
        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = old_journal
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        factory = MagicMock(return_value=cm)
        monkeypatch.setattr(
            paper_journal_bridge, "_session_factory", lambda: factory
        )

        result = await paper_journal_bridge.close_paper_journal(
            symbol="005930",
            exit_price=Decimal("75000"),
            paper_account_name="paper-momentum",
        )

        assert result is not None
        assert old_journal.status == "closed"
        assert old_journal.id == 1  # oldest was closed
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py::TestClosePaperJournal -v`
Expected: FAIL — `close_paper_journal` not defined

- [ ] **Step 3: close_paper_journal 구현**

`app/mcp_server/tooling/paper_journal_bridge.py`에 추가:

```python
async def close_paper_journal(
    *,
    symbol: str,
    exit_price: Decimal,
    exit_reason: str | None = None,
    paper_account_name: str,
) -> dict[str, Any] | None:
    """Close the oldest active paper journal for a symbol (FIFO policy).

    Called automatically after a paper sell order.
    Returns None if no active paper journal exists (not an error —
    the position may have been opened without thesis).
    """
    try:
        async with _session_factory()() as db:
            # FIFO: oldest active journal first (created_at ASC)
            stmt = (
                select(TradeJournal)
                .where(
                    TradeJournal.symbol == symbol,
                    TradeJournal.account_type == "paper",
                    TradeJournal.account == paper_account_name,
                    TradeJournal.status == JournalStatus.active,
                )
                .order_by(TradeJournal.created_at.asc())
                .limit(1)
            )
            result = await db.execute(stmt)
            journal = result.scalars().first()

            if journal is None:
                return None

            journal.status = JournalStatus.closed
            journal.exit_price = exit_price
            journal.exit_date = now_kst()
            journal.exit_reason = exit_reason

            if journal.entry_price and journal.entry_price > 0:
                pnl = (exit_price / journal.entry_price - 1) * Decimal("100")
                journal.pnl_pct = round(pnl, 4)

            await db.commit()
            await db.refresh(journal)

            return {
                "success": True,
                "action": "closed",
                "journal_id": journal.id,
                "pnl_pct": float(journal.pnl_pct) if journal.pnl_pct else None,
            }
    except Exception as exc:
        logger.exception("close_paper_journal failed")
        return {"success": False, "error": f"close_paper_journal failed: {exc}"}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py -v`
Expected: 모두 PASS

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/paper_journal_bridge.py tests/test_paper_journal_bridge.py
git commit -m "feat(paper): implement close_paper_journal with FIFO policy"
```

---

## Task 9: paper_order_handler에 journal 연동 통합

**Files:**
- Modify: `app/mcp_server/tooling/paper_order_handler.py`
- Modify: `app/mcp_server/tooling/orders_registration.py`
- Test: `tests/test_paper_order_handler.py`

- [ ] **Step 1: paper_order_handler journal 연동 테스트 작성**

`tests/test_paper_order_handler.py`에 추가:

```python
class TestPaperOrderJournalIntegration:
    """place_paper_order → journal 자동 생성/close 테스트."""

    @pytest.mark.asyncio
    async def test_buy_with_thesis_creates_journal(self, monkeypatch):
        from app.mcp_server.tooling import paper_order_handler

        # Mock service.execute_order
        mock_execution = {
            "success": True,
            "dry_run": False,
            "account_id": 1,
            "preview": {
                "symbol": "005930",
                "instrument_type": "equity_kr",
                "side": "buy",
                "order_type": "market",
                "quantity": Decimal("10"),
                "price": Decimal("72000"),
                "gross": Decimal("720000"),
                "fee": Decimal("108"),
                "total_cost": Decimal("720108"),
                "currency": "KRW",
            },
            "execution": {
                "symbol": "005930",
                "instrument_type": "equity_kr",
                "side": "buy",
                "order_type": "market",
                "quantity": Decimal("10"),
                "price": Decimal("72000"),
                "gross": Decimal("720000"),
                "fee": Decimal("108"),
                "total_cost": Decimal("720108"),
                "currency": "KRW",
                "realized_pnl": None,
                "executed_at": now_kst(),
            },
        }
        mock_account = MagicMock()
        mock_account.name = "default"
        mock_account.id = 1

        mock_service = AsyncMock()
        mock_service.execute_order = AsyncMock(return_value=mock_execution)
        mock_service.get_account_by_name = AsyncMock(return_value=mock_account)

        monkeypatch.setattr(
            paper_order_handler, "PaperTradingService", lambda db: mock_service
        )
        mock_session = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        monkeypatch.setattr(
            paper_order_handler, "AsyncSessionLocal", lambda: cm
        )

        mock_create_journal = AsyncMock(
            return_value={"success": True, "journal_id": 1}
        )
        monkeypatch.setattr(
            paper_order_handler, "create_paper_journal", mock_create_journal
        )

        result = await paper_order_handler._place_paper_order(
            symbol="005930",
            side="buy",
            order_type="market",
            quantity=10.0,
            price=None,
            amount=None,
            dry_run=False,
            reason="AI signal",
            paper_account_name=None,
            thesis="AI signal buy thesis",
            strategy="momentum",
            target_price=80000.0,
            stop_loss=65000.0,
            min_hold_days=7,
            notes="Test note",
        )

        assert result["success"] is True
        mock_create_journal.assert_awaited_once()
        call_kwargs = mock_create_journal.call_args.kwargs
        assert call_kwargs["thesis"] == "AI signal buy thesis"
        assert call_kwargs["strategy"] == "momentum"
        assert call_kwargs["paper_account_name"] == "default"

    @pytest.mark.asyncio
    async def test_buy_without_thesis_skips_journal(self, monkeypatch):
        from app.mcp_server.tooling import paper_order_handler

        mock_execution = {
            "success": True,
            "dry_run": False,
            "account_id": 1,
            "preview": {
                "symbol": "005930",
                "instrument_type": "equity_kr",
                "side": "buy",
                "order_type": "market",
                "quantity": Decimal("10"),
                "price": Decimal("72000"),
                "gross": Decimal("720000"),
                "fee": Decimal("108"),
                "total_cost": Decimal("720108"),
                "currency": "KRW",
            },
            "execution": {
                "symbol": "005930",
                "instrument_type": "equity_kr",
                "side": "buy",
                "order_type": "market",
                "quantity": Decimal("10"),
                "price": Decimal("72000"),
                "gross": Decimal("720000"),
                "fee": Decimal("108"),
                "total_cost": Decimal("720108"),
                "currency": "KRW",
                "realized_pnl": None,
                "executed_at": now_kst(),
            },
        }
        mock_account = MagicMock()
        mock_account.name = "default"
        mock_account.id = 1

        mock_service = AsyncMock()
        mock_service.execute_order = AsyncMock(return_value=mock_execution)
        mock_service.get_account_by_name = AsyncMock(return_value=mock_account)

        monkeypatch.setattr(
            paper_order_handler, "PaperTradingService", lambda db: mock_service
        )
        mock_session = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        monkeypatch.setattr(
            paper_order_handler, "AsyncSessionLocal", lambda: cm
        )

        mock_create_journal = AsyncMock()
        monkeypatch.setattr(
            paper_order_handler, "create_paper_journal", mock_create_journal
        )

        result = await paper_order_handler._place_paper_order(
            symbol="005930",
            side="buy",
            order_type="market",
            quantity=10.0,
            price=None,
            amount=None,
            dry_run=False,
            reason="Quick buy",
            paper_account_name=None,
        )

        assert result["success"] is True
        mock_create_journal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sell_calls_close_journal(self, monkeypatch):
        from app.mcp_server.tooling import paper_order_handler

        mock_execution = {
            "success": True,
            "dry_run": False,
            "account_id": 1,
            "preview": {
                "symbol": "005930",
                "instrument_type": "equity_kr",
                "side": "sell",
                "order_type": "market",
                "quantity": Decimal("10"),
                "price": Decimal("80000"),
                "gross": Decimal("800000"),
                "fee": Decimal("1560"),
                "total_cost": Decimal("798440"),
                "currency": "KRW",
            },
            "execution": {
                "symbol": "005930",
                "instrument_type": "equity_kr",
                "side": "sell",
                "order_type": "market",
                "quantity": Decimal("10"),
                "price": Decimal("80000"),
                "gross": Decimal("800000"),
                "fee": Decimal("1560"),
                "total_cost": Decimal("798440"),
                "currency": "KRW",
                "realized_pnl": Decimal("78440"),
                "executed_at": now_kst(),
            },
        }
        mock_account = MagicMock()
        mock_account.name = "default"
        mock_account.id = 1

        mock_service = AsyncMock()
        mock_service.execute_order = AsyncMock(return_value=mock_execution)
        mock_service.get_account_by_name = AsyncMock(return_value=mock_account)

        monkeypatch.setattr(
            paper_order_handler, "PaperTradingService", lambda db: mock_service
        )
        mock_session = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        monkeypatch.setattr(
            paper_order_handler, "AsyncSessionLocal", lambda: cm
        )

        mock_close_journal = AsyncMock(
            return_value={"success": True, "journal_id": 1, "pnl_pct": 11.11}
        )
        monkeypatch.setattr(
            paper_order_handler, "close_paper_journal", mock_close_journal
        )

        result = await paper_order_handler._place_paper_order(
            symbol="005930",
            side="sell",
            order_type="market",
            quantity=10.0,
            price=None,
            amount=None,
            dry_run=False,
            reason="Target reached",
            paper_account_name=None,
        )

        assert result["success"] is True
        mock_close_journal.assert_awaited_once()
        call_kwargs = mock_close_journal.call_args.kwargs
        assert call_kwargs["symbol"] == "005930"
        assert call_kwargs["paper_account_name"] == "default"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_paper_order_handler.py::TestPaperOrderJournalIntegration -v`
Expected: FAIL

- [ ] **Step 3: _place_paper_order 시그니처 확장 및 journal 연동 구현**

`app/mcp_server/tooling/paper_order_handler.py` 수정:

import 추가:
```python
from app.mcp_server.tooling.paper_journal_bridge import (
    close_paper_journal,
    create_paper_journal,
)
```

`_place_paper_order` 시그니처 확장:
```python
async def _place_paper_order(
    *,
    symbol: str,
    side: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
    amount: float | None,
    dry_run: bool,
    reason: str,
    paper_account_name: str | None,
    # Journal 연동 파라미터
    thesis: str | None = None,
    strategy: str | None = None,
    target_price: float | None = None,
    stop_loss: float | None = None,
    min_hold_days: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
```

실행 성공 후 journal 연동 로직 추가 (기존 return 직전):
```python
        # --- Journal 연동 ---
        exec_data = execution["execution"]
        account_name = account.name

        if side.lower() == "buy" and thesis:
            try:
                journal_result = await create_paper_journal(
                    symbol=exec_data["symbol"],
                    instrument_type=exec_data["instrument_type"],
                    entry_price=Decimal(str(exec_data["price"])),
                    quantity=Decimal(str(exec_data["quantity"])),
                    amount=Decimal(str(exec_data["gross"])),
                    paper_trade_id=exec_data.get("trade_id", 0),
                    paper_account_name=account_name,
                    thesis=thesis,
                    strategy=strategy,
                    target_price=Decimal(str(target_price)) if target_price else None,
                    stop_loss=Decimal(str(stop_loss)) if stop_loss else None,
                    min_hold_days=min_hold_days,
                    notes=notes,
                )
            except Exception as exc:
                logger.warning("Paper journal creation failed: %s", exc)
                journal_result = None
        elif side.lower() == "sell":
            try:
                journal_result = await close_paper_journal(
                    symbol=exec_data["symbol"],
                    exit_price=Decimal(str(exec_data["price"])),
                    exit_reason=reason or None,
                    paper_account_name=account_name,
                )
            except Exception as exc:
                logger.warning("Paper journal close failed: %s", exc)
                journal_result = None
        else:
            journal_result = None

        response = {
            "success": True,
            "dry_run": False,
            "account_type": "paper",
            "paper_account": account_name,
            "account_id": account.id,
            "preview": execution["preview"],
            "execution": execution["execution"],
            "message": "[Paper] Order placed successfully",
        }
        if journal_result is not None:
            response["journal"] = journal_result
        return response
```

- [ ] **Step 4: orders_registration.py에서 paper 경로에 journal 파라미터 전달**

`app/mcp_server/tooling/orders_registration.py` 수정:

`place_order` 함수의 paper 분기 (line 111-122) 변경:
```python
        if account_type == "paper":
            return await _place_paper_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                amount=amount,
                dry_run=dry_run,
                reason=reason,
                paper_account_name=paper_account,
                thesis=thesis,
                strategy=strategy,
                target_price=target_price,
                stop_loss=stop_loss,
                min_hold_days=min_hold_days,
                notes=notes,
            )
```

`place_order` MCP description 업데이트 — "In paper mode, thesis/strategy/journal parameters are ignored." 문구 제거, 대체:
```python
"In paper mode, if thesis is provided on buy, a trade journal is auto-created; "
"on sell, active paper journals are auto-closed in FIFO order. "
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `uv run pytest tests/test_paper_order_handler.py -v`
Expected: 모두 PASS

- [ ] **Step 6: 커밋**

```bash
git add app/mcp_server/tooling/paper_order_handler.py app/mcp_server/tooling/orders_registration.py tests/test_paper_order_handler.py
git commit -m "feat(paper): integrate journal create/close into paper order flow"
```

---

## Task 10: compare_strategies 구현

**Files:**
- Modify: `app/mcp_server/tooling/paper_journal_bridge.py`
- Modify: `tests/test_paper_journal_bridge.py`

- [ ] **Step 1: compare_strategies 테스트 작성**

`tests/test_paper_journal_bridge.py`에 추가:

```python
class TestCompareStrategies:
    """compare_strategies 단위 테스트."""

    def _make_closed_journal(
        self,
        *,
        symbol: str,
        account: str,
        strategy: str | None,
        account_type: str = "paper",
        entry_price: float,
        pnl_pct: float,
        journal_id: int,
    ) -> TradeJournal:
        j = TradeJournal(
            symbol=symbol,
            instrument_type=InstrumentType.equity_kr,
            thesis="Test",
            entry_price=Decimal(str(entry_price)),
            account_type=account_type,
            account=account,
            strategy=strategy,
            status="closed",
            pnl_pct=Decimal(str(pnl_pct)),
        )
        j.id = journal_id
        j.created_at = now_kst()
        j.updated_at = now_kst()
        j.exit_date = now_kst()
        j.exit_price = Decimal(str(entry_price * (1 + pnl_pct / 100)))
        return j

    @pytest.mark.asyncio
    async def test_strategies_aggregation_closed_only(self, monkeypatch):
        """closed journal 기준으로만 집계."""
        from app.mcp_server.tooling import paper_journal_bridge

        closed_win = self._make_closed_journal(
            symbol="005930", account="paper-m", strategy="momentum",
            entry_price=72000, pnl_pct=5.0, journal_id=1,
        )
        closed_loss = self._make_closed_journal(
            symbol="AAPL", account="paper-m", strategy="momentum",
            entry_price=150, pnl_pct=-3.0, journal_id=2,
        )
        active_journal = TradeJournal(
            symbol="TSLA",
            instrument_type=InstrumentType.equity_us,
            thesis="Test",
            account_type="paper",
            account="paper-m",
            strategy="momentum",
            status="active",
        )
        active_journal.id = 3
        active_journal.created_at = now_kst()
        active_journal.updated_at = now_kst()

        # Mock: paper journals query returns closed + active
        mock_session = AsyncMock()

        # First call: paper closed journals for strategies
        paper_scalars = MagicMock()
        paper_scalars.all.return_value = [closed_win, closed_loss, active_journal]
        paper_result = MagicMock()
        paper_result.scalars.return_value = paper_scalars

        # Second call: live journals (empty for this test)
        live_scalars = MagicMock()
        live_scalars.all.return_value = []
        live_result = MagicMock()
        live_result.scalars.return_value = live_scalars

        # Third call: paper account lookup
        account_scalars = MagicMock()
        mock_paper_account = MagicMock()
        mock_paper_account.id = 1
        mock_paper_account.name = "paper-m"
        mock_paper_account.strategy_name = "momentum"
        account_scalars.all.return_value = [mock_paper_account]
        account_result = MagicMock()
        account_result.scalars.return_value = account_scalars

        mock_session.execute = AsyncMock(
            side_effect=[account_result, paper_result, live_result]
        )

        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        factory = MagicMock(return_value=cm)
        monkeypatch.setattr(
            paper_journal_bridge, "_session_factory", lambda: factory
        )

        result = await paper_journal_bridge.compare_strategies(days=30)

        assert result["success"] is True
        strategies = result["strategies"]
        assert len(strategies) == 1
        s = strategies[0]
        # Only closed journals counted
        assert s["total_trades"] == 2
        assert s["win_count"] == 1
        assert s["loss_count"] == 1
        assert s["win_rate"] == 50.0

    @pytest.mark.asyncio
    async def test_include_live_comparison_false(self, monkeypatch):
        """include_live_comparison=False → live_vs_paper 빈 배열."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_session = AsyncMock()
        account_scalars = MagicMock()
        account_scalars.all.return_value = []
        account_result = MagicMock()
        account_result.scalars.return_value = account_scalars

        paper_scalars = MagicMock()
        paper_scalars.all.return_value = []
        paper_result = MagicMock()
        paper_result.scalars.return_value = paper_scalars

        mock_session.execute = AsyncMock(
            side_effect=[account_result, paper_result]
        )

        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        factory = MagicMock(return_value=cm)
        monkeypatch.setattr(
            paper_journal_bridge, "_session_factory", lambda: factory
        )

        result = await paper_journal_bridge.compare_strategies(
            days=30, include_live_comparison=False
        )
        assert result["success"] is True
        assert result["live_vs_paper"] == []

    @pytest.mark.asyncio
    async def test_live_vs_paper_same_symbol(self, monkeypatch):
        """같은 종목 live/paper journal → 비교 결과 생성."""
        from app.mcp_server.tooling import paper_journal_bridge

        paper_j = self._make_closed_journal(
            symbol="005930", account="paper-m", strategy="momentum",
            account_type="paper", entry_price=72000, pnl_pct=5.0, journal_id=1,
        )
        live_j = self._make_closed_journal(
            symbol="005930", account="kis-main", strategy=None,
            account_type="live", entry_price=71000, pnl_pct=3.0, journal_id=2,
        )

        mock_session = AsyncMock()

        account_scalars = MagicMock()
        mock_paper_account = MagicMock()
        mock_paper_account.id = 1
        mock_paper_account.name = "paper-m"
        mock_paper_account.strategy_name = "momentum"
        account_scalars.all.return_value = [mock_paper_account]
        account_result = MagicMock()
        account_result.scalars.return_value = account_scalars

        paper_scalars = MagicMock()
        paper_scalars.all.return_value = [paper_j]
        paper_result = MagicMock()
        paper_result.scalars.return_value = paper_scalars

        live_scalars = MagicMock()
        live_scalars.all.return_value = [live_j]
        live_result = MagicMock()
        live_result.scalars.return_value = live_scalars

        mock_session.execute = AsyncMock(
            side_effect=[account_result, paper_result, live_result]
        )

        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        factory = MagicMock(return_value=cm)
        monkeypatch.setattr(
            paper_journal_bridge, "_session_factory", lambda: factory
        )

        result = await paper_journal_bridge.compare_strategies(
            days=30, include_live_comparison=True
        )

        assert result["success"] is True
        assert len(result["live_vs_paper"]) == 1
        comp = result["live_vs_paper"][0]
        assert comp["symbol"] == "005930"
        assert comp["paper_pnl_pct"] == 5.0
        assert comp["live_pnl_pct"] == 3.0
        assert comp["delta_pnl_pct"] == pytest.approx(2.0)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py::TestCompareStrategies -v`
Expected: FAIL

- [ ] **Step 3: compare_strategies 구현**

`app/mcp_server/tooling/paper_journal_bridge.py`에 추가:

```python
from app.models.paper_trading import PaperAccount


async def compare_strategies(
    days: int = 30,
    strategy_name: str | None = None,
    include_live_comparison: bool = True,
) -> dict[str, Any]:
    """Compare paper trading strategy performance over a given period.

    All metrics (win_rate, total_return_pct, avg_pnl_pct, best/worst_trade)
    are based on **closed** journals only (realized performance).

    Aggregation unit is paper account. strategy_name is a filter on
    TradeJournal.strategy (not PaperAccount.strategy_name).
    """
    cutoff = now_kst() - timedelta(days=days)

    try:
        async with _session_factory()() as db:
            # 1. Get paper accounts for metadata
            acct_stmt = select(PaperAccount).where(PaperAccount.is_active.is_(True))
            acct_result = await db.execute(acct_stmt)
            accounts = {a.name: a for a in acct_result.scalars().all()}

            # 2. Fetch paper journals in period
            paper_filters = [
                TradeJournal.account_type == "paper",
                TradeJournal.created_at >= cutoff,
            ]
            if strategy_name is not None:
                paper_filters.append(TradeJournal.strategy == strategy_name)

            paper_stmt = (
                select(TradeJournal)
                .where(*paper_filters)
                .order_by(desc(TradeJournal.created_at))
            )
            paper_result = await db.execute(paper_stmt)
            paper_journals = list(paper_result.scalars().all())

            # 3. Aggregate by account (closed only)
            from collections import defaultdict

            by_account: dict[str, list[TradeJournal]] = defaultdict(list)
            for j in paper_journals:
                if j.status == JournalStatus.closed and j.account:
                    by_account[j.account].append(j)

            strategies_out: list[dict[str, Any]] = []
            for account_name, journals in by_account.items():
                acct = accounts.get(account_name)
                wins = [j for j in journals if j.pnl_pct is not None and j.pnl_pct > 0]
                losses = [j for j in journals if j.pnl_pct is not None and j.pnl_pct <= 0]
                total = len(journals)
                win_count = len(wins)
                loss_count = len(losses)

                pnl_values = [
                    float(j.pnl_pct) for j in journals if j.pnl_pct is not None
                ]
                total_return_pct = sum(pnl_values) if pnl_values else 0.0
                avg_pnl_pct = (
                    round(total_return_pct / len(pnl_values), 2) if pnl_values else 0.0
                )
                win_rate = round(win_count / total * 100, 1) if total > 0 else 0.0

                best = max(journals, key=lambda j: float(j.pnl_pct or 0))
                worst = min(journals, key=lambda j: float(j.pnl_pct or 0))

                strategies_out.append({
                    "strategy_name": acct.strategy_name if acct else None,
                    "account_name": account_name,
                    "account_id": acct.id if acct else None,
                    "total_trades": total,
                    "win_count": win_count,
                    "loss_count": loss_count,
                    "win_rate": win_rate,
                    "total_return_pct": round(total_return_pct, 2),
                    "avg_pnl_pct": avg_pnl_pct,
                    "best_trade": {
                        "symbol": best.symbol,
                        "pnl_pct": float(best.pnl_pct) if best.pnl_pct else 0.0,
                    },
                    "worst_trade": {
                        "symbol": worst.symbol,
                        "pnl_pct": float(worst.pnl_pct) if worst.pnl_pct else 0.0,
                    },
                })

            # 4. Live vs paper comparison
            live_vs_paper: list[dict[str, Any]] = []
            if include_live_comparison:
                live_stmt = (
                    select(TradeJournal)
                    .where(
                        TradeJournal.account_type == "live",
                        TradeJournal.status == JournalStatus.closed,
                        TradeJournal.created_at >= cutoff,
                    )
                    .order_by(desc(TradeJournal.created_at))
                )
                live_result = await db.execute(live_stmt)
                live_journals = list(live_result.scalars().all())

                # Most recent closed journal per symbol (live)
                live_by_symbol: dict[str, TradeJournal] = {}
                for j in live_journals:
                    if j.symbol not in live_by_symbol:
                        live_by_symbol[j.symbol] = j

                # Most recent closed journal per symbol (paper)
                paper_closed = [
                    j for j in paper_journals if j.status == JournalStatus.closed
                ]
                paper_by_symbol: dict[str, TradeJournal] = {}
                for j in paper_closed:
                    if j.symbol not in paper_by_symbol:
                        paper_by_symbol[j.symbol] = j

                # Match
                common_symbols = set(live_by_symbol) & set(paper_by_symbol)
                for sym in sorted(common_symbols):
                    lj = live_by_symbol[sym]
                    pj = paper_by_symbol[sym]
                    l_pnl = float(lj.pnl_pct) if lj.pnl_pct is not None else 0.0
                    p_pnl = float(pj.pnl_pct) if pj.pnl_pct is not None else 0.0
                    live_vs_paper.append({
                        "symbol": sym,
                        "live_entry_price": float(lj.entry_price) if lj.entry_price else None,
                        "live_pnl_pct": l_pnl,
                        "paper_entry_price": float(pj.entry_price) if pj.entry_price else None,
                        "paper_pnl_pct": p_pnl,
                        "paper_strategy": pj.strategy,
                        "delta_pnl_pct": round(p_pnl - l_pnl, 4),
                    })

            return {
                "success": True,
                "period_days": days,
                "strategies": strategies_out,
                "live_vs_paper": live_vs_paper,
            }
    except Exception as exc:
        logger.exception("compare_strategies failed")
        return {"success": False, "error": f"compare_strategies failed: {exc}"}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py::TestCompareStrategies -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/paper_journal_bridge.py tests/test_paper_journal_bridge.py
git commit -m "feat(paper): implement compare_strategies in paper_journal_bridge"
```

---

## Task 11: recommend_go_live 구현

**Files:**
- Modify: `app/mcp_server/tooling/paper_journal_bridge.py`
- Modify: `tests/test_paper_journal_bridge.py`

- [ ] **Step 1: recommend_go_live 테스트 작성**

`tests/test_paper_journal_bridge.py`에 추가:

```python
class TestRecommendGoLive:
    """recommend_go_live 판정 테스트."""

    def _make_closed_journal(
        self, *, pnl_pct: float, journal_id: int
    ) -> TradeJournal:
        j = TradeJournal(
            symbol=f"SYM{journal_id}",
            instrument_type=InstrumentType.equity_kr,
            thesis="Test",
            entry_price=Decimal("10000"),
            account_type="paper",
            account="paper-test",
            status="closed",
            pnl_pct=Decimal(str(pnl_pct)),
        )
        j.id = journal_id
        j.created_at = now_kst()
        j.updated_at = now_kst()
        return j

    def _mock_session_for_recommend(
        self, monkeypatch, account, closed_journals, active_count=0
    ):
        from app.mcp_server.tooling import paper_journal_bridge

        mock_session = AsyncMock()

        # First query: account lookup
        acct_scalars = MagicMock()
        acct_scalars.one_or_none.return_value = account
        acct_result = MagicMock()
        acct_result.scalars.return_value = acct_scalars

        # Second query: closed journals
        closed_scalars = MagicMock()
        closed_scalars.all.return_value = closed_journals
        closed_result = MagicMock()
        closed_result.scalars.return_value = closed_scalars

        # Third query: active count
        active_scalar_result = MagicMock()
        active_scalar_result.scalar_one.return_value = active_count
        
        mock_session.execute = AsyncMock(
            side_effect=[acct_result, closed_result, active_scalar_result]
        )

        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        factory = MagicMock(return_value=cm)
        monkeypatch.setattr(
            paper_journal_bridge, "_session_factory", lambda: factory
        )

    @pytest.mark.asyncio
    async def test_all_criteria_met_go_live(self, monkeypatch):
        from app.mcp_server.tooling import paper_journal_bridge

        account = MagicMock()
        account.name = "paper-test"
        account.strategy_name = "momentum"

        # 25 trades, 16 wins, 9 losses → win_rate=64%, total_return positive
        journals = []
        for i in range(16):
            journals.append(self._make_closed_journal(pnl_pct=3.0, journal_id=i + 1))
        for i in range(9):
            journals.append(
                self._make_closed_journal(pnl_pct=-2.0, journal_id=i + 17)
            )

        self._mock_session_for_recommend(monkeypatch, account, journals)

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test"
        )
        assert result["success"] is True
        assert result["recommendation"] == "go_live"
        assert result["all_passed"] is True
        assert result["criteria"]["min_trades"]["passed"] is True
        assert result["criteria"]["min_win_rate"]["passed"] is True
        assert result["criteria"]["min_return_pct"]["passed"] is True

    @pytest.mark.asyncio
    async def test_insufficient_trades_not_ready(self, monkeypatch):
        from app.mcp_server.tooling import paper_journal_bridge

        account = MagicMock()
        account.name = "paper-test"
        account.strategy_name = "test"

        journals = [
            self._make_closed_journal(pnl_pct=5.0, journal_id=i + 1)
            for i in range(10)
        ]
        self._mock_session_for_recommend(monkeypatch, account, journals)

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test"
        )
        assert result["recommendation"] == "not_ready"
        assert result["criteria"]["min_trades"]["passed"] is False

    @pytest.mark.asyncio
    async def test_low_win_rate_not_ready(self, monkeypatch):
        from app.mcp_server.tooling import paper_journal_bridge

        account = MagicMock()
        account.name = "paper-test"
        account.strategy_name = "test"

        # 20 trades, only 5 wins → 25%
        journals = []
        for i in range(5):
            journals.append(self._make_closed_journal(pnl_pct=2.0, journal_id=i + 1))
        for i in range(15):
            journals.append(
                self._make_closed_journal(pnl_pct=-1.0, journal_id=i + 6)
            )
        self._mock_session_for_recommend(monkeypatch, account, journals)

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test"
        )
        assert result["recommendation"] == "not_ready"
        assert result["criteria"]["min_win_rate"]["passed"] is False

    @pytest.mark.asyncio
    async def test_custom_thresholds(self, monkeypatch):
        from app.mcp_server.tooling import paper_journal_bridge

        account = MagicMock()
        account.name = "paper-test"
        account.strategy_name = "test"

        journals = [
            self._make_closed_journal(pnl_pct=1.0, journal_id=i + 1)
            for i in range(10)
        ]
        self._mock_session_for_recommend(monkeypatch, account, journals)

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test",
            min_trades=5,
            min_win_rate=80.0,
            min_return_pct=5.0,
        )
        assert result["recommendation"] == "go_live"
        assert result["criteria"]["min_trades"]["required"] == 5

    @pytest.mark.asyncio
    async def test_account_not_found(self, monkeypatch):
        from app.mcp_server.tooling import paper_journal_bridge

        self._mock_session_for_recommend(monkeypatch, None, [])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="nonexistent"
        )
        assert result["success"] is False
        assert "not found" in result["error"]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py::TestRecommendGoLive -v`
Expected: FAIL

- [ ] **Step 3: recommend_go_live 구현**

`app/mcp_server/tooling/paper_journal_bridge.py`에 추가:

```python
from sqlalchemy import func as sa_func


async def recommend_go_live(
    account_name: str,
    min_trades: int = 20,
    min_win_rate: float = 50.0,
    min_return_pct: float = 0.0,
) -> dict[str, Any]:
    """Evaluate whether a paper trading account meets go-live criteria.

    All metrics are based on **closed** journals only (realized performance).
    Active positions are shown in summary for reference but excluded from judgment.
    """
    try:
        async with _session_factory()() as db:
            # 1. Account lookup
            acct_stmt = select(PaperAccount).where(
                PaperAccount.name == account_name
            )
            acct_result = await db.execute(acct_stmt)
            account = acct_result.scalars().one_or_none()
            if account is None:
                return {
                    "success": False,
                    "error": f"Paper account '{account_name}' not found",
                }

            # 2. Closed journals
            closed_stmt = (
                select(TradeJournal)
                .where(
                    TradeJournal.account_type == "paper",
                    TradeJournal.account == account_name,
                    TradeJournal.status == JournalStatus.closed,
                )
                .order_by(desc(TradeJournal.created_at))
            )
            closed_result = await db.execute(closed_stmt)
            closed_journals = list(closed_result.scalars().all())

            # 3. Active positions count (reference only)
            active_stmt = (
                select(sa_func.count())
                .select_from(TradeJournal)
                .where(
                    TradeJournal.account_type == "paper",
                    TradeJournal.account == account_name,
                    TradeJournal.status == JournalStatus.active,
                )
            )
            active_result = await db.execute(active_stmt)
            active_positions = active_result.scalar_one()

            # 4. Calculate metrics
            total_trades = len(closed_journals)
            pnl_values = [
                float(j.pnl_pct) for j in closed_journals if j.pnl_pct is not None
            ]
            win_count = sum(1 for v in pnl_values if v > 0)
            loss_count = total_trades - win_count
            total_return_pct = round(sum(pnl_values), 2) if pnl_values else 0.0
            win_rate = round(win_count / total_trades * 100, 1) if total_trades > 0 else 0.0
            avg_pnl_pct = (
                round(total_return_pct / len(pnl_values), 2) if pnl_values else 0.0
            )

            best_trade = None
            worst_trade = None
            if closed_journals:
                best = max(closed_journals, key=lambda j: float(j.pnl_pct or 0))
                worst = min(closed_journals, key=lambda j: float(j.pnl_pct or 0))
                best_trade = {
                    "symbol": best.symbol,
                    "pnl_pct": float(best.pnl_pct) if best.pnl_pct else 0.0,
                }
                worst_trade = {
                    "symbol": worst.symbol,
                    "pnl_pct": float(worst.pnl_pct) if worst.pnl_pct else 0.0,
                }

            # 5. Criteria check
            trades_passed = total_trades >= min_trades
            wr_passed = win_rate >= min_win_rate
            return_passed = total_return_pct >= min_return_pct
            all_passed = trades_passed and wr_passed and return_passed

            return {
                "success": True,
                "account_name": account_name,
                "strategy_name": account.strategy_name,
                "recommendation": "go_live" if all_passed else "not_ready",
                "criteria": {
                    "min_trades": {
                        "required": min_trades,
                        "actual": total_trades,
                        "passed": trades_passed,
                    },
                    "min_win_rate": {
                        "required": min_win_rate,
                        "actual": win_rate,
                        "passed": wr_passed,
                    },
                    "min_return_pct": {
                        "required": min_return_pct,
                        "actual": total_return_pct,
                        "passed": return_passed,
                    },
                },
                "all_passed": all_passed,
                "summary": {
                    "total_trades": total_trades,
                    "win_count": win_count,
                    "loss_count": loss_count,
                    "win_rate": win_rate,
                    "total_return_pct": total_return_pct,
                    "avg_pnl_pct": avg_pnl_pct,
                    "best_trade": best_trade,
                    "worst_trade": worst_trade,
                    "active_positions": active_positions,
                },
            }
    except Exception as exc:
        logger.exception("recommend_go_live failed")
        return {"success": False, "error": f"recommend_go_live failed: {exc}"}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py::TestRecommendGoLive -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/paper_journal_bridge.py tests/test_paper_journal_bridge.py
git commit -m "feat(paper): implement recommend_go_live in paper_journal_bridge"
```

---

## Task 12: MCP 등록 — compare_strategies, recommend_go_live

**Files:**
- Create: `app/mcp_server/tooling/paper_journal_registration.py`
- Modify: `app/mcp_server/tooling/registry.py`

- [ ] **Step 1: registration 파일 생성**

`app/mcp_server/tooling/paper_journal_registration.py` 생성:

```python
"""Paper Journal MCP tool registration — compare_strategies, recommend_go_live."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.paper_journal_bridge import (
    compare_strategies,
    recommend_go_live,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

PAPER_JOURNAL_TOOL_NAMES: set[str] = {
    "compare_strategies",
    "recommend_go_live",
}


def register_paper_journal_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="compare_strategies",
        description=(
            "Compare paper trading strategy performance over a given period. "
            "Shows per-account/per-strategy metrics such as win rate, realized return, "
            "and best/worst trade. All metrics are based on closed journals only. "
            "If include_live_comparison=True, also compares same-symbol live vs paper "
            "journal outcomes within the same period."
        ),
    )(compare_strategies)

    _ = mcp.tool(
        name="recommend_go_live",
        description=(
            "Evaluate whether a paper trading account meets criteria for live trading. "
            "Checks total trades, win rate, and realized return against thresholds "
            "(default: 20 trades, 50% win rate, positive return). "
            "All metrics are based on closed journals only."
        ),
    )(recommend_go_live)


__all__ = ["PAPER_JOURNAL_TOOL_NAMES", "register_paper_journal_tools"]
```

- [ ] **Step 2: registry.py에 등록 추가**

`app/mcp_server/tooling/registry.py` 수정:

import 추가:
```python
from app.mcp_server.tooling.paper_journal_registration import (
    register_paper_journal_tools,
)
```

`register_all_tools` 함수에 추가:
```python
register_paper_journal_tools(mcp)
```

- [ ] **Step 3: 등록 테스트**

`tests/test_paper_journal_bridge.py`에 추가:

```python
class TestPaperJournalRegistration:
    """MCP 도구 등록 확인."""

    def test_tool_names_defined(self):
        from app.mcp_server.tooling.paper_journal_registration import (
            PAPER_JOURNAL_TOOL_NAMES,
        )

        assert "compare_strategies" in PAPER_JOURNAL_TOOL_NAMES
        assert "recommend_go_live" in PAPER_JOURNAL_TOOL_NAMES

    def test_register_does_not_raise(self):
        from unittest.mock import MagicMock

        from app.mcp_server.tooling.paper_journal_registration import (
            register_paper_journal_tools,
        )

        mock_mcp = MagicMock()
        mock_mcp.tool.return_value = lambda fn: fn
        register_paper_journal_tools(mock_mcp)
        assert mock_mcp.tool.call_count == 2
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py::TestPaperJournalRegistration -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/paper_journal_registration.py app/mcp_server/tooling/registry.py tests/test_paper_journal_bridge.py
git commit -m "feat(paper): register compare_strategies and recommend_go_live MCP tools"
```

---

## Task 13: 전체 테스트 스위트 실행 및 lint

- [ ] **Step 1: 전체 테스트 실행**

Run: `uv run pytest tests/test_paper_journal_bridge.py tests/test_mcp_trade_journal.py tests/test_paper_trading_service.py tests/test_paper_account_tools.py tests/test_paper_order_handler.py tests/test_trade_journal_model.py -v`
Expected: 모두 PASS

- [ ] **Step 2: lint 실행**

Run: `make lint`
Expected: 오류 없음

- [ ] **Step 3: format 실행**

Run: `make format`

- [ ] **Step 4: lint 재확인 후 커밋**

Run: `make lint`

```bash
git add -A
git commit -m "style: fix formatting from lint"
```

(변경사항 없으면 커밋 생략)
