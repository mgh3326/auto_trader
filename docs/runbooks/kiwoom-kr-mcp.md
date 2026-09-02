# KR-only Kiwoom MCP profile (`MCP_PROFILE=kiwoom_kr`)

ROB-1159. Least-privilege split of `MCP_PROFILE=kiwoom`.

## 왜 있는가

`MCP_PROFILE=kiwoom`은 **두 개의 Kiwoom mock namespace를 무조건** 등록한다
(`app/mcp_server/tooling/registry.py`, `KIWOOM` 브랜치):

- KR `kiwoom_mock_*` 8개 (`app/mcp_server/tooling/orders_kiwoom_variants.py`)
- US `kiwoom_mock_us_*` 7개 (`app/mcp_server/tooling/orders_kiwoom_us_variants.py`) —
  이 중 **mutation 4개**:
  `kiwoom_mock_us_preview_order` / `kiwoom_mock_us_place_order` /
  `kiwoom_mock_us_modify_order` / `kiwoom_mock_us_cancel_order`

DEFAULT profile에서는 US namespace가 `settings.kiwoom_mock_us_enabled`(ROB-867)
뒤에 있지만 **KIWOOM 브랜치에는 그 게이트가 없다.** 즉 KR 세션(예: KR-B1)이
`kiwoom`을 고르면 필요 없는 US mutation 표면까지 물리적으로 노출된다. 실행 시점
게이트(`dry_run=False` + `confirm=True`, mock config fail-closed)는 그대로 살아
있지만 **profile 등록 자체가 least-privilege 위반**이다.

`kiwoom_kr`은 그 profile에서 US namespace와 별도 KIS mock broker mutation을 뺀
KR Kiwoom 전용 표면이다.

| | `kiwoom` | `kiwoom_kr` |
|---|---|---|
| 공용 read-only research/account 표면 | O | O (동일) |
| KR `kiwoom_mock_*` (8) | O | O (동일, `kiwoom_mock_get_order_detail` 포함) |
| US `kiwoom_mock_us_*` (7, mutation 4) | O | **없음** |
| KIS mock mirror mutation (`kis_mock_mirror_execute_report`) | O | **없음** |
| network transport `MCP_AUTH_TOKEN` 필수 | O | O |
| 기동 시 mock config·mock host 강제 | X | **O** (아래) |

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
  기동 거부**한다(`kiwoom` profile은 이 검사가 없어 첫 도구 호출까지 지연됐다).
- `KIWOOM_MOCK_ENABLED=false`면 도구는 등록되지만 호출 시점에 fail-closed —
  `kiwoom` profile과 동일한 동작이다.

과거 mock spawn은 `MCP_PROFILE=kiwoom` env만 pane에 넘겨 전역 full MCP config가
계속 선택될 수 있었다. 이 env를 profile 적용 증거로 사용하지 않는다. 실제 Claude argv,
생성 JSON, connected `tools/list`를 모두 확인해야 한다. profile-isolated adapter가 없는
Codex/Kiro mock lane은 full/default로 fallback하지 않고 spawn 전에 실패한다.

## 🔴 배포 체인 판정

Mac native 배포에는 MCP profile 포트·plist·watchdog가 없다. NCP MCP fleet가
`kiwoom` fixed profile의 상주 배포와 health supervision을 소유한다. 운영 변경은
[`ncp-mcp.md`](ncp-mcp.md)를 따르며, Mac launchd 표면을 다시 추가하지 않는다.

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
  전체 active exact set과의 동등성, `kiwoom` 표면과의 차집합이 US namespace + KIS
  mock mirror mutation임, 중앙 broker mutation 계약상 KR
  3개(place/modify/cancel) 외 direct mutation 0, kt00007 read 유지.
- `tests/test_mcp_profiles.py::TestOrderSurfaceMatrix` — `_ORDER_SURFACE_MATRIX`에
  `kiwoom_kr → KIWOOM_MOCK_TOOL_NAMES` 집합 **동등성**으로 고정(추가·삭제 양방향 탐지).
- `tests/test_mcp_server_main.py` — network transport 토큰 필수, mock config 불완전/
  live host 시 기동 거부.
