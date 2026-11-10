# Retro Consumer Pinning and Verification Gate (hk 3819)

**Status: live.** The `retro/v1` HTTP provider surface in `auto_trader` is gated against pinned out-of-process consumers to prevent unintentional schema drift, precision violations, or authentication regressions.

---

## 1. Supported Combinations

The authoritative consumer pin definition lives in [contracts/retro/v1/consumers.lock](file:///Users/robin/work/auto_trader.at-3819-retro-consumer-gate/contracts/retro/v1/consumers.lock).

Currently supported consumer combination:

| Consumer | Repository | Pinned Commit | Go Version | Scope |
|---|---|---|---|---|
| **Factortrail** | `github.com/mgh3326/factortrail` | `4e111bfe3346c4052b0e9cae588411e5b809c017` | `1.27` | `internal/providers/autotrader` |

The gate executes the pinned consumer's actual contract assertions against a live, candidate loopback instance of `auto_trader` running the exact candidate provider code.

---

## 2. Gate Architecture and Tooling

- **Consumer lock file**: `contracts/retro/v1/consumers.lock` pins the consumer repository, commit SHA, Go toolchain version, and tested package paths.
- **Verification harness**: `tools/retro-consumer-check/main.go` runs all 8 retro operations (`retro_day`, `retro_candidates`, `retro_proposals`, `retro_orders`, `retro_fills`, `retro_markouts`, `retro_positions`, `retro_funnel`) using the pinned consumer client, validating:
  - Exact envelope structure and pagination semantics.
  - Required field presence and non-zero counts.
  - Precision discipline (financial quantities, prices, amounts encoded as exact decimal strings; integer counts).
  - Unknown/unavailability discipline (`total: null` requires `unknowns` explanation with `reason=count_unavailable`).
  - Privacy discipline (zero exposure of raw account numbers or broker order IDs).
  - Authentication and verb boundaries (HTTP 401 without token, 401 on bad token, 403 on non-GET methods).
- **Execution runner**: `scripts/ci/run_retro_consumer_gate.py` starts a candidate `auto_trader` HTTP server on an ephemeral loopback port and executes the Go harness. Supports regression testing mutants via `--mutant` (`delete_field`, `float_integer`, `bypass_token`).
- **CI enforcement**:
  - `scripts/ci/classify_changes.py` triggers `run_retro_consumer_gate=true` whenever `contracts/retro/**`, `app/routers/retro_v1.py`, `app/services/retro_v1/**`, `app/services/account_value/retro_scoreboard.py`, or `tools/retro-consumer-check/**` change (or on `run_all`).
  - `retro-consumer-gate` job in `.github/workflows/test.yml` runs before `gate-verdict`.
  - `scripts/ci/aggregate_required.py` fails closed if `retro-consumer-gate` is skipped without classifier authorization.

---

## 3. Consumer Pin Update Procedure

When upgrading the pinned consumer version (e.g. updating factortrail to consume new additive retro fields):

### Procedure: Compatible Extension First
The golden sequence is: **Compatible extension in provider -> Consumer update & commit -> Provider pin bump -> Retire old version.**

1. **Verify candidate compatibility**:
   Ensure `auto_trader` candidate provider satisfies both existing and new consumer needs (additive non-breaking changes only).
2. **Commit consumer changes**:
   In the consumer repository (`factortrail`), update the client/schema, ensure consumer unit and integration tests pass, and commit to `main`. Note the commit SHA (e.g. `abcdef12...`).
3. **Update lock file**:
   In `auto_trader`, update `contracts/retro/v1/consumers.lock` with the new commit SHA and timestamp.
4. **Run local gate**:
   ```bash
   uv run python scripts/ci/run_retro_consumer_gate.py
   ```
   Verify all 8 operations and auth boundary checks exit 0 (GREEN).
5. **Run mutant checks**:
   ```bash
   uv run python scripts/ci/run_retro_consumer_gate.py --mutant delete_field
   uv run python scripts/ci/run_retro_consumer_gate.py --mutant float_integer
   uv run python scripts/ci/run_retro_consumer_gate.py --mutant bypass_token
   ```
   Verify each mutant fails (RED) as expected.
6. **Submit PR**:
   Open a PR in `auto_trader` containing the lock bump and any provider enhancements. CI will run `retro-consumer-gate` and aggregate the result into `PR gate`.

---

## 4. Breaking Change Procedure

Breaking changes to `retro/v1` (e.g. removing fields, renaming endpoints, changing decimal format, or altering authentication headers) are strictly prohibited while active consumers depend on them.

If a breaking change is unavoidable:

1. **Do not break v1 in place**:
   Never alter existing `retro/v1` types, endpoints, or response formats in a backwards-incompatible manner while the pinned consumer is on `v1`.
2. **Three-step coordinated rollout**:
   - **Step 1 (Stage v2 alongside v1)**: Implement `/trading/api/retro/v2` in `auto_trader` alongside `/trading/api/retro/v1`. Keep `retro/v1` intact and fully operational.
   - **Step 2 (Migrate consumer to v2)**: Update consumer (`factortrail`) to target `/retro/v2`. Verify consumer against `auto_trader` v2 endpoints. Cut a commit in the consumer repo.
   - **Step 3 (Pin bump & eventual v1 retirement)**: In `auto_trader`, update `contracts/retro/v1/consumers.lock` to pin the new consumer commit. Once all consumers have migrated and enough observation time has elapsed, deprecate and safely retire `retro/v1`.
