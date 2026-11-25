# KR-only Kiwoom MCP profile (`MCP_PROFILE=kiwoom_kr`)

ROB-1159. KR 전용 Kiwoom mock 프로필.

## 왜 있는가

`kiwoom_kr`은 US `kiwoom_mock_us_*` namespace와 별도 KIS mock broker mutation이 없는
KR Kiwoom 전용 표면이다. 원래는 KR·US 두 namespace를 게이트 없이 함께 등록하던 전체
Kiwoom 프로필을 좁힌 것이었고, 그 전체 프로필은 hk 6456(2026-11-25)에 은퇴했다
(같은 이름의 `MCP_PROFILE`은 미지 프로필로 기동 거부된다). DEFAULT에서는 KR·US
도구가 각각 `KIWOOM_MOCK_ENABLED`·`KIWOOM_MOCK_US_ENABLED` 게이트 뒤에 있다.

| | `kiwoom_kr` |
|---|---|
| 공용 read-only research/account 표면 | O |
| KR `kiwoom_mock_*` (8) | O (`kiwoom_mock_get_order_detail` 포함) |
| US `kiwoom_mock_us_*` (7, mutation 4) | **없음** |
| KIS mock mirror mutation (`kis_mock_mirror_execute_report`) | **없음** |
| network transport `MCP_AUTH_TOKEN` 필수 | O |
| 기동 시 mock config·mock host 강제 | **O** (아래) |

`kiwoom_kr` 등록 경로는 `app/mcp_server/tooling/kiwoom_kr_registration.py`이며
US registrar를 **호출하지 않는다**. US exported name set은 negative contract
evidence로만 import한다. 전체 shared profile registration을 독립된 closed-world
exact set 프록시로 감싸며, KR registrar 자체도 8-name exact set으로 다시 감싼다.
따라서 중앙 mutation 목록에도 없는 새 foreign alias가 shared/KR registrar 어디에
추가되더라도 등록 시점에 drop된다.

🔴 **주문 경로는 불변이다.** 이 작업은 "무엇을 등록할지"만 바꾼다. `dmst_stex_tp="KRX"`
고정, `MOCK_REJECTED_EXCHANGES={NXT, SOR}`, `dry_run`/`confirm` 이중 게이트, place/
cancel/modify 본문은 모두 기존 `orders_kiwoom_variants` / `app/services/brokers/kiwoom/`
코드를 그대로 재사용한다(해당 파일 diff 0줄).

## 기동 (per-session, ROB-1173 strict stdio)

```bash
scripts/mock_session_mcp.py run \
  --profile kiwoom_kr \
  --session-id "<bounded-session-id>" \
  --client claude \
  -- claude --model opus --dangerously-skip-permissions
```

- wrapper는 session별 mode-0600 JSON을 만들고 stdio server 하나만 넣는다. Claude
  executable 바로 뒤에 `--mcp-config <그 JSON>`과 `--strict-mcp-config`를
  강제로 넣는다. profile-filtered env는 `KIWOOM_MOCK_*`만 유지하고 다른 broker
  credential scope를 제거한다. 정상/비정상 종료 모두 stdio child를 bounded
  TERM→KILL→reap한 뒤 JSON을 지운다. spawn 전 parent signal block, child exec 전
  원래 mask 복원, assignment 후 parent unblock 계약과 실제 tool-list 검증은
  `docs/runbooks/mock-session-mcp.md`를 따른다.
- **network transport(`streamable-http`/`sse`)에서 토큰이 비면 FastMCP 생성 전에
  기동 실패한다.** ROB-1173 mock session은 network transport나 TCP 8771을 쓰지 않는다.
  로컬 `MCP_TYPE=stdio` 경로만 tokenless 허용한다.
- `KIWOOM_MOCK_ENABLED=true`인 경우 기동 시점에 mock 자격증명 전부 + base URL이
  정확히 `https://mockapi.kiwoom.com`이어야 한다. 아니면 **누락 키 이름을 명시하며
  기동 거부**한다.
- `KIWOOM_MOCK_ENABLED=false`면 도구는 등록되지만 호출 시점에 fail-closed다.

과거 mock spawn은 `MCP_PROFILE=kiwoom` env만 pane에 넘겨 전역 full MCP config가
계속 선택될 수 있었다. 이 env를 profile 적용 증거로 사용하지 않는다. 실제 Claude argv,
생성 JSON, connected `tools/list`를 모두 확인해야 한다. profile-isolated adapter가 없는
Codex/Kiro mock lane은 full/default로 fallback하지 않고 spawn 전에 실패한다.

## 🔴 배포 체인 판정

상주 배포 유닛은 없다. 8772 고정 유닛은 ncp와 함께 사라졌고 hk 6456에서 은퇴했다.
Mac native 배포에도 MCP profile 포트·plist·watchdog를 두지 않으며, 필요해지면 새
리뷰로 추가한다.

per-session `stdio` 기동은 위의 profile-isolated adapter 계약을 계속 따른다.

## 회귀 테스트

```bash
uv run pytest tests/test_mcp_kiwoom_kr_profile.py tests/test_mcp_profiles.py \
  tests/test_mcp_server_main.py -q
```

- `tests/test_mcp_kiwoom_kr_profile.py` — 이름 집합(KR 8개 / 제외되는 US mutation
  4개), 전체 기본 inventory 118개 exact, optional gate별 명시 확장,
  **closed-world 필터가 load-bearing임**(중앙 상수 밖
  `kis_mock_shadow_place_order`도 shared registrar에서 드롭), KR registrar가 KR만
  등록, 주문 경로 불변(KRX 고정, place body).
- `tests/test_mcp_profiles.py::TestKiwoomKrProfile` — profile 등록 결과에
  전체 active exact set과의 동등성, US namespace와 KIS mock mirror mutation 부재,
  중앙 broker mutation 계약상 KR
  3개(place/modify/cancel) 외 direct mutation 0, kt00007 read 유지.
- `tests/test_mcp_profiles.py::TestOrderSurfaceMatrix` — `_ORDER_SURFACE_MATRIX`에
  `kiwoom_kr → KIWOOM_MOCK_TOOL_NAMES` 집합 **동등성**으로 고정(추가·삭제 양방향 탐지).
- `tests/test_mcp_server_main.py` — network transport 토큰 필수, mock config 불완전/
  live host 시 기동 거부.
