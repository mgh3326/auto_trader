# ROB-503 — 크립토 리서치 MCP 도구 DEFAULT 복원 + generic 이름 리네임

날짜: 2026-06-10
이슈: [ROB-503](https://linear.app/mgh3326/issue/ROB-503) (Bug/High)
브랜치: `rob-503` (worktree `/Users/mgh3326/work/auto_trader.rob-503`)

## 배경 / 근본 원인

2026-06-10 재배포 후 크립토 레짐/파생 MCP 도구 12종이 일괄 `Unknown tool`이 된 regression.

- **원인은 등록 버그/`on_duplicate` 충돌이 아님** (이슈 본문의 추정과 다름).
- ROB-488(PR #1226, `6bb1954d`)이 크립토 리서치 도구를 `MCP_PROFILE=crypto`
  전용으로 분리(`registry.py`의 `include_crypto_tools = profile is McpProfile.CRYPTO`).
- 운영 launchd plist(`mcp-blue`/`mcp-green`)는 `MCP_PROFILE` 미설정 → DEFAULT
  프로파일 → 크립토 도구 물리 미등록. ROB-488 operator 체크리스트의
  "PROFILE cutover"(crypto 인스턴스 신설)가 수행되지 않은 채 메인 서버만 재배포됨.
- 같은 PR에서 `get_fear_greed_index` → `get_crypto_fear_greed` 리네임도 발생.

운영 현실: 크립토 라이브 매매(BTC DCA 래더)는 DEFAULT 서버의 generic 주문
도구로 수행 중 — "주문은 되는데 판단 근거 조회만 불가"인 비정합 상태.

## 결정 (user 합의)

1. **A안 채택**: 크립토 read-only 리서치 도구를 모든 프로파일에 상시 등록.
   별도 crypto 인스턴스 신설(B안)은 채택하지 않음 — 단일 세션 KR+크립토 운영 유지.
2. **프로파일 분리의 안전 목적(주문 surface 분기)은 보존**: `McpProfile` 체계와
   주문 도구 프로파일 분기는 무변경. 리서치 도구 격리만 철회.
3. **generic 이름은 파라미터가 아닌 리네임으로 해소**:
   - `get_crypto_fear_greed` — ROB-488 리네임 유지 (옛 `get_fear_greed_index` 부활 안 함).
     향후 US/KR 소스가 생겨 generic 역할이 가능해지면 그때 generic 이름으로 작업.
   - `get_funding_rate` → `get_crypto_funding_rate`
   - `get_open_interest` → `get_crypto_open_interest`
   - `get_long_short_ratio` → `get_crypto_long_short_ratio`
   - 4종 모두 현재 어떤 서버에도 미등록(죽은 상태)이라 호출자 0 — 리네임 비용 0인 시점.
4. **US CNN fear-greed 분기는 스코프 제외** (브레인스토밍 중 검토 후 철회 —
   market 파라미터를 도입하지 않으므로 불필요).

## 변경 내역

### 1. 프로파일 게이트 제거 (regression 복구)

- `app/mcp_server/tooling/registry.py`: `include_crypto_tools` 변수 및
  `include_crypto=` 전달 제거. `register_fundamentals_tools(mcp)` /
  `register_analysis_tools(mcp)` 무조건 호출.
- `app/mcp_server/tooling/fundamentals_registration.py`,
  `app/mcp_server/tooling/fundamentals_handlers.py`,
  `app/mcp_server/tooling/analysis_registration.py`:
  `include_crypto` 파라미터와 `if include_crypto:` 분기 제거 — 크립토 도구
  무조건 등록. `CRYPTO_FUNDAMENTALS_TOOL_NAMES` set은 "크립토 도구 분류"
  메타데이터로 유지하되 주석을 "전 프로파일 등록"으로 정정 (또는 테스트에서만
  쓰면 거기로 이동).
- 주문 surface 분기(`register_order_tools`/`register_kis_live_order_tools` 등
  profile별 등록)는 무변경.

### 2. 리네임 (MCP 도구 이름만, impl 함수명 유지)

- `fundamentals_handlers.py`의 `@mcp.tool(name=...)` 3건:
  `get_funding_rate`→`get_crypto_funding_rate`,
  `get_open_interest`→`get_crypto_open_interest`,
  `get_long_short_ratio`→`get_crypto_long_short_ratio`.
- `FUNDAMENTALS_TOOL_NAMES` / `CRYPTO_FUNDAMENTALS_TOOL_NAMES` set 갱신.
- 레포 전체에서 옛 도구 이름 문자열 grep → 도구 description 상호 참조,
  런북, 프롬프트/문서 일괄 정정 (예: `get_crypto_market_regime` description의
  OI 언급, `docs/runbooks/rob449-452-mcp-activation.md`).
- 옛 이름 alias 등록은 하지 않음 (호출자 0이므로 불필요; surface 중복 금지).

### 3. 문서

- `docs/runbooks/rob449-452-mcp-activation.md`: "MCP_PROFILE=crypto 서버에서만
  등록" 노트를 "전 프로파일 등록"으로 정정.
- `app/mcp_server/tooling/registry.py` 모듈 docstring의 프로파일→surface 매핑
  주석 갱신 (DEFAULT에서 crypto-only 도구 omitted 문구 제거).
- CLAUDE.md는 해당 내용 없음 — 무변경.

## 변경하지 않는 것 (Non-goals)

- `McpProfile` enum / `MCP_PROFILE` env 메커니즘 (유지)
- 주문 도구 프로파일 분기, kiwoom/paper/us-paper/db-paper surface (유지)
- crypto 인스턴스 launchd 신설 (불채택)
- US/KR fear-greed 소스 추가, market 파라미터 (future work)
- impl 함수/서비스 레이어, DB (migration 0)

## 테스트 (TDD)

- `tests/test_mcp_profiles.py` 프로파일×도구 매트릭스 갱신:
  - 크립토 리서치 12종(`get_crypto_fear_greed` 포함)이 **모든 프로파일**에 등록됨
  - 옛 이름 4종(`get_fear_greed_index`, `get_funding_rate`, `get_open_interest`,
    `get_long_short_ratio`)은 **어느 프로파일에도 부재**
  - 주문 surface 분기는 기존 단언 유지 (회귀 방지)
- 리네임된 도구의 wiring 테스트(이름→impl 연결)가 기존에 있으면 이름만 갱신.
- 실 HTTP 호출 없음 (등록/이름 수준 테스트).

## 안전 경계

- migration 0, 브로커/주문 mutation 경로 무변경.
- 추가되는 도구 전부 read-only public 데이터 조회.
- DEFAULT surface 76 → 88종 (ROB-488 슬리밍의 부분 후퇴 — 크립토 라이브
  운영이 DEFAULT 서버에서 이뤄지는 현실을 반영한 의도적 결정).

## 검증 (이슈 체크박스 매핑)

- [ ] 배포 후 도구 목록에 12종 전부 노출 (리네임 반영된 새 이름 기준)
- [ ] `get_crypto_fear_greed` / `get_crypto_market_regime` 각 1회 정상 응답 (operator)
- [ ] `get_execution_strength` description이 참조하는 `get_crypto_order_flow`
  실재 — 복원으로 자동 해소
