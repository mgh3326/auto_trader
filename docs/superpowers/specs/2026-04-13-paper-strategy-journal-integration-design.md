# Paper Trading 다중 전략 + Trade Journal 연동 설계

## 목표

모의투자 계좌에 전략 태깅을 강화하고, 기존 Trade Journal 시스템과 연동하여
paper 매매 시 투자 논거(thesis)를 자동 기록하고, 전략 간 성과를 비교하며,
실전 전환 추천 기준을 제공한다.

## 컨텍스트

- Trade Journal 모델: `app/models/trade_journal.py` (review 스키마)
- Trade Journal MCP 도구: `app/mcp_server/tooling/trade_journal_tools.py`
- Paper Trading 서비스: `app/services/paper_trading_service.py`
- Paper Trading 모델: `app/models/paper_trading.py` (paper 스키마)
- Paper MCP 도구: `app/mcp_server/tooling/paper_account_registration.py`, `paper_order_handler.py`, `paper_portfolio_handler.py`

## 설계 결정 요약

| 결정 항목 | 선택 | 이유 |
|-----------|------|------|
| Journal 저장 방식 | 기존 TradeJournal에 `account_type` 컬럼 추가 | 중복 최소화, 실전/모의 비교 JOIN 용이 |
| 주문-Journal 연동 | 자동 생성 + paper_trade_id 링크 | 원스텝 기록, 데이터 일관성 |
| 매도 시 Journal close | FIFO 정책 | 같은 종목 여러 active journal 허용, 오래된 것부터 close |
| 전략 비교 범위 | 계좌 단위 + 실전 vs 모의 동일 종목 비교 | 원 요구사항 충족 |
| 실전 전환 기준 | 파라미터로 override 가능한 기본값 | 유연성 확보, 최소 복잡도 |
| list_paper_accounts | strategy_name 필터만 (그룹핑 없음) | 반환 스키마 변경 없음 |
| 구현 접근법 | paper_journal_bridge.py 모듈 분리 | SRP 유지, 테스트 용이 |

---

## 1. DB 변경: TradeJournal 모델

### 컬럼 추가

`app/models/trade_journal.py`의 `TradeJournal` 클래스에 2개 컬럼 추가:

```python
# 실전/모의 구분
account_type: Mapped[str] = mapped_column(
    Text, nullable=False, default="live", server_default="live"
)

# paper.paper_trades 소프트 참조 (스키마가 달라 DB FK 미사용)
paper_trade_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
```

### 제약 조건 추가

```python
CheckConstraint(
    "account_type IN ('live','paper')",
    name="trade_journals_account_type",
)
CheckConstraint(
    "NOT (account_type = 'live' AND paper_trade_id IS NOT NULL)",
    name="trade_journals_no_paper_trade_on_live",
)
```

### 인덱스 추가

```python
Index("ix_trade_journals_account_type", "account_type")
```

### 마이그레이션

- `account_type TEXT NOT NULL DEFAULT 'live'` — 기존 row 전부 `'live'`
- `paper_trade_id BIGINT NULL`
- 인덱스 1개, CheckConstraint 2개

---

## 2. Paper Journal Bridge 모듈

### 새 파일: `app/mcp_server/tooling/paper_journal_bridge.py`

paper 주문 결과를 journal 도메인에 반영하는 어댑터 계층.
주문 체결은 order handler/service가 책임지고, journal create/close/compare/recommend만 담당.

### 2-1. `create_paper_journal()`

paper 매수 주문 체결 후 호출. TradeJournal 레코드 생성.

```python
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
```

- buy 전용 함수 (side 파라미터 없음)
- `account_type="paper"`, `account=paper_account_name`
- `status="active"` (paper는 즉시 체결 — draft 건너뜀)
- `trade_id=None` (live용), `paper_trade_id=paper_trade_id`

### 2-2. `close_paper_journal()`

paper 매도 주문 체결 후 호출. 해당 종목의 active paper journal을 FIFO로 close.

```python
async def close_paper_journal(
    *,
    symbol: str,
    exit_price: Decimal,
    exit_reason: str | None = None,
    paper_account_name: str,
) -> dict[str, Any] | None:
```

- 조회 조건: `symbol + account_type="paper" + account=paper_account_name + status="active"`
- **FIFO 정책**: 같은 종목에 active journal이 여러 개면 `created_at` 가장 오래된 것부터 close
- `pnl_pct` 자동 계산, `status="closed"`, `exit_date=now_kst()`
- journal 없으면 `None` 반환 (에러 아님 — thesis 없이 주문한 경우)

### 2-3. `compare_strategies()`

섹션 4에서 상세 기술.

### 2-4. `recommend_go_live()`

섹션 5에서 상세 기술.

### 호출 흐름

```
_place_paper_order()
  ├─ service.execute_order()          # 주문 체결
  ├─ if thesis and side=="buy":
  │    create_paper_journal()         # journal 자동 생성
  └─ if side=="sell":
       close_paper_journal()          # active journal 자동 close (FIFO)
```

---

## 3. MCP 도구 변경 (기존 도구 수정)

### 3-1. `create_paper_account` — strategy_name 노출

```python
async def create_paper_account(
    name: str,
    initial_capital: float = 100_000_000.0,
    initial_capital_usd: float = 0.0,
    description: str | None = None,
    strategy_name: str | None = None,    # 추가
) -> dict[str, Any]:
```

- 서비스 `create_account()`는 이미 `strategy_name`을 받으므로 pass-through
- MCP description에 strategy 예시값(daytrading, swing, ai-signal) 포함

### 3-2. `list_paper_accounts` — strategy_name 필터

```python
async def list_paper_accounts(
    is_active: bool = True,
    strategy_name: str | None = None,    # 추가
) -> dict[str, Any]:
```

- 반환 형태는 기존 flat list 유지
- **서비스 레이어**에서 `strategy_name` 필터 처리 (MCP 후처리가 아님)

### 3-3. `place_order` — paper 경로에 thesis/strategy 전달

`_place_paper_order()` 시그니처 확장:

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
    # 추가 — journal 연동용 (live/paper 공통 파라미터)
    thesis: str | None = None,
    strategy: str | None = None,
    target_price: float | None = None,
    stop_loss: float | None = None,
    min_hold_days: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
```

- 상위 `place_order` MCP 도구에서 위 파라미터를 받음
- live/paper 공통 파라미터로 취급 (paper만의 예외가 아님)
- live 경로는 기존 처리 유지, paper 경로에서 bridge 호출

### 3-4. `get_trade_journal` / `save_trade_journal` — account_type 반영

```python
# get_trade_journal
account_type: str | None = "live"   # 기본 live만 반환, None이면 전체, "paper"면 paper만

# save_trade_journal
account_type: str = "live"
paper_trade_id: int | None = None
```

**애플리케이션 레벨 검증 (DB constraint와 별도로):**
- `account_type="live"` + `paper_trade_id` 제공 → 명시적 오류
- `account_type="paper"` + `trade_id` 제공 → 명시적 오류
- `account_type="paper"` + `account` 비어있음 → 명시적 오류

### 3-5. `_serialize_journal` 확장

반환 dict에 `account_type`, `paper_trade_id` 추가:
- live journal: `account_type="live"`, `trade_id=<id>`, `paper_trade_id=None`
- paper journal: `account_type="paper"`, `trade_id=None`, `paper_trade_id=<id>`

---

## 4. `compare_strategies` MCP 도구

### 시그니처

```python
async def compare_strategies(
    days: int = 30,
    strategy_name: str | None = None,
    include_live_comparison: bool = True,
) -> dict[str, Any]:
```

### 반환 구조

```python
{
    "success": True,
    "period_days": 30,
    "strategies": [
        {
            "strategy_name": "momentum",
            "account_name": "paper-momentum",
            "account_id": 1,
            "total_trades": 15,
            "win_count": 9,
            "loss_count": 6,
            "win_rate": 60.0,
            "total_return_pct": 5.2,
            "avg_pnl_pct": 1.8,
            "best_trade": {"symbol": "005930", "pnl_pct": 8.5},
            "worst_trade": {"symbol": "AAPL", "pnl_pct": -3.2},
        },
    ],
    "live_vs_paper": [
        {
            "symbol": "005930",
            "live_entry_price": 72000,
            "live_pnl_pct": 3.5,
            "paper_entry_price": 71500,
            "paper_pnl_pct": 5.2,
            "paper_strategy": "momentum",
            "delta_pnl_pct": 1.7,
        },
    ],
}
```

### 집계 기준 (고정 규칙)

1. **strategies 섹션의 모든 지표(total_trades, win_rate, avg_pnl_pct, best/worst_trade)는 closed journal 기준으로만 계산.** active journal은 집계에서 제외.
2. **`total_return_pct`는 realized 기준.** closed journal들의 `pnl_pct` 합산. 코드 주석과 MCP description에 realized 기준임을 명시.
3. **집계 단위는 paper account 기준.** `strategy_name`은 필터/표시 역할. 같은 전략명을 여러 계좌가 공유해도 각각 별도 항목.
4. **`strategy_name` 필터는 `TradeJournal.strategy` 기준.** `PaperAccount.strategy_name`이 아닌 journal의 strategy 필드 사용.

### live_vs_paper 비교 규칙

- 같은 `symbol`에 대해 `account_type="live"`와 `account_type="paper"` closed journal이 모두 존재
- 지정 기간 내 `created_at` 기준
- **종목별 최근 1건 비교로 단순화** (정밀 timestamp 매칭 안 함)
- `include_live_comparison=False`일 때 `live_vs_paper`는 빈 배열 유지 (필드 생략하지 않음)

### MCP description

```
"Compare paper trading strategy performance over a given period. "
"Shows per-account/per-strategy metrics such as win rate, realized return, "
"and best/worst trade. If include_live_comparison=True, also compares "
"same-symbol live vs paper journal outcomes within the same period."
```

---

## 5. `recommend_go_live` MCP 도구

### 시그니처

```python
async def recommend_go_live(
    account_name: str,
    min_trades: int = 20,
    min_win_rate: float = 50.0,
    min_return_pct: float = 0.0,
) -> dict[str, Any]:
```

### 반환 구조

```python
{
    "success": True,
    "account_name": "paper-momentum",
    "strategy_name": "momentum",
    "recommendation": "go_live" | "not_ready",
    "criteria": {
        "min_trades": {"required": 20, "actual": 25, "passed": True},
        "min_win_rate": {"required": 50.0, "actual": 64.0, "passed": True},
        "min_return_pct": {"required": 0.0, "actual": 3.8, "passed": True},
    },
    "all_passed": True,
    "summary": {
        "total_trades": 25,
        "win_count": 16,
        "loss_count": 9,
        "win_rate": 64.0,
        "total_return_pct": 3.8,
        "avg_pnl_pct": 1.2,
        "best_trade": {"symbol": "005930", "pnl_pct": 8.5},
        "worst_trade": {"symbol": "AAPL", "pnl_pct": -3.2},
        "active_positions": 3,
    },
}
```

### 판정 로직

1. `account_name`으로 PaperAccount 조회 → `strategy_name` 가져옴
2. 해당 계좌의 **closed** journal만 조회 (`account_type="paper"`, `account=account_name`, `status="closed"`)
3. 세 기준 각각 판정:
   - `total_trades >= min_trades`
   - `win_rate >= min_win_rate` (win = `pnl_pct > 0`)
   - `total_return_pct >= min_return_pct` (closed journal들의 realized pnl_pct 합산)
4. 세 기준 모두 통과 → `"go_live"`, 아니면 `"not_ready"`

### 설계 원칙

- `compare_strategies`와 동일한 집계 기준: closed journal, realized pnl 기준
- active position 수는 `summary`에 참고용으로 포함하되 판정에는 미포함
- `total_return_pct` 계산은 `compare_strategies`와 동일 정의

### MCP description

```
"Evaluate whether a paper trading account meets criteria for live trading. "
"Checks total trades, win rate, and realized return against thresholds "
"(default: 20 trades, 50% win rate, positive return). "
"All metrics are based on closed journals only."
```

---

## 6. MCP 등록

새 파일 `app/mcp_server/tooling/paper_journal_registration.py` 생성:

- `compare_strategies` 도구 등록
- `recommend_go_live` 도구 등록

기존 파일 수정:
- `paper_account_registration.py`: `create_paper_account`에 `strategy_name`, `list_paper_accounts`에 `strategy_name` 파라미터
- `paper_order_handler.py`: `_place_paper_order`에 thesis/strategy 파라미터 + bridge 호출
- `trade_journal_tools.py`: `account_type`, `paper_trade_id` 지원
- `trade_journal_registration.py`: MCP description 업데이트

---

## 7. 파일 변경 요약

| 파일 | 변경 유형 | 내용 |
|------|----------|------|
| `app/models/trade_journal.py` | 수정 | `account_type`, `paper_trade_id` 컬럼, constraints |
| `alembic/versions/xxx_add_paper_journal_fields.py` | 신규 | 마이그레이션 |
| `app/mcp_server/tooling/paper_journal_bridge.py` | 신규 | create/close/compare/recommend |
| `app/mcp_server/tooling/paper_journal_registration.py` | 신규 | compare_strategies, recommend_go_live 등록 |
| `app/mcp_server/tooling/paper_account_registration.py` | 수정 | strategy_name 파라미터 |
| `app/mcp_server/tooling/paper_order_handler.py` | 수정 | thesis/strategy 파라미터, bridge 호출 |
| `app/mcp_server/tooling/trade_journal_tools.py` | 수정 | account_type, paper_trade_id, 검증 |
| `app/mcp_server/tooling/trade_journal_registration.py` | 수정 | MCP description 업데이트 |
| `app/services/paper_trading_service.py` | 수정 | list_accounts에 strategy_name 필터 |
| `tests/test_paper_journal_bridge.py` | 신규 | bridge 단위 테스트 |
| `tests/test_paper_strategy_integration.py` | 신규 | 전략 계좌 생성→거래→저널→비교 통합 테스트 |

---

## 8. 테스트 계획

### 단위 테스트 (`tests/test_paper_journal_bridge.py`)

**create_paper_journal:**
- 매수 체결 후 journal 생성, account_type="paper" 확인
- paper_trade_id 연결 확인
- thesis 필수값 검증

**close_paper_journal:**
- 매도 시 active journal close, pnl_pct 자동 계산
- FIFO 정책: 같은 종목 여러 active journal 중 오래된 것 close
- journal 없으면 None 반환 (에러 아님)

**compare_strategies:**
- closed journals만으로 집계 계산
- active journals가 win_rate/avg_pnl_pct/best/worst_trade에서 제외
- strategy_name 필터가 TradeJournal.strategy 기준으로 적용
- include_live_comparison=False → live_vs_paper 빈 배열
- 같은 기간 내 동일 종목 live/paper journal → 비교 결과 생성
- live 또는 paper 한쪽만 → live_vs_paper에 미포함

**recommend_go_live:**
- 세 기준 모두 충족 → "go_live"
- 거래 수/승률/수익률 각각 미달 → "not_ready"
- active journal은 집계에서 제외
- 커스텀 기준값 override 동작
- 존재하지 않는 account_name → 에러

### MCP 도구 테스트

- `create_paper_account(strategy_name=...)` 정상 동작
- `list_paper_accounts(strategy_name=...)` 필터 동작
- `get_trade_journal(account_type="live")` 기본 동작 유지
- `get_trade_journal(account_type="paper")` paper journal만 반환
- `save_trade_journal(account_type="live", paper_trade_id=123)` → 오류
- `save_trade_journal(account_type="paper", account=None)` → 오류
- `_serialize_journal()`에 account_type, paper_trade_id 포함
