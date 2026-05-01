# Alpaca Paper Read-Only Smoke Check — Operator Runbook

Owner: Ops
Related issues: ROB-71 / ROB-69

Verify that the deployed Alpaca paper MCP tools are reachable and healthy without mutating any broker state.

## Scope and safety boundary

This runbook covers the 7 read-only MCP tools in `ALPACA_PAPER_READONLY_TOOL_NAMES` (defined in `app/mcp_server/tooling/alpaca_paper.py`). It covers account, cash, positions, open orders, single-order lookup when an open order exists, assets, and fills.

Hard safety rules:

- Do **not** submit, place, cancel, replace, modify, or otherwise mutate any order.
- Do **not** use a generic order route.
- Do **not** print or paste Alpaca API keys, secrets, account numbers, or full raw broker payloads.
- Do **not** edit production files under `/Users/mgh3326/services/auto_trader/` while following this smoke runbook.
- Do **not** reintroduce `paper_001` registry/profile/strategy/DB-row modeling.
- `ALPACA_PAPER_BASE_URL` must be unset or exactly `https://paper-api.alpaca.markets` without `/v2`. The service appends `/v2/...` internally.
- Forbidden endpoint (safety note — do not set as target): the live trading endpoint (`https://api.alpaca.markets`) is explicitly rejected by the Alpaca paper endpoint guard before network calls.

## Step 1 — Verify repo/main vs production SHA

Run from the operator checkout:

```bash
cd /Users/mgh3326/work/auto_trader
git fetch origin main production --prune
printf 'repo_HEAD=' && git rev-parse HEAD
printf 'origin_main=' && git rev-parse origin/main
printf 'origin_production=' && git rev-parse origin/production
printf 'native_current=' && git -C /Users/mgh3326/services/auto_trader/current rev-parse HEAD
```

Interpretation:

- If `repo_HEAD != origin_main`, mark **BLOCKED** until the checkout is updated.
- If `native_current != origin_production`, mark **BLOCKED** until the native production release is reconciled.
- If `origin_main != origin_production`, record it. This can be **PASS** only if the operator intentionally smokes the currently deployed production release rather than latest main.

## Step 2 — Verify environment without printing secrets

Check presence and lengths only. Never echo secret values.

```bash
python - <<'PY'
import os
for key in ('ALPACA_PAPER_API_KEY', 'ALPACA_PAPER_API_SECRET'):
    value = os.environ.get(key, '')
    print(f'{key}: present={bool(value)} len={len(value)}')
base = os.environ.get('ALPACA_PAPER_BASE_URL', '<unset, will default>')
print(f'ALPACA_PAPER_BASE_URL: {base}')
PY
```

Expected:

- API key and secret are present with non-zero lengths.
- `ALPACA_PAPER_BASE_URL` is either unset or exactly `https://paper-api.alpaca.markets`.
- `https://paper-api.alpaca.markets/v2` is invalid because the service appends `/v2/...` internally and would produce duplicated `/v2/v2/...` requests.
- Any live/data endpoint configuration is **BLOCKED**.

## Step 3 — Run local guard tests

```bash
uv run pytest tests/test_alpaca_paper_config.py tests/test_alpaca_paper_isolation.py tests/test_mcp_alpaca_paper_tools.py -q
```

Expected: all tests pass before live paper smoke. If they fail, mark **BLOCKED** and do not continue.

## Step 4 — Confirm the 7 read-only tools are present

```bash
uv run python - <<'PY'
from app.mcp_server.tooling.alpaca_paper import ALPACA_PAPER_READONLY_TOOL_NAMES
for name in sorted(ALPACA_PAPER_READONLY_TOOL_NAMES):
    print(name)
print('count=', len(ALPACA_PAPER_READONLY_TOOL_NAMES))
PY
```

Expected read-only names:

- `alpaca_paper_get_account`
- `alpaca_paper_get_cash`
- `alpaca_paper_get_order`
- `alpaca_paper_list_assets`
- `alpaca_paper_list_fills`
- `alpaca_paper_list_orders`
- `alpaca_paper_list_positions`

After ROB-73, `alpaca_paper_submit_order` and `alpaca_paper_cancel_order` may
also be registered as explicit paper-only, confirm-gated tools. This read-only
smoke must not call them; use `docs/runbooks/alpaca-paper-dev-smoke.md` for the
separate dev-owned side-effect smoke.

Mark **BLOCKED** if any registered Alpaca MCP name includes `alpaca_live_`,
`place`, `replace`, `modify`, `cancel_all`, `cancel_orders`, or
`cancel_by_symbol`, or if Alpaca paper appears in the generic order route.

## Step 5 — Run `hermes mcp test auto_trader`

Use the operator's existing Hermes CLI configuration for the deployed MCP server:

```bash
mkdir -p .smoke
hermes mcp test auto_trader 2>&1 | tee ".smoke/alpaca-paper-mcp-test-$(date +%Y%m%d-%H%M%S).log"
```

Expected:

- MCP connection succeeds.
- The 7 Alpaca paper read-only tool names from Step 4 are visible.
- No `alpaca_live_*` tool is visible.
- No Alpaca paper `place`/`replace`/`modify`/bulk-cancel tool is visible.
- If `alpaca_paper_submit_order` and `alpaca_paper_cancel_order` are visible after ROB-73, treat them as allowed explicit paper-only tools, but do not exercise them in this read-only smoke.

If the command prints a credential value, redact it before sharing the log and file a follow-up hardening issue. Do not paste unredacted logs into Linear, Discord, or Paperclip.

## Step 6 — Run the read-only helper script

```bash
uv run python scripts/smoke/alpaca_paper_readonly_smoke.py
```

The helper is argumentless by design. It calls only the 7 read-only handlers, prints counts/status only, and exits non-zero if any required call fails.

Expected successful shape:

```text
  [OK] alpaca_paper_get_account: status=ACTIVE
  [OK] alpaca_paper_get_cash: cash_set=True
  [OK] alpaca_paper_list_positions: count=0
  [OK] alpaca_paper_list_orders: count=0
  [OK] alpaca_paper_get_order: skipped: no orders to inspect
  [OK] alpaca_paper_list_assets: count=8742
  [OK] alpaca_paper_list_fills: count=0
summary: PASS tools_ok=7/7
```

Notes:

- `count=0` is normal for a fresh paper account with no open orders or fills.
- `alpaca_paper_get_order` is skipped and counted as OK when no open orders exist. When open orders are present, the script derives one order id from `list_orders(status="open", limit=1)` and fetches it read-only.
- The script must never print full account, position, order, asset, or fill records.

## Step 7 — Classify result

Use one of these labels in the operator report.

### PASS

All of the following are true:

- Repo/production SHA checks are understood and not blocked.
- Env presence checks pass and base URL is unset or exactly `https://paper-api.alpaca.markets`.
- Local guard tests pass.
- `hermes mcp test auto_trader` connects and lists the expected read-only tools.
- Helper script exits `0` and prints `summary: PASS tools_ok=7/7`.
- No forbidden Alpaca live/generic/place/replace/modify/bulk-cancel tool name or write-path endpoint is observed.

### PARTIAL

Use **PARTIAL** when the safety boundary is intact but a read-only upstream call fails. Examples:

- `hermes mcp test auto_trader` connects, tool inventory is correct, but one Alpaca read-only call returns a rate limit or transient upstream error.
- The helper exits non-zero with one or more `[FAIL]` lines that are not configuration/endpoint guard failures.

Record the failed tool name and exception class, but do not paste raw payloads or secrets.

### BLOCKED

Use **BLOCKED** for any safety or prerequisite failure:

- Checkout or production SHA cannot be verified.
- API key/secret presence check fails.
- Base URL contains `/v2`, points to a live/data endpoint, or raises `AlpacaPaperEndpointError`.
- Local guard tests fail.
- `hermes mcp test auto_trader` cannot connect.
- A forbidden tool name appears (`alpaca_live_*`, `place`, `replace`, `modify`, `cancel_all`, `cancel_orders`, or `cancel_by_symbol`).
- Any generic Alpaca order route appears.
- The script or command output contains an unredacted secret.

## Step 8 — Report template

Paste a short report like this, with no secrets and no raw broker payloads:

```text
ROB-71 Alpaca paper read-only smoke: PASS|PARTIAL|BLOCKED
repo_HEAD: <sha>
origin_main: <sha>
origin_production: <sha>
native_current: <sha>
hermes_mcp_test: PASS|PARTIAL|BLOCKED
helper_summary: summary: PASS tools_ok=7/7
notes: <redacted exception class/tool name only, if any>
safety: read-only helper made no submit/cancel calls; no forbidden Alpaca live/generic/place/replace/modify/bulk-cancel tool; no secrets printed; base URL has no /v2.
```
