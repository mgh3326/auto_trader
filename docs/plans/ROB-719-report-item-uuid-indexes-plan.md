# ROB-719 — `report_item_uuid` 인덱스 추가 (715 조인 경로)

**우선순위**: Low · **스코프**: additive index migration ×2 (인덱스 2개, 단일 파일) · **migration**: up/down 왕복 clean

## 배경 / 문제

ROB-715의 번들 배치맵(`app/services/investment_reports/item_loop_links.py`)이
리포트 상세를 열 때마다 아래처럼 `report_item_uuid` 로 세트 조인한다:

- `TradeForecast.report_item_uuid.in_(keys)` (`item_loop_links.py:70`)
- `TradeRetrospective.report_item_uuid.in_(keys)` (`item_loop_links.py:93`)

그런데 두 컬럼 모두 **인덱스 없는 plain `Text`** 이다:

- `app/models/review.py:1041` — `TradeRetrospective.report_item_uuid`
- `app/models/review.py:1161` — `TradeForecast.report_item_uuid`

인덱스 목록(`1022-1032` / `1141-1145`)에 둘 다 빠져 있다. 현재는 테이블이 작아
seq-scan 이 무해하나, **ROB-714 place-time 자동 forecast 발행**으로 주문마다
forecast 볼륨이 쌓이면 리포트 상세를 열 때마다 스캔 비용이 커진다.

`.in_(keys)` 는 순수 동등-집합 술어 → **btree 인덱스가 정확히 맞는 도구**.

## 목표 / 비목표

- **목표**: 두 컬럼에 btree 인덱스 추가 (모델 선언 + additive migration). 조인 경로가
  인덱스 스캔 가능 상태가 되게 한다.
- **비목표**: 코드/쿼리 변경 없음. FK·NOT NULL·CHECK 추가 없음. 컬럼 타입 변경 없음.
  스케줄러/배포 게이트 변경 없음.

## 확정 결정 (사용자 승인)

1. **단일 마이그레이션 파일** 에 인덱스 2개 create/drop 대칭 (ROB-714 선례).
2. **로컬 up/down 왕복 검증** 수행. 프로덕션 적용은 관례대로 operator.
3. **plain `CREATE INDEX`** (소규모 테이블 · alembic 인-트랜잭션 기본). `CONCURRENTLY` 불필요.

## 변경 사항

### 1) 모델 선언 (`app/models/review.py`)

기존 `Index(...)` 컨벤션(`ix_<full_table>_<col>`, full 테이블명)에 맞춰 추가:

- `TradeRetrospective.__table_args__` (line 1026 `ix_trade_retrospectives_report_uuid` 다음):
  ```python
  Index("ix_trade_retrospectives_report_item_uuid", "report_item_uuid"),
  ```
- `TradeForecast.__table_args__` (line 1144 `ix_trade_forecasts_correlation_id` 다음):
  ```python
  Index("ix_trade_forecasts_report_item_uuid", "report_item_uuid"),
  ```

### 2) 마이그레이션 (`alembic/versions/20260706_rob719_report_item_uuid_indexes.py`)

- `revision = "20260706_rob719"` (15자 ≤ 32 — `test_alembic_revision_ids.py` 통과)
- `down_revision = "20260705_rob714"` (현재 단일 head)
- ROB-714 구조 그대로: `op.create_index(name, table, ["report_item_uuid"], schema="review")` /
  `op.drop_index(name, table_name=table, schema="review")` 대칭.

```python
"""ROB-719 report_item_uuid indexes

Revision ID: 20260706_rob719
Revises: 20260705_rob714
Create Date: 2026-07-06 00:00:00.000000

Additive btree indexes on ``review.trade_forecasts(report_item_uuid)`` and
``review.trade_retrospectives(report_item_uuid)`` so the ROB-715 report-detail
bundle batch-map join (``item_loop_links.py`` ``.in_(...)``) stays cheap as
ROB-714 place-time forecast volume grows. Purely additive — no column/constraint
change.
"""
from __future__ import annotations
from collections.abc import Sequence
from alembic import op

revision: str = "20260706_rob719"
down_revision: str | Sequence[str] | None = "20260705_rob714"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = {
    "trade_forecasts": "ix_trade_forecasts_report_item_uuid",
    "trade_retrospectives": "ix_trade_retrospectives_report_item_uuid",
}


def upgrade() -> None:
    for table, index in _INDEXES.items():
        op.create_index(index, table, ["report_item_uuid"], schema="review")


def downgrade() -> None:
    for table, index in _INDEXES.items():
        op.drop_index(index, table_name=table, schema="review")
```

### double-prefix 함정 회피 (ROB-705 교훈)

- 인덱스명을 **명시 문자열** `ix_<table>_report_item_uuid` 로 지정 (autogenerate가
  리비전 접두어를 이름에 섞는 `20260705_rob705` 류 사고 방지).
- 모델 `Index(...)` 이름과 migration `op.create_index` 이름을 **정확히 일치**시켜
  autogenerate diff 가 비어 있게 한다.

## 검증

1. `uv run alembic upgrade head` — 인덱스 2개 생성
2. `uv run alembic downgrade -1` — 2개 drop (clean)
3. `uv run alembic upgrade head` — 재적용 clean (왕복 성공 기준)
4. `uv run alembic check` (또는 `--autogenerate` 임시 리비전) — **모델↔DB diff 없음** 확인
   → 모델 `Index` 선언과 migration 이름이 일치함을 증명
5. `uv run pytest tests/test_alembic_revision_ids.py -q` — 리비전 ID/참조 규칙 통과
6. (선택) `EXPLAIN` 로 조인 경로가 인덱스 사용 가능함 확인.
   ⚠️ 소규모 테이블에서는 Postgres 플래너가 여전히 seq-scan 을 고를 수 있으므로,
   `SET enable_seqscan = off;` 후 `EXPLAIN ... WHERE report_item_uuid IN (...)` 로
   **인덱스가 존재하고 플래너가 쓸 수 있음**을 확인하는 것이 정직한 체크.
   (실제 인덱스 스캔 선택은 볼륨이 커진 뒤에만 관측됨.)

## 리스크

- **매우 낮음.** 순수 additive, 코드/쿼리 미변경, 롤백 대칭. 소규모 테이블이라
  인덱스 생성 락 시간도 무시할 수준.

## 산출물

- `app/models/review.py` (Index 선언 2줄)
- `alembic/versions/20260706_rob719_report_item_uuid_indexes.py` (신규)
- PR base `main`, migration 0개 스키마 파괴 없음 (additive only)
