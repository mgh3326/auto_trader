# Kiwoom Mock US-Equity Order Smoke

> **MCP profile:** `MCP_PROFILE=kiwoom`은 US namespace를 항상 등록한다.
> default profile에서는 `KIWOOM_MOCK_US_ENABLED=true`일 때만 등록된다.
> CLI smoke 스크립트는 프로파일과 무관하다.

ROB-867. Operator-safe smoke for the Kiwoom **mock-investment** (모의투자)
**US-equity** order lifecycle: submit → order history → cancel → reconcile.

ROB-872 hardened this workflow entirely with fake transports. It did not run a
confirmed smoke or verify any mutation capability; all mutation/order-type
evidence below remains pending or unverified until ROB-873 records a dated
market-hours exercise.

The US namespace is completely independent from the KR `kiwoom_mock` smoke
([`kiwoom-mock-smoke.md`](kiwoom-mock-smoke.md)). It uses a separate app key,
app secret, and account number, and never reads or falls back to
`KIWOOM_MOCK_APP_KEY` / `KIWOOM_MOCK_APP_SECRET` / `KIWOOM_MOCK_ACCOUNT_NO`.

The guiding rule is **evidence-first capability exposure**: the low-level
client may represent the documented Kiwoom request shape, but MCP exposes only
order types proven necessary for current consumers and safe to support now. No
capability is described as supported solely because it appears in Kiwoom
documentation.

## Safety boundaries

This smoke performs **real Kiwoom mock broker mutation**, allowed only inside
these boundaries:

- **Mock host only.** `KiwoomMockUsClient` rejects any base URL other than
  `https://mockapi.kiwoom.com` and re-checks the resolved host before sending
  (transport-layer fail-closed). The live host (`api.kiwoom.com`) is a defensive
  constant that no code path can select.
- **US-only.** NASDAQ (`NASD`), NYSE, and AMEX only. Symbols are resolved from
  `us_symbol_universe` before any network call; missing, inactive, or
  unsupported exchanges fail closed.
- **KRX 미지원 (US 전용).** `kiwoom_mock_us`은 미국주식 전용이며 KR 주문은
  지원하지 않는다. KR은 별도 `kiwoom_mock` namespace를 사용한다.
- **`dry_run=False` requires `confirm=True`** on every order-mutating tool.
- **No live anything.** No KIS live, Kiwoom live, Alpaca live, or real-money
  calls. No scheduler / recurring automation.
- **No secrets printed.** The CLI reports only the **names** of missing env keys,
  never their values. Broker responses are mock-only and contain no credentials.
- **Limit-only full mode.** `full` rejects every `trde_tp` except `00` before
  tool dispatch; an immediately filled market order cannot satisfy cleanup safety.
- **Cancel-before-submit.** `full` mode only submits a real order because cancel
  is wired; it always attempts to cancel any order it opened (finally-block) and
  reconciles. If cancel ever regresses, stop after dry-run.

## Required env (mock only, US namespace)

| Env key | Purpose |
|---|---|
| `KIWOOM_MOCK_US_ENABLED=true` | Master gate (default `false`) |
| `KIWOOM_MOCK_US_APP_KEY` | Mock US app key |
| `KIWOOM_MOCK_US_APP_SECRET` | Mock US app secret |
| `KIWOOM_MOCK_US_ACCOUNT_NO` | Mock US account number |

The base URL remains `KIWOOM_MOCK_BASE_URL`, but construction and every resolved
request continue to require exactly `https://mockapi.kiwoom.com`.

`TESTNET`/live env vars do nothing here. Without these four keys the smoke
fails closed. The US namespace never reads `KIWOOM_MOCK_*` (KR) credentials.

## Supported MCP order types (trde_tp allowlist)

MCP exposes only the two initial consumer-required order types. Their mock
acceptance remains smoke evidence, not a conclusion from documentation. All
other codes are rejected **before symbol lookup, client construction, or
network I/O** with a stable error envelope:

| MCP order type | `trde_tp` | Price rule |
|---|---:|---|
| `limit` | `00` | Positive price required; formatted as a USD decimal string |
| `market` | `03` | Price omitted by the caller and sent as an empty string |

Unsupported-code rejection envelope:

```json
{
  "success": false,
  "error_code": "unsupported_trde_tp",
  "rejected_trde_tp": "<code>",
  "supported_trde_tp": ["00", "03"]
}
```

This allowlist lives in one constant so a later evidence-backed issue can expand
it without changing the public dispatch structure.

### Known unsupported TR: `ust31490` (orderable quantity)

The documented `ust31490` orderable-quantity TR returned `return_code=20` with
`RC9000: 모의투자에서는 해당업무가 제공되지 않습니다.` on the 2026-07-13
operator read-only smoke. Therefore this documented TR is **not** considered
supported until mock evidence exists.

`kiwoom_mock_us_get_orderable_cash` does **not** call `ust31490`. It parses
`ust21160.d0_usd_fx_entr` as a decimal USD deposit when present and returns
`cash_semantics="deposit_not_broker_orderable"` with
`orderable_quantity_supported=false` — it never mislabels deposit cash as a
broker-calculated per-symbol orderable amount.

## CLI

```bash
# 1. Config presence (names only, no values) + all five read-only TRs
uv run python -m scripts.kiwoom_mock_us_smoke --mode preflight

# 2. DB-resolved dry-run preview (no broker mutation)
uv run python -m scripts.kiwoom_mock_us_smoke --mode preview \
    --symbol AAPL --price 150.00 --quantity 1 --trde-tp 00

# 3. Full real mock lifecycle (requires --confirm)
#    submit -> get_order_history(scope=open) -> cancel -> reconcile
uv run python -m scripts.kiwoom_mock_us_smoke --mode full \
    --symbol AAPL --price 150.00 --quantity 1 --trde-tp 00 --confirm

# 4a. Optional: probe documented advanced buy types (double-gated).
#     Probes place REAL mock orders, so they live under a dedicated mode —
#     `--mode preflight` stays strictly read-only and rejects probe flags.
uv run python -m scripts.kiwoom_mock_us_smoke --mode probe \
    --symbol AAPL --quantity 1 --price 1.00 \
    --probe-order-types 26,27,30 --probe-side buy --confirm-probes

# 4b. Sell-only probes require an existing mock position and an extra assertion.
uv run python -m scripts.kiwoom_mock_us_smoke --mode probe \
    --symbol AAPL --quantity 1 --price 150.00 --stop-price 149.00 \
    --probe-order-types 33,34,35 --probe-side sell \
    --confirm-existing-position --confirm-probes
```

Exit codes:
- `0` — smoke OK (or stopped cleanly after dry-run when `--confirm` omitted)
- `2` — anomaly: target evidence is absent/unknown, bounded pagination fails,
  cleanup times out, a fill or position delta appears, or an accepted order is
  untrackable. **Manual cleanup/unwind required** — see the redacted
  `cleanup_required` output and `final_reconciliation` when available, then
  reconcile in the broker UI. Do not retry a place reported as
  `accepted_untracked` or `acceptance_uncertain`.

`full` mode without `--confirm` stops after the dry-run and emits a `stop` step.

## Choosing a non-marketable price

US uses decimal USD prices; there is no KRX-style price-banded tick table in
this workflow. To pick a conservative buy limit well below market that will **remain
pending** long enough to cancel:

1. Reference an existing auto_trader KIS/Yahoo quote out of band.
2. Pick a price safely below the current bid. A price outside the broker's
   accepted band will be rejected (a safe failure — re-pick).
3. Pass it as `--price` (operator-approved override).

Market orders are not used in `full` mode because immediate fill would defeat
the cancel-before-submit safety goal.

## Probe mode (optional, double-gated)

Advanced order-type discovery runs under the dedicated `--mode probe` (it
performs real broker mutations, so it is not reachable from the read-only
`preflight` mode) and stays disabled unless a comma-separated
`--probe-order-types` list and `--confirm-probes` are both supplied. Probe mode
runs the read-only preflight first and aborts if it fails. The
probe:

- Calls the low-level `KiwoomUsOrderClient` directly (NOT the MCP surface) so
  unverified codes can be characterized without weakening the MCP allowlist.
- Records each attempted code and the exact broker result.
- Captures a bounded, paginated positions baseline before each submit.
- Immediately cancels every accepted order with a valid 1-18 digit ID, then
  uses the same bounded cleanup proof as full mode.
- Requires `--probe-side sell --confirm-existing-position` for sell-only types.
- Documented candidates: buy `26,27,30`; sell `33,34,35`. STOP types `34/35`
  also require `--stop-price`.

Probe evidence updates **this runbook**; expanding the MCP allowlist requires a
separate reviewed change.

## Smoke sequence (`full` mode)

1. `kiwoom_mock_us_preview_order` (DB exchange resolution + exact request body).
2. `kiwoom_mock_us_place_order(dry_run=True)`.
3. Capture the paginated `kiwoom_mock_us_get_positions` baseline.
4. `kiwoom_mock_us_place_order(dry_run=False, confirm=True)` → require strict
   broker success and exactly one non-conflicting canonical 1-18 digit order ID
   across documented ID fields. Missing, invalid, or conflicting ID evidence is
   `accepted_untracked`. Leading zeroes are retained.
5. Walk bounded `scope="open"` and `scope="today"` pages and require the exact
   normalized target ID. Repeated tokens, malformed continuation, and page-cap
   exhaustion fail closed.
6. If `--new-price` is supplied, require one unambiguous broker-issued modify
   order ID and retain both the original and replacement IDs as one lifecycle.
   A successful modify with missing, malformed, or conflicting ID evidence is
   reconciliation-required and must not be retried automatically.
7. `kiwoom_mock_us_cancel_order(dry_run=False, confirm=True)` — in a finally-block,
   targeting the latest known lifecycle ID.
8. Poll one bounded open/today-history and positions snapshot per attempt until
   **every** known lifecycle ID is terminal and the baseline position delta is
   zero. `final_reconciliation.order_states` reports the per-ID proof.

The schema-aware classifier reports `open`, `partial`, `filled`,
`cancel_pending`, `cancelled`, `rejected`, or `unknown`. Only a terminal
`cancelled`/`rejected` target with no position delta is clean for this smoke.
Immediate/partial fills, unknown/malformed evidence, position changes, and poll
timeouts all exit 2. Provider exceptions expose only their exception type and
are normalized into the same redacted cleanup evidence. The seven registered
US mock tools share one mock-host-pinned client and OAuth token cache so bounded
pagination and cleanup polling do not request a new token for every page. The
transport also serializes dispatches per `api-id` at least one second apart, as
required by Kiwoom's [mock-account per-TR limit](https://openapi.kiwoom.com/intro?dummyVal=0),
without serializing unrelated TRs. Full mode skips modify after any unsafe
post-place state; probe mode stops before submitting another order type after
its first unsafe baseline or lifecycle outcome.

## Cleanup / verification after smoke

- Re-run with `--mode preflight` is not enough — inspect the final
  `final_reconciliation` state and the paginated open/today history plus
  positions in MCP or the broker UI.
- If any order remains open, record its order number and cancel it (re-run cancel
  via the MCP tool or the broker UI). Do **not** report the smoke as clean while
  an order is open.
- Confirm no live endpoint was contacted (the host allowlist guarantees this; the
  CLI never accepts a non-mock host or non-US exchange).

### Manual cancellation

If `full` mode exits with code 2 (`cleanup_required`), cancel the stranded order:

```bash
# Via MCP tool (MCP_PROFILE=kiwoom session):
#   kiwoom_mock_us_cancel_order(order_id="<1-18 digit order id>", symbol="AAPL",
#                              dry_run=False, confirm=True)

# Then inspect both paginated scope="open" and scope="today" history and
# positions; a cancel return_code alone is not cleanup proof.
```

## Per-TR evidence table

| TR | Path | Purpose | Mock status | Evidence |
|---|---|---|---|---|
| `ust20000` | `/api/us/ordr` | Buy order | Implemented; smoke pending | no acceptance claim yet |
| `ust20001` | `/api/us/ordr` | Sell order | Implemented; smoke pending | no acceptance claim yet |
| `ust20002` | `/api/us/ordr` | Modify order | Implemented; smoke pending | no acceptance claim yet |
| `ust20003` | `/api/us/ordr` | Cancel order | Implemented; smoke pending | no acceptance claim yet |
| `ust21050` | — | Open orders | Proven (2026-07-13 read-only smoke) | return_code=0 |
| `ust21070` | — | Positions | Proven (2026-07-13 read-only smoke) | return_code=0 |
| `ust21510` | — | Today's orders/fills | Proven (2026-07-13 read-only smoke) | return_code=0 |
| `ust21160` | — | USD deposit detail | Proven (2026-07-13 read-only smoke) | return_code=0; `d0_usd_fx_entr` parsed |
| `ust21110` | — | Foreign deposit (raw) | Proven (2026-07-13 read-only smoke) | return_code=0; diagnostics only |
| `ust31490` | — | Orderable quantity | **Unsupported** | `return_code=20`, `RC9000: 모의투자에서는 해당업무가 제공되지 않습니다.` |

> "smoke pending" = the TR path is implemented; the first live mock exercise is
> this smoke. Record the actual `return_code` / `return_msg` here after running.

## Per-order-type evidence table

| `trde_tp` | Type | MCP-exposed | Probe status | Evidence |
|---:|---|---|---|---|
| `00` | Limit | Yes | Unverified | full smoke required |
| `03` | Market | Yes | Unverified | not used in full mode |
| `26` | Documented advanced buy type | No | Unverified | explicit buy probe required |
| `27` | Documented advanced buy type | No | Unverified | explicit buy probe required |
| `30` | Documented advanced buy type | No | Unverified | explicit buy probe required |
| `33` | Documented advanced sell type | No | Unverified | existing-position sell probe required |
| `34` | STOP LIMIT | No | Unverified | existing-position sell probe required |
| `35` | STOP | No | Unverified | existing-position sell probe required |

Omit all secret values. If a probe returns a non-zero `return_code`, record the
`return_msg` as **unsupported evidence** and a follow-up — never fake success.

## Class-share symbol evidence

| Symbol | Class | Status | Evidence |
|---|---|---|---|
| `BRK.B` | Class B (dot format) | **Unverified** | Not exercised until smoke evidence; the DB-standard dot symbol is passed to Kiwoom unchanged initially |

Class-share symbols are marked unverified until the smoke workflow records
broker evidence.

## PR evidence table template

| Step / tool | symbol | exchange | order type | dry_run / confirm | order id | broker status | cleanup |
|---|---|---|---|---|---|---|---|
| preflight | AAPL | NASD | — | — | — | ok / missing keys (names) | — |
| preview | AAPL | NASD | limit | dry | — | success | — |
| place dry | AAPL | NASD | limit | dry | — | success | — |
| place confirmed | AAPL | NASD | limit | confirm | `00001112…` | return_code=0 | — |
| order history (open) | AAPL | NASD | — | — | `00001112…` | pending | — |
| cancel confirmed | AAPL | NASD | — | confirm | `00001112…` | return_code=0 | closed |
| final reconcile | AAPL | NASD | — | — | `00001112…` | target cancelled/rejected; baseline position delta 0 | clean |

Omit all secret values. If `cancel`/account queries return a non-zero
`return_code`, record the `return_msg` as **unsupported evidence** and a
follow-up — never fake success.

## API-contract note

The Kiwoom mock US request body field names and the deposit-cash candidate keys
are mirrored from the Kiwoom REST docs and validated by unit tests with fakes.
The real mock API is first exercised by this smoke. If a field name is wrong,
the tool degrades to an explicit broker-evidence failure (or `cash: null` +
`cash_source: "*_unparsed"`) rather than faking success — capture that as a
follow-up and adjust the field mapping.
