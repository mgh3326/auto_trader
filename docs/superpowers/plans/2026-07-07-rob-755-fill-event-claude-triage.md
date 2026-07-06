# Fill Event Claude Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 체결(fill) 이벤트, 특히 매도 체결 후 생긴 현금 재배치 판단을 `claude -p` read-only 트리아지로 자동 기동해 human-in-loop dry-run 제안을 남긴다.

**Architecture:** ROB-602의 watch-alert 자동 기동 패턴을 재사용하되, 이벤트 소스는 `review.execution_ledger`의 durable fill row로 둔다. Poller는 `execution_ledger.id` 워터마크로 신규 websocket fill row를 읽고, `/fill-event-triage` 슬래시 커맨드를 read-only 설정으로 호출한다. 레포 변경은 조회 표면, CLI, MCP read tool, 슬래시 커맨드, runbook/test에 한정한다.

**Tech Stack:** Python 3.13 / uv / SQLAlchemy async / PostgreSQL / FastMCP / pytest / Claude Code CLI(`claude -p`) / bash + jq.

## Global Constraints

- 자동 트리아지 경로는 **read-only 분석 + dry-run 제안**까지만 한다. 주문 place/modify/cancel/reconcile, watch/report mutation, settings/manual holdings mutation 호출 금지.
- 기존 `.claude/settings.readonly.json`을 사용한다. 새 mutation 도구가 추가되면 deny-list와 `tests/test_watch_triage_readonly_settings.py`를 함께 갱신한다.
- 신규 DB 테이블/마이그레이션은 만들지 않는다. `review.execution_ledger.id` primary key 워터마크와 기존 websocket upsert 경로를 재사용한다.
- Poller 기본 소스는 `source='websocket'`이다. Reconciler/manual_import 과거 행은 기본 자동 기동 대상이 아니다.
- 워터마크는 timestamp가 아니라 `execution_ledger.id`다. 늦게 insert된 fill row도 신규 id로 감지하고, 동일 row update는 중복 기동하지 않는다.
- DB 접근은 repository 경유를 기본으로 한다. CLI/MCP handler에서 ad hoc SQL을 쓰지 않는다.
- 체결 payload에는 raw broker payload를 노출하지 않는다. Poller JSON은 sanitized summary 필드만 출력한다.
- 매도 fill은 최우선: 현금/주문가능금액/현재 보유/기존 후보를 다시 읽고 재배치 제안을 낸다. 매수 fill은 남은 rung/중복 주문 조정 판단까지만 한다.

---

## File Structure

| 파일 | 책임 |
|---|---|
| `app/services/execution_ledger/repository.py` | poller/MCP가 쓰는 recent fill read method 추가 |
| `tests/services/execution_ledger/test_repository.py` | repository 필터/정렬/limit 테스트 추가 |
| `scripts/list_recent_fill_events.py` | operator-host poller용 JSON stdout CLI |
| `tests/scripts/test_list_recent_fill_events_cli.py` | CLI JSON/arg/error 테스트 |
| `app/mcp_server/tooling/execution_ledger_events.py` | read-only MCP tool implementation/registration |
| `app/mcp_server/tooling/registry.py` | MCP registry에 fill event read tool 등록 |
| `tests/mcp_server/test_execution_ledger_fill_events_tool.py` | MCP tool 테스트 |
| `.claude/commands/fill-event-triage.md` | fill-event 트리아지 슬래시 커맨드 |
| `tests/test_fill_event_triage_command.py` | 커맨드 bootstrap/safety 구조 가드 |
| `docs/runbooks/fill-event-claude-triage.md` | operator-host poller/runbook |
| `app/mcp_server/README.md` | 새 read-only MCP tool 목록/설명 동기화 |

---

### Task 1: Execution Ledger Recent Fill Read Method

**Files:**
- Modify: `app/services/execution_ledger/repository.py`
- Modify: `tests/services/execution_ledger/test_repository.py`

**Interfaces:**
- Produces:
  ```python
  async def list_recent_fills_for_triage(
      self,
      *,
      after_id: int | None = None,
      market: str | None = None,
      side: str | None = None,
      source: str | None = "websocket",
      broker: str | None = None,
      account_mode: str | None = None,
      limit: int = 50,
  ) -> list[ExecutionLedger]
  ```

- [ ] Add failing tests that seed `ExecutionLedger` rows with unique `broker_order_id` values prefixed `ROB755-`, then assert:
  - `after_id` excludes older rows.
  - default `source='websocket'` excludes reconciler/manual_import rows.
  - `market='crypto'` maps through `ExecutionLedgerRepository.apply_market_filter`.
  - `side='sell'`, `broker`, and `account_mode` filters work.
  - result order is `id ASC`, limit clamps to `1..500`.
- [ ] Add `finally` cleanup in the DB-backed test:
  ```python
  await db_session.execute(
      delete(ExecutionLedger).where(
          ExecutionLedger.broker_order_id.like("ROB755-%")
      )
  )
  await db_session.commit()
  ```
- [ ] Implement the method using `select(ExecutionLedger).where(ExecutionLedger.id > after_id).order_by(ExecutionLedger.id.asc())`.
- [ ] Run:
  ```bash
  uv run pytest tests/services/execution_ledger/test_repository.py -v
  ```

### Task 2: Poller CLI `scripts/list_recent_fill_events.py`

**Files:**
- Create: `scripts/list_recent_fill_events.py`
- Create: `tests/scripts/test_list_recent_fill_events_cli.py`

**Interfaces:**
- Produces `collect(...) -> dict` with this JSON shape:
  ```json
  {
    "success": true,
    "count": 1,
    "fills": [
      {
        "ledger_id": 123,
        "event_key": "execution_ledger:123",
        "broker": "upbit",
        "account_mode": "live",
        "venue": "upbit_krw",
        "instrument_type": "crypto",
        "market": "crypto",
        "symbol": "BTC",
        "raw_symbol": "KRW-BTC",
        "side": "sell",
        "filled_qty": "0.01",
        "filled_price": "100000000",
        "filled_notional": "1000000",
        "currency": "KRW",
        "broker_order_id": "uuid",
        "fill_seq": 0,
        "correlation_id": "uuid",
        "source": "websocket",
        "filled_at": "2026-07-07T00:00:00+00:00",
        "created_at": "2026-07-07T00:00:01+00:00"
      }
    ]
  }
  ```

- [ ] Add CLI args: `--after-id`, `--market`, `--side`, `--source` (`websocket|reconciler|manual_import|all`, default `websocket`), `--broker`, `--account-mode`, `--limit`.
- [ ] Derive `market` from `instrument_type`: `equity_kr -> kr`, `equity_us -> us`, `crypto -> crypto`.
- [ ] Never include `raw_payload_json` in CLI output.
- [ ] Unit-test `collect()` with monkeypatched `AsyncSessionLocal` and fake repository rows so the CLI test does not require live DB cleanup.
- [ ] Unit-test bad args/error path: invalid `--source` returns non-zero JSON `{"success": false, "error": ...}`.
- [ ] Run:
  ```bash
  uv run pytest tests/scripts/test_list_recent_fill_events_cli.py -v
  ```

### Task 3: MCP Read Tool

**Files:**
- Create: `app/mcp_server/tooling/execution_ledger_events.py`
- Modify: `app/mcp_server/tooling/registry.py`
- Create: `tests/mcp_server/test_execution_ledger_fill_events_tool.py`
- Modify: `app/mcp_server/README.md`

**Interfaces:**
- Produces:
  ```python
  async def execution_ledger_fill_events_list_recent_impl(
      after_id: int | None = None,
      market: str | None = None,
      side: str | None = None,
      source: str | None = "websocket",
      broker: str | None = None,
      account_mode: str | None = None,
      limit: int = 50,
  ) -> dict[str, Any]
  ```
- Registers MCP tool name: `execution_ledger_fill_events_list_recent`.

- [ ] Implement the MCP handler as a thin wrapper over `ExecutionLedgerRepository.list_recent_fills_for_triage`.
- [ ] Return the same sanitized fill schema as the CLI.
- [ ] Reject invalid `source` with `{"success": False, "error": "invalid_source"}`.
- [ ] Register the tool in the always-read-only section of `register_all_tools`; it must be available to normal and read-heavy profiles without adding mutation surface.
- [ ] Update `app/mcp_server/README.md` where read-only investment/execution tools are documented.
- [ ] Add MCP tests for successful JSON output and invalid source.
- [ ] Run:
  ```bash
  uv run pytest tests/mcp_server/test_execution_ledger_fill_events_tool.py -v
  ```

### Task 4: Fill Event Slash Command

**Files:**
- Create: `.claude/commands/fill-event-triage.md`
- Create: `tests/test_fill_event_triage_command.py`

**Command contract:**
- Input is `$ARGUMENTS` key/value text from the poller:
  `ledger_id=... event_key=... broker=... account_mode=... market=... symbol=... side=... filled_qty=... filled_price=... filled_notional=... currency=... filled_at=... correlation_id=...`
- Required bootstrap:
  1. `get_operating_briefing(market=<market>)`
  2. `get_cash_balance(...)` for live orderable/cash context, especially sell fills
  3. `get_portfolio_allocation(include_cash=true)` if available in the active MCP profile
  4. `session_context_get_recent(market=<market>, limit=10)`
  5. `session_context_append(...)` with refs `{event_key, ledger_id, correlation_id, symbols}`

- [ ] Write the command so sell fill output answers:
  - What cash/currency was freed?
  - Is the current portfolio under/over target after the sale?
  - Which existing candidate/report/watch context should be reconsidered?
  - What dry-run buy/redeploy proposal should the operator review?
- [ ] Write the command so buy fill output answers:
  - Was the intended tranche filled?
  - Are remaining rung/open order assumptions still valid?
  - Should the operator pause, tighten, or leave remaining orders alone?
- [ ] Explicitly state that place/modify/cancel/reconcile/report/watch mutation tools are forbidden.
- [ ] Test the command file contains `$ARGUMENTS`, `get_operating_briefing`, `get_cash_balance`, `session_context_get_recent`, `session_context_append`, `sell`, `redeploy`, and does not instruct direct order mutation.
- [ ] Run:
  ```bash
  uv run pytest tests/test_fill_event_triage_command.py tests/test_watch_triage_readonly_settings.py -v
  ```

### Task 5: Operator Runbook and Poller Script

**Files:**
- Create: `docs/runbooks/fill-event-claude-triage.md`
- Modify: `docs/runbooks/watch-alert-claude-triage.md` with a short cross-link only.

**Runbook content:**
- [ ] Document environment:
  ```bash
  export AUTO_TRADER_REPO="$HOME/work/auto_trader"
  export FILL_TRIAGE_MARKET="crypto"
  export DISCORD_FILL_TRIAGE_WEBHOOK="https://discord.com/api/webhooks/..."
  ```
- [ ] Provide a repo-outside poller script that stores state under `~/.local/state/fill-event-triage/last_ledger_id`.
- [ ] Poll with:
  ```bash
  uv run python -m scripts.list_recent_fill_events \
    --market "$FILL_TRIAGE_MARKET" \
    --source websocket \
    ${last_id:+--after-id "$last_id"} \
    --limit 50
  ```
- [ ] For each fill, call:
  ```bash
  claude -p "/fill-event-triage $payload" \
    --permission-mode bypassPermissions \
    --settings "$REPO/.claude/settings.readonly.json" \
    --output-format json
  ```
- [ ] Record validation JSONL with `ledger_id`, `session_id`, `cost_usd`, `duration_ms`, `num_turns`.
- [ ] Advance `last_ledger_id` only after Claude and Discord post both succeed. This preserves at-least-once behavior.
- [ ] Include dry-run mode that prints the `claude -p` command without invoking Claude or Discord.

### Task 6: End-to-End Verification

**Files:**
- No new files unless prior tasks reveal a missing targeted test.

- [ ] Run all ROB-755 targeted tests:
  ```bash
  uv run pytest \
    tests/services/execution_ledger/test_repository.py \
    tests/scripts/test_list_recent_fill_events_cli.py \
    tests/mcp_server/test_execution_ledger_fill_events_tool.py \
    tests/test_fill_event_triage_command.py \
    tests/test_watch_triage_readonly_settings.py \
    -v
  ```
- [ ] Run websocket regression tests because this feature depends on fill ledger insert semantics:
  ```bash
  uv run pytest tests/test_websocket_monitor.py -v
  ```
- [ ] Run lint:
  ```bash
  make lint
  ```
- [ ] Manual smoke with existing DB:
  ```bash
  uv run python -m scripts.list_recent_fill_events --source websocket --limit 5
  ```
- [ ] If the CLI returns at least one row, run the runbook poller in `DRY_RUN=1` and verify it prints `/fill-event-triage ...` with the expected key/value payload.

## Acceptance Criteria

- A new websocket fill row in `review.execution_ledger` is discoverable by CLI and MCP using `after_id`.
- Sell fills can wake a read-only `claude -p` triage path that proposes cash redeployment without executing orders.
- Buy fills can wake the same path for remaining rung/open-order review.
- Duplicate websocket events that upsert the same ledger row do not generate a second poller event.
- No raw broker payload or secret-bearing field appears in poller JSON.
- Existing watch-alert triage tests and read-only deny-list tests still pass.
