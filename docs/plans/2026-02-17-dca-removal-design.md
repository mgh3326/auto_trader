# DCA 기능 전면 제거 설계

## 메타
- 날짜: 2026-02-17
- 범위: `create_dca_plan`, `get_dca_status` 제거를 포함한 DCA 관련 런타임 코드 전면 제거
- 비범위: 기존 운영 DB에서 DCA 테이블 즉시 삭제

## 배경
- 현재 DCA 기능은 MCP 공개 툴, DCA 전용 서비스/모델, 체결 모니터 보조 로직으로 분산되어 있다.
- 사용자 요구사항은 오버엔지니어링된 DCA 경로를 코드에서 제거하는 것이다.
- 단, 이미 운영 환경에 존재할 수 있는 DB 테이블(`dca_plans`, `dca_plan_steps`)은 지금 즉시 정리하지 않는다.

## 목표
- MCP 공개 계약에서 DCA 툴(`create_dca_plan`, `get_dca_status`) 제거
- DCA 전용 런타임 코드(`portfolio_dca_*`, `DcaService`, `DcaPlan*`) 제거
- 실시간 체결 모니터의 DCA 연동 제거
- 테스트/문서를 새 계약에 맞게 정리

## 제약 및 정책
- 운영 안정성 우선: DB 테이블 drop 마이그레이션은 이번 범위에서 제외
- 코드/문서 동기화: MCP 툴 계약 변경 시 `app/mcp_server/README.md` 동시 갱신
- 신규 환경의 DCA 테이블 생성 차단은 후속 migration rebaseline 작업으로 분리

## 접근안 비교
1. A안 (채택): 런타임 완전 제거 + 기존 마이그레이션 브랜치는 보존
- 장점: 운영 DB 영향 최소, 코드 복잡도 즉시 감소, 회귀 범위 예측 가능
- 단점: 신규 DB에서 `upgrade head` 시 DCA 테이블 생성 가능성이 남음

2. B안: 런타임 제거 + 과거 DCA 리비전 즉시 재작성
- 장점: 신규 DB 생성 경로 즉시 정리
- 단점: 이미 적용된 DB와 revision 정합성 리스크 큼

3. C안: MCP만 제거하고 내부 DCA 스텁 유지
- 장점: 변경량 감소
- 단점: 전면 제거 요구와 불일치, 죽은 코드 잔존

## 상세 설계

### 1) MCP 계약 및 도메인 제거
- `app/mcp_server/__init__.py`
  - `AVAILABLE_TOOL_NAMES`에서 `create_dca_plan`, `get_dca_status` 제거
- `app/mcp_server/tooling/portfolio_holdings.py`
  - DCA 관련 import 제거
  - `PORTFOLIO_TOOL_NAMES`에서 DCA 툴 제거
  - `@mcp.tool(name="create_dca_plan")`, `@mcp.tool(name="get_dca_status")` 제거
- 삭제 파일
  - `app/mcp_server/tooling/portfolio_dca_core.py`
  - `app/mcp_server/tooling/portfolio_dca_status.py`

### 2) 서비스/모델 레이어 제거
- 삭제 파일
  - `app/services/dca_service.py`
  - `app/models/dca_plan.py`
- `app/models/__init__.py`
  - DCA 모델/enum export 제거

### 3) 모니터링 경로 정리
- `kis_websocket_monitor.py`
  - `DcaService` 의존 제거
  - `_initialize_dca_service`, `_update_dca_step`, `dca_service` 상태 제거
  - `_on_execution`은 주문 ID 유무와 무관하게 체결 이벤트 publish 중심으로 단순화
- `app/services/execution_event.py`
  - 주석/설명에서 `dca_next_step` 옵션 필드 언급 제거

### 4) 지표 모듈 정리
- `app/mcp_server/tooling/market_data_indicators.py`
  - DCA 전용 helper `_compute_dca_price_levels` 제거
  - 관련 `__all__` 항목 정리

### 5) 테스트/문서 정리
- 테스트
  - `tests/test_mcp_server_tools.py` 내 DCA 관련 테스트 블록 제거
  - `tests/test_dca_service.py` 삭제
  - `tests/test_kis_websocket_monitor.py`의 DCA 연동 테스트를 non-DCA 동작 기준으로 갱신
  - `tests/test_execution_event.py`의 `dca_next_step` 의존 케이스 제거/갱신
  - `tests/test_mcp_tool_registration.py`은 제거된 툴 목록 기준으로 자동 정합 확인
- 문서
  - `app/mcp_server/README.md` 툴 목록/스펙에서 DCA 툴 내용 제거
  - AGENTS/플랜 문서 내 DCA 언급은 필요한 최소 범위로 업데이트

## 에러 처리 및 호환성
- 계약 변경 성격: DCA MCP 툴 호출은 더 이상 불가
- 체결 이벤트 호환성: `dca_next_step` 부가 필드 제거로 소비자가 해당 필드 optional 가정 필요
- 내부 코드 참조: `rg` 기준 DCA 타입/서비스 참조가 0이 되는 것을 완료 조건으로 사용

## 검증 계획
- 필수 테스트
  - `uv run pytest tests/test_mcp_tool_registration.py -q`
  - `uv run pytest tests/test_mcp_server_tools.py -q`
  - `uv run pytest tests/test_kis_websocket_monitor.py -q`
  - `uv run pytest tests/test_execution_event.py -q`
- 정적 검증
  - `uv run ruff check app tests`
  - `uv run pyright app`
- 검색 검증
  - `rg -n "create_dca_plan|get_dca_status|DcaService|from app.models.dca_plan|dca_next_step" app tests`

## 롤아웃/리스크 통제
- 이번 변경은 DB 스키마 미변경이므로 롤백은 코드 복구 중심으로 단순하다.
- 배포 후 모니터링 포인트:
  - MCP tool registration 오류 여부
  - KIS websocket monitor 실행 오류 여부
  - 체결 이벤트 publish 정상 여부

## 후속 작업 (별도 트랙)
- 목적: 신규 환경에서 DCA 테이블이 생성되지 않도록 migration chain 정리
- 방식: 기존 DB를 고려한 migration rebaseline/squash 계획 수립 및 단계적 이행
- 주의: `add_dca_plans_and_steps` 리비전 파일 단순 삭제/수정은 기존 DB와 revision 불일치 위험이 높으므로 금지
