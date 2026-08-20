# ROB-1300 — 상방여력 stale 입력 신선화 인계·구현 보고서

## 1. 인계 판단 근거

- 착수 HEAD: `1fb649be44236e553386f80fe4f5eb2cad078fab`
- base 확인: `origin/main`은 #1902 (`1bcf9edc5`), #1903 (`15069da31`), #1906
  (`5435f2cf7`)까지 전진했다. 인계 worktree가 15개 파일의 uncommitted 변경을
  가졌으므로, 먼저 전체 diff를 검토·검증해 하나의 task commit으로 보존한 뒤 최신
  `origin/main`에 rebase한다. #1902/#1903은 이 표면과 무관하고 #1906은 테스트
  경계 갱신이므로, 충돌이 나면 최신 테스트 경계를 우선한다.
- rebase 결과: 충돌 없이 `5435f2cf7` 위에 재적용했다. 이 보고서는 해당
  rebase 후 task commit에 포함한다.

### 전임자 변경 — 파일별 판정

| 파일 | 전임자 작업 | 판정 |
| --- | --- | --- |
| `app/services/analyst_normalizer.py` | 기존 ROB-486 stale-window 메타데이터를 읽는 helper와 mixed-stale target/upside null 처리 | 유지. stale 입력으로 남은 행 평균을 조용히 쓰지 않는 핵심 fail-closed 경계다. helper는 alias 불일치 시 stale을 놓칠 수 있어 보정한다. |
| `app/mcp_server/tooling/buy_candidate_fanout.py` | stale-window 입력이면 upside 관문을 `honest_upside_stale_inputs`로 fail | 유지. 이유와 제외 행 수를 명시하고 이후 관문을 미평가로 끝낸다. |
| `app/services/invest_view_model/analyst_consensus_cache.py` | cache에 남은 평균으로 upside를 재계산하지 않음 | 유지. 당일 cache hit가 stale 입력을 되살리지 않게 한다. |
| `app/services/invest_view_model/screener_analysis_enrichment.py` | stale mixed consensus의 목표가/upside를 화면 모델에 복사하지 않고 warning 추가 | 유지. 기존 스크리너 소비자에 명시적 `consensus_stale_window_inputs`를 전달한다. |
| `app/mcp_server/tooling/fundamentals_sources_common.py` | screen-enrichment payload에서 stale 평균/upside 차단 | 유지. 별도 스크리너 payload 경로도 같은 경계를 탄다. |
| `app/mcp_server/tooling/fundamentals_handlers.py` | MCP 설명을 null + metadata 계약으로 수정 | 유지. 소비자가 `rows_excluded_stale`를 확인할 수 있게 한다. |
| `app/services/naver_finance/investor.py` | Naver 의견 API 문서화 갱신 | 유지. 원 데이터와 집계 계약을 맞춘다. |
| `docs/runbooks/buy-candidate-fanout.md` | fanout fail-closed 사유 문서화 | 유지. 운영자가 numeric leftover를 pass로 해석하지 않게 한다. |
| `tests/test_analyst_normalizer.py` | 대한유화 8/10 survivor mutant 및 all-fresh 전환 증명 | 유지·보강. stale alias 불일치 mutant도 추가한다. |
| `tests/mcp_server/tooling/test_buy_candidate_fanout.py` | stale numeric target이 upside pass가 되지 않는 mutant | 유지. |
| `tests/services/test_analyst_consensus_cache.py` | cache 재계산이 stale target을 부활시키지 않는 mutant | 유지. |
| `tests/services/test_screener_analysis_enrichment.py` | screen 모델이 stale target을 표시하지 않고 warning을 내는 mutant | 유지. |
| `tests/test_naver_finance.py` | Naver live-assembly mixed-stale 기대값을 null로 전환 | 유지. |
| `tests/test_rob486_consensus_blast_radius.py` | 공통 screen payload blast-radius mutant | 유지. |
| `tests/test_stock_detail_research_consensus_service.py` | stock-detail 재집계 및 in-window outlier 회귀 | 유지. |

### 원인 재검증 및 범위

전임자의 "로컬 스냅샷을 갱신하면 해결된다"는 전제는 성립하지 않는다. KR 의견 입력은
`fetch_investment_opinions`가 Naver `company_list`와 report detail을 매 호출 읽어
`build_consensus`에 넘긴다. 상세 cache는 report `nid`별 immutable detail cache이고,
스크리너 cache는 KST 당일 Redis cache-aside일 뿐이다. durable
`analyst_consensus_snapshots`도 이 스크리너 경로의 입력이 아니다. 즉 대한유화의
`2/10 stale`은 이 레포가 묵힌 재무 스냅샷이 아니라 공급자 목록에 포함된 오래된 의견이며,
새 스케줄은 이를 신선하게 만들지 못한다. 스케줄 등록은 승인 없이 금지되어 있으므로
추가하지 않았다.

기존 ROB-486의 12개월 opinion window와 그 메타데이터
(`rows_excluded_stale`/`stale_opinion_count`)만 판정에 사용한다. 새 TTL 또는 임의
신선도 기준은 만들지 않았다. 입력이 stale이면 target 통계와 upside는 `null`이며,
fanout은 `honest_upside_stale_inputs`로 명시 fail한다. 입력 10건이 모두 window 안으로
갱신되면 기존 target/upside 계산과 `target_price_honest=true`가 복구되는 것을 mutant
테스트로 확인한다.
