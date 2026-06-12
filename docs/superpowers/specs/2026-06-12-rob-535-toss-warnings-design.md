# [Feature] 토스 종목 경고(warnings) 매수 가드 및 동기화 (ROB-535)

## 1. 배경 및 목적
현재 시스템은 국내 주식의 VI 발동, 투자경고, 정리매매, 거래정지 등 동적 위험 상태를 수집하거나 감지하지 못함. 이를 위해 토스증권 API를 활용하여 주문 전 실시간 가드와 보유/관심 종목에 대한 일배치 동기화 기능을 구현함.

## 2. 설계 상세

### 2.1 데이터베이스 스키마 (`kr_stock_warnings`)
동적 경고 데이터를 저장하기 위한 별도 테이블을 신설함.
- **Table Name**: `kr_stock_warnings`
- **Columns**:
    - `id`: BigInt (PK, autoincrement)
    - `market`: String(10) (예: 'kr', 'us')
    - `symbol`: String(10) (종목 코드)
    - `warning_type`: String(50) (Unknown 허용)
    - `exchange`: String(20), nullable (거래소)
    - `start_date`: Date, nullable
    - `end_date`: Date, nullable
    - `source`: String(32), default 'toss_openapi'
    - `fetched_at`: DateTime(timezone=True)
- **Index**: `(market, symbol)` - 조회 성능 최적화

### 2.2 Toss API 연동 (`app/services/brokers/toss`)
- **`dto.py`**: `TossWarningInfo` 데이터 클래스 및 파서 추가. 8종 enum(LIQUIDATION_TRADING, VI_STATIC 등) 대응.
- **`client.py`**: `GET /api/v1/stocks/{symbol}/warnings` 호출 메서드 추가.

### 2.3 주문 가드 헬퍼 (`warnings_guard.py`)
- **Location**: `app/services/brokers/toss/warnings_guard.py`
- **Logic**:
    - `market == "equity_kr"` 종목만 실조회 수행 (US는 스킵하여 latency 최적화).
    - **Fail-open**: API 조회 실패 시 로그를 남기고 주문 진행 허용.
    - **Blocking Policy**: `_BLOCKING_WARNING_TYPES = {"LIQUIDATION_TRADING"}`에 해당하는 경우 `ok=False` 반환. 그 외는 정보만 제공.
    - **Timeout**: 2~3초 이내로 제한.

### 2.4 MCP 도구 배선
- **`toss_preview_order`**: 주문 미리보기 결과에 현재 활성 경고 목록 노출.
- **`toss_place_order`**: `confirm=True` 시점에 가드 호출. 차단 유형 발견 시 주문 실행 중단.
- **`kis_live_place_order`**: KR live buy 경로에서도 동일한 Toss 실시간 경고 조회를 수행. `dry_run=True`는 활성 경고를 표시하고, 실주문은 활성 `LIQUIDATION_TRADING` 발견 시 KIS POST 전 차단.

### 2.5 동기화 서비스 (`sync_toss_warnings`)
- **Semantic**: Per-symbol replace.
- 심볼별로 기존 데이터를 모두 삭제하고 현재 API 결과로 전체 교체하여 `NULL` 값 정합성 문제 회피.
- `is_active` 필드 없이 `start_date`와 `end_date`를 기준으로 조회 시점에 판정.
- 기본 일배치 대상은 Toss 보유, 활성 수동보유, 활성 watch alert 종목으로 제한. 전체 KR/US 유니버스 폴링은 Toss API 처리량과 비용 리스크 때문에 기본 경로에서 제외.
- 운영자가 명시한 `symbols` 입력은 그대로 정규화하여 sync 가능.

## 3. 제약 및 고려사항
- 주문 가드는 항상 실조회 API를 사용하여 적시성 확보.
- DB 테이블은 리포트 및 브리핑 생성용으로만 활용.
- ROB-534(`kr_symbol_universe` 수정)와의 마이그레이션 충돌 방지를 위해 별도 테이블 유지.
- 이 변경은 DB migration, 스케줄러, live order guard를 건드리므로 `high_risk_change`, `needs_stronger_model_review`, `hold_for_final_review` 대상이다. 병합/배포/실거래 사용 전 CTO/Opus급 최종 검토가 필요하다.
