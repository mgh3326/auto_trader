# Mock session-owned MCP

ROB-1173. Mock session마다 하나의 profile-specific stdio MCP child를 소유하고,
Claude가 종료되면 child와 임시 config도 함께 종료·삭제하는 실행 계약이다.

## 안전 경계

- 허용 profile은 `hermes-paper-kis`, `kiwoom_kr`, `us-paper`뿐이다.
- `default`, 과거의 full `kiwoom`, 빈 값, unknown profile은 spawn 전에 거부한다.
- Claude만 profile-isolated adapter가 있다. Codex/Kiro mock lane은 adapter가 생기기
  전까지 spawn 전에 fail-close한다. full/default MCP fallback은 없다.
- 생성 JSON에는 stdio server가 정확히 하나뿐이고 credential 값이나 credential env
  mapping을 넣지 않는다.
- wrapper는 parent env와 `ENV_FILE`을 읽은 뒤 profile별 broker scope만 남긴 환경을
  Claude/stdio child에 전달한다. `kiwoom_kr`은 `KIWOOM_MOCK_*`만 허용하며
  `KIWOOM_MOCK_US_*`, KIS, Toss, Alpaca, Binance, Upbit broker env는 상속하지 않는다.
  필터 뒤 child의 `ENV_FILE=/dev/null`로 고정해 repo-wide `.env` 재로딩을 막는다.
- TCP 8771이나 다른 상주 listener를 만들거나 재사용하지 않는다.
- 이 배선은 도구를 **연결하고 목록을 읽을 뿐**, broker 도구를 자동 호출하지 않는다.

과거 `MOCK_MCP_PROFILE=kiwoom`을 env로만 넘긴 mock spawn은 client가 전역
`.mcp.json`의 full HTTP server를 계속 사용할 수 있었다. env 존재만으로 MCP profile
적용을 추론하면 안 된다. `kiwoom` profile 자체도 KR 8개와 US 7개를 함께 등록하므로
KR-only 세션에 사용하지 않는다.

## Repo-owned launcher seam

herdr의 Claude agent command 자리에 다음 wrapper를 둔다.

```bash
scripts/mock_session_mcp.py run \
  --profile kiwoom_kr \
  --session-id "<bounded-session-id>" \
  --client claude \
  -- claude --model opus --dangerously-skip-permissions
```

wrapper가 실제 Claude executable 바로 뒤(모든 subcommand보다 앞)에 아래를 강제한다.

```text
--mcp-config <per-session-mode-0600-json> --strict-mcp-config
```

호출자가 `--mcp-config value`, `--mcp-config=value`,
`--strict-mcp-config`, `--strict-mcp-config=value`,
`--no-strict-mcp-config` 형식을 직접 넘기면 충돌을 허용하지 않고 spawn 전에
실패한다. config는 wrapper가 살아 있는 동안만 존재한다.
정상 종료뿐 아니라 SIGINT/SIGTERM/SIGHUP, post-`Popen` exception, TERM 무시
child에서도 bounded TERM→KILL→reap을 끝낸 뒤 삭제한다.

현재 운영 `/Users/mgh3326/bin/herdr-spawn`은 Git 밖의 unversioned 파일이다.
실측 live script의 mock 분기는 `:27`에서 `MCP_PROFILE` env만 넘기며 이 seam을
호출하지 않는다. 이 저장소, operator 저장소, herdr templates에는 그 live script의
versioned source가 없다. 이 PR은 위 seam을 제공하지만 전역 파일을 수정하지 않는다.
운영자가 unversioned launcher를 변경하거나 먼저 version control로 편입해
mock+Claude 분기에서 이 repo-owned command를 호출하기 전에는 auto_trader PR만으로
ROB-1173 actual wiring AC를 완료했다고 주장할 수 없다.

## Claude 2.1.220 actual strict-single-server blocker

`server_count=1`은 generated JSON의 shape일 뿐, Claude가 실제로 연결한 server가
1개라는 뜻이 아니다. Claude Code 2.1.220의 공식 local help는
`--strict-mcp-config`를 “다른 MCP config 무시”로 설명하지만, 이 wrapper를 통한
read-only `claude mcp list` 관리 subcommand는 owned server 대신 기존
`claude.ai`/plugin/user MCP를 health-check했다.

전역 설정을 바꾸지 않고 local help의 source-isolation 후보를 확인한 결과:

- `--safe-mode`: ambient MCP와 함께 explicit owned MCP도 꺼져
  `No MCP servers configured`가 된다.
- `--setting-sources ''`: user/project/local source는 줄지만 `claude.ai`
  connectors가 남는다.
- `--bare`: 관리 subcommand에서 plugin/user MCP가 남았고, help 계약상
  OAuth/keychain 인증을 읽지 않아 현재 운영 인증을 깨뜨릴 수 있다.

따라서 이 PR은 세 옵션을 wrapper에 강제하지 않는다. direct stdio
`initialize`/`tools/list`는 owned child inventory를 검증하지만 actual Claude
strict-single-server 증거를 대체하지 않는다. 필수 actual AC를 닫으려면 versioned
launcher 배선과 함께, 운영 인증을 보존하면서 ambient settings/plugin/managed
connectors를 배제할 Claude-supported session isolation 또는 동등한 별도 adapter가
필요하다. 그 전까지 actual strict-single-server는 **blocked / 성공 주장 금지**다.

## 실제 connected tool list 확인

broker tool은 하나도 호출하지 않고 MCP `initialize` + `tools/list`만 수행한다.

```bash
uv run python scripts/mock_session_mcp.py verify \
  --profile kiwoom_kr \
  --session-id "verify-kiwoom-kr"
```

출력 전문에서 다음을 확인한다.

1. `kiwoom_mock_*` namespace가 아래 정확히 8개다.
   `preview_order`, `place_order`, `cancel_order`, `modify_order`,
   `get_order_history`, `get_order_detail`, `get_positions`,
   `get_orderable_cash`.
2. `kiwoom_mock_us_*`, `kis_live_*`, `toss_*`가 없다.
3. generic order `place_order`, `cancel_order`, `modify_order`,
   `get_order_history`, `live_reconcile_orders`가 없다.
4. 중앙 `DIRECT_BROKER_MUTATION_TOOLS` 계약과 교차했을 때 위 KR namespace에 속한
   mutation 외에는 0개다. 특히 `kis_mock_mirror_execute_report`가 없다.

테스트:

```bash
uv run pytest tests/scripts/test_mock_session_mcp.py -vv -s
```

이 테스트는 고정 seed를 사용하지 않는다. 난수 기반 판단이 없고 session id도 상수다.
두 profile의 stdio child를 동시에 연결해 namespace 교차오염이 없는지 확인한다.
SIGHUP, post-`Popen` exception, TERM 무시 child fault에서 process/listener/config가
모두 0이 되는지와, 실제 stdio child 종료 후 PID/PPID/comm census에 orphan이 없는지를
확인한다.
