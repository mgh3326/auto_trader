# Mock session-owned MCP

ROB-1173. Mock session마다 하나의 profile-specific stdio MCP child를 소유하고,
Claude가 종료되면 child와 임시 config도 함께 종료·삭제하는 실행 계약이다.

## 안전 경계

- 허용 profile은 `hermes-paper-kis`, `kiwoom_kr`, `us-paper`뿐이다.
- `default`, 과거의 full `kiwoom`, 빈 값, unknown profile은 spawn 전에 거부한다.
- Claude만 profile-isolated adapter가 있다. Codex/Kiro mock lane은 adapter가 생기기
  전까지 spawn 전에 fail-close한다. full/default MCP fallback은 없다.
- 생성 JSON에는 stdio server가 정확히 하나뿐이고 credential 값이나 credential env
  mapping을 넣지 않는다. child는 session process의 기존 환경을 상속한다.
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

wrapper가 실제 Claude argv 끝에 아래를 강제한다.

```text
--mcp-config <per-session-mode-0600-json> --strict-mcp-config
```

호출자가 두 플래그를 직접 넘기면 충돌을 허용하지 않고 실패한다. config는 wrapper가
살아 있는 동안만 존재하며 Claude 종료 뒤 삭제된다.

현재 운영 `/Users/mgh3326/bin/herdr-spawn`은 Git 밖의 unversioned 파일이다.
이 저장소 PR은 위 seam을 제공하지만 그 전역 파일을 수정하지 않는다. 운영 wrapper가
mock+Claude 분기에서 이 repo-owned command를 사용하도록 전환되기 전에는 기존
`herdr-spawn` 경로가 ROB-1173 배선을 사용한다고 주장할 수 없다.

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

테스트:

```bash
uv run pytest tests/scripts/test_mock_session_mcp.py -vv -s
```

이 테스트는 고정 seed를 사용하지 않는다. 난수 기반 판단이 없고 session id도 상수다.
두 profile의 stdio child를 동시에 연결해 namespace 교차오염이 없는지 확인하고, 종료
뒤 session id를 가진 프로세스와 listener가 남지 않는지 bounded poll로 확인한다.
