# H5 guard deviation record — #1272 non-USDT balances under verified single-asset margin

Filed with the code PR for hk task 1272. The authority is the operator decision
on hk task 1271, option B (operator-desk transition, 2026-10-08 17:54 KST):
the H5 truth gate may admit non-USDT balances only when the same read-only call
proves multiAssetsMargin=false; with multi-asset margin on, or a mode that
cannot be read, any non-USDT balance still fails. This record changes no H5
policy key of the operator contract entry
h5_ls_env_v1_futures_demo_orders_20260928 (margin_mode isolated, leverage 1,
universe, sizing, exits, scoring), grants no order permission and does not
start the runner.

```yaml
change_id: task1272-h5-single-asset-margin-foreign-balances-20261008
lane: H5-LS-ENV-v1
contract_entry: h5_ls_env_v1_futures_demo_orders_20260928
contract_doc: auto_trader-operator mock/contracts/h5-ls-env-v1.md
operator_decision_ref: >-
  hk task 1271 option B (operator-desk, 2026-10-08): allow non-USDT balances
  only when multiAssetsMargin=false is verified by the same read-only call;
  multi-asset margin on or unverifiable with any non-USDT balance still FAILs.
implementation_ref: hk task 1272
pr_number: TBD_ON_PR
merge_sha: TBD_ON_MERGE
change_class:
  - h5_account_truth_guard_change
account_scope: >-
  Binance USD-M Futures Demo account behind demo-fapi.binance.com only.
  No live, Spot, testnet or other account path.
affected_policy_keys: []
consumer_paths:
  - app/services/brokers/binance/h5/client.py  # H5DemoClient.read_account
  - app/services/brokers/binance/h5/truth_gate.py  # account_isolated_1x
read_path: >-
  Unchanged. One signed GET /fapi/v2/account per read_account call. The mode
  and the balances come from the same response. No new endpoint, no write,
  no signed mutation.
effective_at: >-
  Only after the code PR merges and an operator deploys an image containing
  it. Merge alone starts nothing.
rollback: >-
  Revert the code PR. Without non-USDT balances behaviour is identical either
  way; with the demo grants present the gate returns to FAIL.
```

## Why the demo grants exist

The Futures Demo account is created with USDC 5000 and BTC 0.01. The demo app
has no Transfer, and Reset re-grants them (hk task 1271; the operator found no
Transfer on the web demo either). Before this change the truth gate check
account_isolated_1x failed on any non-USDT marginBalance different from 0, so
the gate could never pass on this account.

## Changed rule and exact scope

Purpose of the original rule: a non-USDT asset must never act as margin for H5.
In single-asset margin mode (multiAssetsMargin=false) a USD-M position in a
USDT symbol is margined by USDT alone, so the USDC and BTC grants cannot margin
BTCUSDT, ETHUSDT or SOLUSDT.

- Before: any non-USDT asset with a marginBalance different from zero raised
  foreign account asset exposure, whatever the margin mode.
- After: read_account still refuses first unless multiAssetsMargin is exactly
  the JSON boolean false (missing, null, true, the string "false" and numbers
  all refuse, as before). Only then may a non-USDT asset hold a positive,
  finite balance. Its asset name must be 1 to 20 characters of A-Z and 0-9.
  A negative, non-finite or unreadable balance, or an unreadable name, still
  refuses.
- NAV: with any non-USDT balance present, totalMarginBalance (H5 NAV) must
  equal the USDT asset's marginBalance in the same response, else refuse. In
  single-asset mode Binance reports totalMarginBalance for USDT only; the
  check proves the grants did not enter NAV sizing or the drawdown baseline.
- The gate re-checks the mode itself: a reported non-USDT asset with a mode
  other than exactly False fails account_isolated_1x.
- Output: a passing account_isolated_1x detail appends
  margin_mode=single_asset non_usdt_assets=<sorted names>. Without non-USDT
  balances the detail is unchanged (nav_usdt=<NAV> symbols=all).
- The runner reads the account through the same read_account, so the same
  rule applies to its NAV reads; otherwise a passing gate could not be used.
- Unchanged: the other five gate checks, their order, the CLI flags and exit
  codes (0 PASS, 2 FAIL), the per-symbol isolated 1x BOTH requirement,
  one-way mode and every order path.

## Evidence

tests/services/brokers/binance/h5/test_single_asset_margin_gate.py drives the
real client over a mock transport and the real gate: grants pass only with
multiAssetsMargin exactly false; true, missing, null, string, numeric and HTTP
error cases fail; no-grant accounts give the unchanged detail; the run issues
only the same signed GETs; read_account reaches the broker only through one
signed GET of /fapi/v2/account (AST).
tests/services/brokers/binance/h5/test_single_asset_margin_mutants.py counts
every branch of read_account and the gate's account check from disk and kills
each one with its invariant sentence.

## Residual risk

The NAV equality relies on the demo account reporting USDT-only totals in
single-asset mode as documented; if it does not, the guard fails closed and the
runner stays blocked until reviewed. Switching the account to multi-asset mode
later makes the gate and every runner account read fail again.
