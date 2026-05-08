# MCP screen_stocks 테스트 fixture 통합 설계

## 배경 및 동기

SonarCloud(`mgh3326_auto_trader`) 측정에서 프로젝트 전체 중복 라인의 가장 큰 단일 카테고리는 MCP `screen_stocks` 관련 테스트 4개 파일이며, 합산 약 3,450줄(전체 13,243줄 중 26%)이 중복으로 잡힌다.

| 파일 | 줄 수 | 중복 줄 | 중복 비율 |
|---|---:|---:|---:|
| `tests/_mcp_screen_stocks_support.py` | 4,214 | 1,709 | 40.5% |
| `tests/test_mcp_screen_stocks_tvscreener_contract.py` | 1,018 | 665 | 65% |
| `tests/test_mcp_screen_stocks_filters_and_rsi.py` | 944 | 612 | 65% |
| `tests/test_mcp_screen_stocks_crypto.py` | 1,462 | 464 | 32% |

원인은 두 가지 구조 결함이다.

### 결함 1 — 헬퍼 클래스 인라인 재정의

`tests/_mcp_tooling_support.py` 모듈 docstring은 *“Import from this module rather than duplicating code across test files.”* 라고 단일 소스를 선언하지만, 다음 두 파일이 같은 헬퍼를 인라인으로 재정의한다.

- `tests/_mcp_screen_stocks_support.py:31–71`
- `tests/test_crypto_composite_score.py:37–87`

재정의된 심볼: `_TvCondition`, `_TvField`, `DummyMCP`, `build_tools`. 더불어 `fake_crypto_tvscreener_module` fixture도 중복.

### 결함 2 — `TestScreenStocksTvScreenerContract` 클래스 양쪽 존재

같은 이름의 테스트 클래스가 두 파일에 존재한다.

- `tests/_mcp_screen_stocks_support.py:641` — 16개 메서드 (1080줄)
- `tests/test_mcp_screen_stocks_tvscreener_contract.py:50` — 14개 메서드 (969줄)

8개 메서드는 양쪽에 동명으로 존재하며 1–2줄 단위의 미세한 구현 차이가 있다. pytest는 모듈을 단위로 collect하므로 두 클래스 모두 발견되며, 결과적으로 **동일 의도의 contract 테스트가 두 곳에서 살짝 다른 형태로 병행 실행**되고 있다. 이는 단순 코드 중복을 넘어 “contract”라는 이름이 무엇을 보장하는지 모호해진 의미적 결함이다.

## 비-목표

- `_mcp_screen_stocks_support.py`의 다른 다섯 테스트 클래스(`TestScreenStocksKR`, `TestScreenStocksKRRegression`, `TestScreenStocksUS`, `TestScreenStocksCrypto`, `TestScreenStocksFundamentalsExpansion`, `TestScreenStocksRsiLogging`)를 도메인별 파일로 분리하는 것 — 후속 PR(스코프 C).
- 다른 도메인(market_events, research_reports 등)의 fixture 통합 — 별 작업.
- 프로덕션 코드(`app/`) 변경 — 본 PR은 테스트 전용.

## 변경 1 — 헬퍼 import 통일

`_mcp_tooling_support.py`를 단일 소스로 확립한다.

1. `tests/_mcp_tooling_support.py` 점검: `_TvCondition`, `_TvField`, `DummyMCP`, `build_tools`가 이미 정의되어 있음. `fake_crypto_tvscreener_module` fixture는 미존재하면 추가한다.
2. `tests/_mcp_screen_stocks_support.py` 31–89행의 인라인 정의 삭제 → `from tests._mcp_tooling_support import …` 로 교체.
3. `tests/test_crypto_composite_score.py` 37–87행의 인라인 정의 삭제 → 동일 import.

위험은 낮다 (`_patch_runtime_attr`은 이미 동일 패턴으로 import되고 있어 의존 그래프 변경이 없음).

## 변경 2 — `TestScreenStocksTvScreenerContract` 통합

`test_mcp_screen_stocks_tvscreener_contract.py`를 정본으로 두고 통합한다.

1. **동명 8개 메서드** — git blame 또는 직접 diff로 더 최신·완전한 쪽 선택. 차이가 의도된 진화면 contract 우선, support 쪽이 더 풍부하면 contract로 옮겨 통합.
2. **support-only 6개 메서드** (`test_kr_tvscreener_enriched_rows_preserve_sector_and_analyst_fields`, `test_us_category_and_analyst_filter_stay_on_tvscreener_without_network_enrichment`, `test_us_enrichment_fallback_only_runs_for_rows_missing_tvscreener_fields`, `test_us_enrichment_fallback_preserves_existing_tvscreener_values`, `test_us_category_preserves_acronym_case_for_tvscreener_filter`, `test_us_category_lowercase_technology_canonicalized_for_tvscreener`, `test_us_category_with_max_rsi_falls_back_to_legacy_path`) — contract 파일로 이전.
3. **contract-only 5개 메서드** — 그대로 보존.
4. `_mcp_screen_stocks_support.py:641` 의 클래스 통째 제거.

contract 파일의 helper 함수(`_stock_capability_snapshot`, `_install_stock_capabilities`, line 12–47)가 옮겨오는 메서드에서 필요한지 확인하고, 필요하면 활용·아니면 그대로 둔다.

## 검증

1. **pytest collection 중복 검사**
   ```bash
   uv run pytest --collect-only -q tests/_mcp_screen_stocks_support.py tests/test_mcp_screen_stocks_tvscreener_contract.py tests/test_mcp_screen_stocks_filters_and_rsi.py tests/test_mcp_screen_stocks_crypto.py tests/test_crypto_composite_score.py | awk -F'::' 'NF>=2{print $2"::"$3}' | sort | uniq -d
   ```
   동명 nodeID가 두 파일에서 발견되는 경우가 0건이어야 한다.

2. **테스트 실행**
   ```bash
   uv run pytest tests/_mcp_screen_stocks_support.py tests/test_mcp_screen_stocks_tvscreener_contract.py tests/test_mcp_screen_stocks_filters_and_rsi.py tests/test_mcp_screen_stocks_crypto.py tests/test_crypto_composite_score.py tests/_mcp_tooling_support.py -v
   ```
   모두 통과.

3. **린트/타입체크**
   ```bash
   make lint
   make typecheck
   ```

4. **테스트 카운트** — 통합 전후 비교. contract 영역 14 + support 영역 16 = 30개에서 동명 8 중복 제거 → 22개 유지가 기대값.

## 위험과 완화

| 위험 | 완화 |
|---|---|
| 동명 8 메서드의 미세 차이가 양쪽 다 의도된 케이스 | 자동 머지 금지. 메서드 단위로 사람이 결정. 실행 단계에서 diff를 한 번에 확인 후 케이스별 보존/병합. |
| support 파일의 다른 5 클래스가 제거된 클래스의 fixture를 의존 | 제거 전 grep으로 reference 확인. fixture는 모듈 스코프이므로 클래스 제거 후에도 외부 클래스가 같은 fixture를 쓰면 영향 없음. |
| contract 파일의 helper 함수와 옮겨온 메서드의 인터페이스 불일치 | 통합 시 옮겨온 메서드를 contract의 helper 패턴에 맞춤(필요 시). 차이가 작으면 그대로 둠. |

## 산출물

- 변경 파일 3개: `tests/_mcp_tooling_support.py`(신규 fixture 추가 가능), `tests/_mcp_screen_stocks_support.py`(인라인 헬퍼 + Contract 클래스 제거), `tests/test_mcp_screen_stocks_tvscreener_contract.py`(통합), `tests/test_crypto_composite_score.py`(인라인 헬퍼 제거).
- 예상 라인 단축: support 파일 4,214 → 약 3,000줄 (1,200줄 감소). 인라인 헬퍼 제거로 추가 ~150줄. SonarCloud `duplicated_lines` 기여도 약 600–900줄 단축.
