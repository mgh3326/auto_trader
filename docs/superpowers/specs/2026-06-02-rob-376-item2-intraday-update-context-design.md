# ROB-376 item 2 — `intraday_update` 리포트 타입 + 델타 consumer (설계)

- **이슈**: ROB-376 (오케스트레이션 ROB-412 D라인 마지막). PR1(`investment_report_delta_get` 델타 도구) MERGED(`0b52d266`). 본 문서 = **item 2** = intraday 리포트 타입 + 델타를 Hermes가 pull하도록 결합.
- **날짜**: 2026-06-02
- **상태**: 설계 승인됨 → 플랜 작성 단계
- **범위 경계**: read-only. broker/order/watch/order-intent mutation 없음. no in-process LLM(Hermes compose). migration 0. gate = 기존 `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED`.

## 1. 배경 / 현재 상태 (코드 검증)

- `report_type`은 **자유 문자열**(model `investment_reports.report_type: Mapped[str]`, schema `IngestReportRequest.report_type: str` — enum/CHECK 없음). → `intraday_update_v1` 도입에 마이그레이션 불필요.
- 델타 도구 `DeltaService.compute_delta(report_uuid, *, near_pct=1.0, account_type="live", computed_at_kst=None)`는 PR1로 존재. 반환: `{success, baseline_report_uuid, market, levels_delta, holdings_pnl_delta, index_delta, computed_at_kst, unavailable?}` 또는 `{success:false, error:"baseline_not_found"|"invalid_report_uuid"}`.
- Hermes context는 `HermesContextExporter.export(*, snapshot_bundle_uuid) -> HermesContextPayload`가 결정적으로 생성(차원 evidence/진단/coverage). 현재 baseline 리포트/델타 개념 없음.
- `HermesContextPayload`(`app/schemas/hermes_composition.py`)는 `context_version: Literal["hermes-context.v1"]` + 다수 dict 필드. **델타 블록 없음**(자유롭게 additive 확장 가능).
- Hermes 경로 gate: `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED`(기본 False), `prepare_bundle`/`get_hermes_context`/`create_from_hermes_composition` 등에서 enforce.
- `create_from_hermes_composition`은 `report_type` free 수용(기본 `snapshot_backed_advisory_v1`, override 자유).

## 2. 결정 (Option A — 전용 intraday context 조립 도구)

신규 read-only MCP 도구가 (기존 번들 context) + (baseline 대비 델타 블록)을 **하나의 pullable 봉투**로 조립한다. Hermes는 단일 context pull로 intraday_update 리포트를 compose한다.

## 3. 변경

### 3.1 스키마 (additive)
`app/schemas/hermes_composition.py` `HermesContextPayload`에 optional 필드 2개 추가:
```python
baseline_report_uuid: uuid.UUID | None = None
intraday_delta_block: dict[str, Any] | None = None
```
- `context_version="hermes-context.v1"` 유지(필드 additive·optional → 하위호환). 기존 `investment_report_get_hermes_context`는 둘 다 `None`으로 직렬화 → 회귀 없음.

### 3.2 신규 MCP 도구
`app/mcp_server/tooling/investment_hermes_handlers.py`:
```python
async def investment_report_prepare_intraday_context_impl(
    snapshot_bundle_uuid: str,
    baseline_report_uuid: str,
    near_pct: float = 1.0,
    account_type: str = "live",
) -> dict: ...
```
흐름:
1. **gate**: `settings.SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` False → `{"success": False, "error": "snapshot_backed_report_generator_disabled"}` (기존 Hermes 도구 메시지 패턴 따름).
2. `payload = await HermesContextExporter(...).export(snapshot_bundle_uuid=UUID(...))` — 현재 번들 결정적 context.
3. 델타: `DeltaService(db).compute_delta(baseline_report_uuid, near_pct=near_pct, account_type=account_type)`.
   - **fail-open**: `compute_delta`가 `success:false`면 그 dict를 그대로 `intraday_delta_block`에 담아 **이유 노출**(예: `{"success": false, "error": "baseline_not_found"}`). `DeltaService`/exporter 예외는 try/except로 잡아 `intraday_delta_block = {"unavailable": "<reason>"}`. **context 자체는 success 유지.**
4. `payload.baseline_report_uuid = <UUID>`, `payload.intraday_delta_block = <delta or error/unavailable>`.
5. 반환: `{"success": True, "report_type_hint": "intraday_update_v1", "context": payload.model_dump(mode="json", by_alias=True)}` (기존 `get_hermes_context` 반환 envelope과 일관되게; 실제 형태는 기존 도구 반환을 미러).
6. 등록: `register_*`에 `mcp.tool(name="investment_report_prepare_intraday_context", ...)`, `__all__` + 해당 도구-네임 set(있으면)에 추가.

### 3.3 report_type 규약
모듈 상수 `INTRADAY_UPDATE_REPORT_TYPE = "intraday_update_v1"` (hermes handlers 또는 인접 상수 위치). `create_from_hermes_composition`은 free `report_type` 수용하므로 Hermes가 이 값으로 ingest. baseline 연결은 ingest 시 `previous_report_uuid`로 기록 가능(기존 필드). enum/CHECK/마이그레이션 없음.

## 4. 테스트

`tests/mcp_server/test_investment_hermes_tools.py`(또는 신규 `test_investment_report_intraday_context.py`):
- **gate off** → `{"success": False, "error": "...disabled"}`.
- **정상 baseline** → 반환 context에 `intraday_delta_block`이 델타(success:true) + `baseline_report_uuid` 세팅. (exporter + DeltaService를 주입/모킹하거나, 기존 hermes 테스트의 시드 패턴 재사용.)
- **bad baseline**(unknown/invalid uuid) → `intraday_delta_block`에 error dict, context `success` 유지(fail-open).
- **예외 격리** → exporter는 정상이고 delta가 raise → `intraday_delta_block={"unavailable":...}`, context success.
- **스키마 회귀**: `HermesContextPayload`에 두 필드 additive; 기존 `investment_report_get_hermes_context`는 None 직렬화.
- **report_type round-trip**: `create_from_hermes_composition(report_type="intraday_update_v1", ...)`가 자유 문자열 수용·저장.
- registration(도구 등록) + 단일 alembic head(마이그레이션 없음) + no-mutation(broker/order grep 0).

## 5. 안전경계 / 비범위

- read-only: broker/order/watch/order-intent mutation 없음. scanner/activate/decide 무접촉.
- **no in-process LLM** — 결정적 evidence만 조립; Hermes가 compose/push.
- **migration 0**(report_type 자유 문자열, 스키마 필드 additive).
- gate = 기존 `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED`(신규 env 없음).
- **비범위**: 실제 Hermes intraday 합성 라운드트립(레포 밖, operator-gated, ROB-287/413 계열). 급변주(`screen_stocks`)/뉴스(`get_market_issues`) 신규 신호(델타는 PR1의 3신호 유지).

## 6. 완료 기준 (ROB-376 item 2)

- ✅ intraday_update 리포트 타입 규약(`intraday_update_v1`, 자유 문자열) 도입.
- ✅ 델타가 Hermes가 pull 가능한 단일 context evidence(`intraday_delta_block`)로 결합 — report-vs-now/prior 델타 consumer 성립.
- ✅ read-only/gated/migration 0, no LLM.
- → PR1(델타 도구) + 본 PR(intraday context 결합)으로 **ROB-376 auto_trader 측 작업 종료**. 실 Hermes 합성은 operator-gated 후속.
