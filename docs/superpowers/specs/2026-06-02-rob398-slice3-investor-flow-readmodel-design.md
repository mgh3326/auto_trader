# ROB-398 Slice 3 — 투자자 플로우 read-model query_service + investor_flow collector 설계

- **이슈**: ROB-398 (오케스트레이션 ROB-411 C라인) — Slice 3 of 4
- **상태**: design (브레인스토밍 승인 완료)
- **날짜**: 2026-06-02
- **선행/관련**: ROB-398 Slice 1(뉴스매핑)·Slice 2(모멘텀 랭킹, main a8b6f4b8), ROB-397(symbol_analysis `FlowData`), ROB-276(double_buy screener).

---

## 1. 배경 — 기존 인프라와 갭

탐색 결과 투자자 매매동향 적재는 이미 존재한다 (Slice 1·2와 동일 패턴):

- `InvestorFlowSnapshot`(`app/models/investor_flow_snapshot.py`) — `foreign_net`/`institution_net`/`individual_net`, `double_buy`/`double_sell`(파생 플래그), 투자자별 `*_consecutive_buy_days`/`*_consecutive_sell_days`, `*_net_buy_rank`/`*_net_sell_rank`, `snapshot_date`(Date), `source`(naver_finance/kis/manual), `collected_at`.
- 적재: `app/services/investor_flow_snapshots/`(builder/repository) + `naver_finance/investor.py::fetch_investor_trends`(finance.naver.com/item/frgn.naver) + job `app/jobs/investor_flow_snapshots.py`. view-model read는 `invest_view_model/investor_flow_service.py`. screener double_buy 프리셋(ROB-276)이 소비.
- `INVEST_DATA_SOURCE_CONTRACT`에 `investor_flow_snapshots` 엔트리 이미 존재(`authority_tier="supplementary"`, `collector_snapshot_kind=None` — "folded into candidate_universe / future collector").

**갭**: 투자자 플로우가 적재·저장되지만 (a) **freshness 명시 read-model query_service가 없고**, (b) **번들 evidence로 연결돼 있지 않다**(`investor_flow` snapshot_kind 미존재, 397 `FlowData` 미피드). **체결강도(trade strength)는 컬럼·소스 모두 GREENFIELD**.

## 2. 목표·범위

기존 `InvestorFlowSnapshot` 위에 **read-only query_service + `investor_flow` 번들 collector**를 추가해 397 `FlowData`(foreign_net/inst_net/double_buy/sell/consec_days)를 피드한다.

- **새 적재 경로 없음**(기존 builder+job 재사용).
- 새 `investor_flow` snapshot_kind → **additive CHECK migration + 6곳 동기**(§4).
- collector **optional/non-blocking**.
- **체결강도 제외** (별도 후속: 소스 결정 + `investor_flow_snapshots` 컬럼 추가 + Naver 파서/KIS 변경).

**안전 경계** (ROB-411 상속): broker/order/watch/order-intent mutation 없음, production DB backfill/commit ingest 없음, scheduler activation 없음, Naver는 reference/calibration(supplementary), stale/부재 숨김 없음. screener double_buy 소비자 미변경.

## 3. read-model query_service — `app/services/investor_flow_snapshots/query_service.py` (신규)

기존 `InvestorFlowSnapshotsRepository` read 위 thin freshness 래퍼 (Slice 2 momentum query_service와 대칭).

frozen dataclass:

```python
@dataclass(frozen=True)
class InvestorFlowRow:
    symbol: str
    foreign_net: int | None
    institution_net: int | None
    individual_net: int | None
    double_buy: bool
    double_sell: bool
    foreign_consecutive_buy_days: int | None
    foreign_consecutive_sell_days: int | None
    institution_consecutive_buy_days: int | None
    institution_consecutive_sell_days: int | None

@dataclass(frozen=True)
class Freshness:
    overall: str               # "fresh" | "stale" | "unavailable"
    snapshot_date: date | None
    stale_reason: str | None
    age_days: int | None

@dataclass(frozen=True)
class InvestorFlow:
    market: str                # "kr"
    snapshot_date: date | None
    rows: tuple[InvestorFlowRow, ...]
    freshness: Freshness
```

메서드: `get_investor_flow(*, symbols=None, market="kr", limit=50, now, ttl_days=1) -> InvestorFlow`.

## 4. freshness (일 단위)

투자자 플로우는 EOD 일별 데이터(`snapshot_date` Date) — 분 단위 TTL 부적합.

- 0행 → `unavailable`, `stale_reason="no_flow_rows"`, `age_days=None`.
- `age_days = (now.astimezone(KST).date() - snapshot_date).days`. `age_days <= ttl_days`(기본 1: 당일/전일, EOD 발표 지연 흡수) → `fresh`.
- 초과 → `stale`, `stale_reason="older_than_ttl"`, `age_days` 동봉.

(`now` 주입 가능 — 테스트 결정성.)

## 5. collector — `app/services/action_report/snapshot_backed/collectors/investor_flow.py` (신규)

- `snapshot_kind = "investor_flow"`.
- `collect(request)`: `request.market != "kr"`면 `unavailable_result`. query_service로 플로우 읽어 `build_result(snapshot_kind="investor_flow", market=..., account_scope=..., payload=..., origin="auto_trader_db", as_of=..., freshness_status=...)`. payload: `{market, snapshot_date, freshness, rows: [InvestorFlowRow asdict]}`. 조회 예외/0행 → `unavailable_result` degrade(크래시 금지).
- query `stale` → snapshot row `freshness_status="soft_stale"`(모델 CHECK 허용값), `fresh`→`fresh`, `unavailable`→`unavailable_result`.
- registry `production_collector_registry` 등록 + policy **optional/non-blocking**(soft_ttl=900, hard_ttl=86400[일 단위] — plan에서 확정).

## 6. 6곳 동기 (Slice 2 교훈)

새 `investor_flow` kind 도입 시 동기 필수:
- (a) `app/models/investment_snapshots.py` CHECK에 `'investor_flow'` 추가.
- (b) `app/schemas/investment_snapshots.py` `SnapshotKind` Literal에 `"investor_flow"` 추가.
- (c) `production_collector_registry`에 `InvestorFlowSnapshotCollector` 등록.
- (d) `policy.py`에 `investor_flow` `SnapshotKindPolicy`(optional) 추가.
- (e) **기존 `investor_flow_snapshots` contract 엔트리의 `collector_snapshot_kind=None` → `"investor_flow"`** (새 엔트리 추가 아님 — `test_every_collector_kind_has_exactly_one_entry` 충족).
- (f) `docs/invest/data-source-contract.md` GENERATED matrix 블록 재렌더(`render_contract_matrix_markdown()`).
- additive CHECK migration(ROB-329 템플릿, `down_revision`=구현 시점 `alembic heads` 실값, operator가 `alembic upgrade head` 별도 실행).

## 7. 테스트 (TDD)

1. query_service freshness: snapshot_date 당일→fresh; 전일→fresh(ttl_days=1); 3일 전→stale(older_than_ttl, age_days=3); 0행→unavailable(no_flow_rows).
2. query_service 매핑: repository → InvestorFlowRow(double_buy/double_sell/consec_days 보존).
3. collector: fresh → build_result payload(rows+freshness); 예외/0행 → unavailable_result(degrade); stale→soft_stale.
4. drift-guard 회귀: `collector_wired_kinds()` == 런타임 registry(이제 `investor_flow` 포함); contract 엔트리 정확히 1개; 모델/schema CHECK에 `investor_flow` + 기존 kind 전부 보존; doc matrix 동기.
5. repository는 fake/in-memory로 DB-free 단위테스트.

## 8. 비목표 (YAGNI)

- **체결강도(trade strength)** — 별도 후속(소스 결정 + `investor_flow_snapshots` 컬럼 + Naver 파서/KIS 변경).
- 새 적재 경로/CLI(기존 builder+job 재사용), scheduler activation.
- screener double_buy 소비자 / 리포트 배선 변경.
- 397 `SymbolAnalysis` 런타임 통합(계약 피드 형태만; 실제 머티리얼라이즈는 후속).
- Toss screener(Slice 4).
