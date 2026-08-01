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

## 🔴 배포 체인 판정 — `MCP_PROFILE_PORTS`에 넣지 않는다

**판정: per-session 기동(위 명령 + 클라이언트 url 등록). 배포 상주 서비스로 만들지
않는다.** 근거는 추론이 아니라 코드다.

1. **`MCP_PROFILE_PORTS`는 서비스를 만들지 않는다.** `scripts/deploy-native.sh:75-79`의
   `label:port` 배열은 `verify_mcp_profile_release_paths()`
   (`scripts/deploy-native.sh:211-249`, `:466`에서 호출)에서만 소비된다. 그 함수는
   `lsof`로 해당 포트의 LISTEN 프로세스를 찾아 cwd가 `$NEW_RELEASE`인지 검증한다
   (ROB-831, 2026-07-11 wedged 프로세스 사고). 엔트리만 추가하면 **아무것도 뜨지 않고
   deploy hard gate만 하나 늘어난다** — 리스너가 없으면 rc=1 → 롤백.
2. **실제로 상주시키려면 3곳을 동시에 건드려야 한다.** launchd plist
   (`ops/native/plists/com.robinco.auto-trader.mcp-*.plist`: `KeepAlive=true`,
   `RunAtLoad=false`, 고정 포트, 전용 토큰 env) + `SINGLE_ACTIVE_LABELS`
   (`scripts/deploy-native.sh:51-64`) + `$BASE/shared/.env.prod.native`의 토큰
   (`ops/native/scripts/run-mcp-profile.sh:14-19`, 빈 토큰이면 exit 78). 현재
   `kiwoom` 계열은 plist·포트·라벨이 **하나도 없다** — 원래부터 세션 기동 profile이다.
3. **상주화는 권한을 넓히는 방향이다.** `MCP_HOST` 기본값이 `0.0.0.0`
   (`app/mcp_server/main.py`)이고 profile 서비스는 HAProxy(`127.0.0.1:8765`
   단일 바인드) 뒤에 없다. 즉 mock 자격증명을 물고 24/7 LAN에 노출된 리스너가 하나
   늘어난다. ROB-1159는 권한을 **좁히는** 이슈이므로 자기모순이다.
4. **관측 공백.** `ops/native/scripts/healthcheck-native.sh`는 `:8000`과 `:8765`만
   찔러본다. 8768-8770조차 healthcheck 대상이 아니고 `mcp-watchdog`은 blue/green
   color 전용이다. 상주 서비스를 늘리면 감시되지 않는 표면이 늘어난다.
5. **수요 형태가 세션형이다.** KR-B1 P0는 정해진 세션(2026-07-30 08:50)이고 상시
   트래픽이 없다. per-session 기동은 배포 변경 0, 노출 창 = 세션 길이다.
6. AGENTS.md #6(스케줄러/상주 등록은 명시 승인 없이 금지)과 이 이슈의 정지점(PR까지,
   머지·배포 금지)에도 배포 체인 배선은 맞지 않는다.

**나중에 상주가 필요해지면** 필요한 것은 이 목록이다(별도 승인 필요): plist 1개 +
`SINGLE_ACTIVE_LABELS` 엔트리 + `MCP_PROFILE_PORTS` 엔트리 + `.env.prod.native`에
전용 토큰 + `healthcheck-native.sh` 포트 추가. 이 중 하나라도 빠지면 deploy가 깨지거나
감시되지 않는 리스너가 남는다.

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
