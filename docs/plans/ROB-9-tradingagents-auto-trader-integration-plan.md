# ROB-9 — TradingAgents Advisory → auto_trader Decision Workspace Plan

- **PR scope:** Linear ROB-9, single PR slice. No UI, no API endpoint, no scheduler, no broker/watch side effect.
- **Branch / worktree:** `feature/ROB-9-tradingagents-advisory-integration` (this worktree).
- **Status:** Plan only. **No code is to be written until this plan is reviewed.**
- **Depends on:**
  - ROB-1 / PR #595 — `trading_decision_*` schema, models, service (merged to `main`).
  - ROB-2 / PR #597 — `trading_decisions` API contract endpoints (merged to `main`).
  - TradingAgents fork branch `auto-trader-research-runner` @ `8d3fa63` providing
    `scripts/run_auto_trader_research.py`. The runner emits **advisory-only JSON**
    (`advisory_only: true`, `execution_allowed: false`) and is the only TradingAgents
    surface auto_trader is permitted to call.

> ⚠️ **Advisory-only.** This PR ships a research-ingestion adapter that converts a
> TradingAgents runner JSON payload into a `TradingDecisionSession` +
> `TradingDecisionProposal` row with `source_profile="tradingagents"`. It performs
> **zero** broker, watch-alert, paper-trading, Paperclip, or Redis-watch-key writes.
> It is **forbidden** to create `TradingDecisionAction` rows, register watch alerts,
> place live or paper orders, call `place_order`, `manage_watch_alerts`, KIS/Upbit
> trading services, or treat the TradingAgents output as authorization for a real or
> paper trade. Those concerns are out of scope and tracked as follow-ups (§10).

---

## 1. Goal

Provide a single internal Python entry point that:

1. Invokes the TradingAgents runner (`scripts/run_auto_trader_research.py`) as a
   short-lived subprocess against a known local OpenAI-compatible shim.
2. Validates the resulting JSON against a strict Pydantic contract that **enforces the
   advisory-only invariants** (`advisory_only=True`, `execution_allowed=False`,
   `status="ok"`).
3. Persists the validated payload into the existing ROB-1 trading_decision tables as
   one session + one proposal, mapped so a future Decision Workspace UI can render the
   advisory next to other proposals **without** any execution affordance attached.

The single-call surface from auto_trader code is:

```python
async def ingest_tradingagents_research(
    db: AsyncSession,
    *,
    user_id: int,
    symbol: str,
    instrument_type: InstrumentType,
    as_of_date: date | None = None,
    analysts: Sequence[str] | None = None,
) -> tuple[TradingDecisionSession, TradingDecisionProposal]:
    ...
```

Nothing else (no FastAPI route, no Discord push, no cron registration) is in scope.

---

## 2. In-scope vs Out-of-scope

| Area | In scope (this PR) | Deferred |
|---|---|---|
| Pydantic models for the runner JSON contract | ✅ `app/schemas/tradingagents_research.py` | — |
| `TradingAgentsResearchService` subprocess wrapper | ✅ `app/services/tradingagents_research_service.py` | — |
| Settings additions for repo path / runner path / shim URL / model / timeout | ✅ `app/core/config.py` | — |
| Mapping runner output → `TradingDecisionSession` + `TradingDecisionProposal` | ✅ Reuses existing `trading_decision_service.create_decision_session` / `add_decision_proposals` | — |
| Unit tests: subprocess success, failure, timeout, JSON parse error, invariant violations | ✅ `tests/services/test_tradingagents_research_service.py` | — |
| Integration test: persisted session/proposal shape against a stubbed subprocess | ✅ `tests/services/test_tradingagents_research_service_integration.py` | — |
| Side-effect-safety test: importing the new service must not load forbidden execution modules | ✅ `tests/services/test_tradingagents_research_service_safety.py` | — |
| FastAPI endpoint to trigger an ingestion | ❌ | ROB-10 (proposed) |
| UI surface for `source_profile="tradingagents"` proposals | ❌ | follow-up (UI track) |
| Action / counterfactual / outcome creation for these proposals | ❌ — **forbidden** | product decision required before any future PR |
| Scheduler / CLI to run the runner on a symbol list | ❌ | follow-up |
| Discord / Telegram delivery of new advisories | ❌ | follow-up |
| Modifying the TradingAgents fork itself | ❌ | upstream-only |
| Reading or echoing API keys / env values in logs | ❌ — **forbidden** | — |

---

## 3. Workflow the service must support

```text
caller → ingest_tradingagents_research(db, user_id, symbol, instrument_type)
         │
         ├─ build argv from settings + caller args (no shell interpolation)
         ├─ asyncio.create_subprocess_exec(...)         # stdout=PIPE, stderr=PIPE, env filtered
         ├─ communicate() with timeout                   # cancel + kill on timeout
         ├─ json.loads(stdout)                           # ParseError → raise
         ├─ TradingAgentsRunnerResult.model_validate(..) # invariant violations → raise
         ├─ create_decision_session(source_profile="tradingagents", ...)
         ├─ add_decision_proposals([single advisory proposal])
         └─ return (session, proposal)
```

The caller is responsible for the surrounding `db.commit()`. The service performs only
`session.flush()` (consistent with `trading_decision_service`) so the caller controls
the transaction boundary.

---

## 4. Files

### 4.1 New files

| File | Purpose |
|---|---|
| `app/schemas/tradingagents_research.py` | Pydantic v2 models for runner JSON contract + helpers |
| `app/services/tradingagents_research_service.py` | Subprocess invocation + mapping to `trading_decision_*` |
| `tests/services/test_tradingagents_research_service.py` | Unit tests (subprocess stubbed) |
| `tests/services/test_tradingagents_research_service_integration.py` | Integration tests (real DB, stubbed subprocess) |
| `tests/services/test_tradingagents_research_service_safety.py` | Forbidden-import boundary test |
| `tests/fixtures/tradingagents/runner_ok_nvda.json` | Canned runner output for tests |
| `tests/fixtures/tradingagents/runner_invariant_violation.json` | `execution_allowed=true` payload to assert rejection |

### 4.2 Files modified

| File | Change |
|---|---|
| `app/core/config.py` | Add `tradingagents_*` settings block (§5) |
| `app/services/__init__.py` | **No re-export needed.** Service is imported by absolute path; do not add to package init to avoid pulling subprocess code into unrelated import paths. |

### 4.3 Files NOT modified

| File | Reason |
|---|---|
| `app/services/trading_decision_service.py` | New service composes existing helpers; no schema or signature change. |
| `app/models/trading_decision.py` | No new columns, no new enum values. `proposal_kind="other"` is already valid for non-actionable advisories. |
| `app/routers/trading_decisions.py` | No endpoint exposure in this PR. |
| `app/services/order_service.py`, `app/services/brokers/*`, `app/services/kis*`, `app/services/upbit*`, `app/services/watch_alerts.py`, `app/services/paper_trading_service.py` | **Must not be touched or imported.** Forbidden by §9. |

---

## 5. Settings additions (`app/core/config.py`)

Append to `Settings`:

```python
# TradingAgents advisory runner (ROB-9)
tradingagents_repo_path: str | None = None
# Path to the python interpreter that has `tradingagents` installed.
# Typically the .venv python inside the TradingAgents fork checkout.
tradingagents_python: str | None = None
# Defaults to <repo>/scripts/run_auto_trader_research.py when None.
tradingagents_runner_path: str | None = None
tradingagents_base_url: str = "http://127.0.0.1:8796/v1"
tradingagents_model: str = "gpt-5.5"
tradingagents_default_analysts: str = "market"
tradingagents_subprocess_timeout_sec: int = 300
tradingagents_max_debate_rounds: int = 1
tradingagents_max_risk_discuss_rounds: int = 1
tradingagents_max_recur_limit: int = 30
tradingagents_output_language: str = "English"
tradingagents_checkpoint_enabled: bool = False
# Optional path under which to write a per-run JSON copy for ops review.
# When None (default) no extra file is written.
tradingagents_memory_log_path: str | None = None
```

**Resolution rules (in service code, not config):**

- If `tradingagents_repo_path` is `None` → service raises `TradingAgentsNotConfigured`.
- If `tradingagents_python` is `None` → fall back to `sys.executable` only when
  the runner is importable from auto_trader's own venv. Otherwise raise
  `TradingAgentsNotConfigured`. (Default expectation: separate venv configured.)
- If `tradingagents_runner_path` is `None` →
  `Path(tradingagents_repo_path) / "scripts/run_auto_trader_research.py"`.
- All three resolved paths must exist and be readable before the subprocess is
  launched; otherwise raise `TradingAgentsNotConfigured`.

**Env policy:** the service passes a **filtered** env to the subprocess containing
only `PATH`, `HOME`, `LANG`, `LC_ALL`, `PYTHONPATH` (when set), and
`TRADINGAGENTS_*` / `OPENAI_API_KEY` keys that are already present in
`os.environ`. Other variables are **not** forwarded, never logged, never echoed,
and never embedded in error messages.

---

## 6. JSON contract (`app/schemas/tradingagents_research.py`)

The runner output is documented in `scripts/run_auto_trader_research.py:run_analysis`.
Mirror it as Pydantic v2 models with **strict literal pins** on the safety fields.

```python
from typing import Literal
from datetime import date
from pydantic import BaseModel, Field, ConfigDict


class TradingAgentsLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    provider: str
    model: str
    base_url: str


class TradingAgentsConfigSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    max_debate_rounds: int
    max_risk_discuss_rounds: int
    max_recur_limit: int
    output_language: str
    checkpoint_enabled: bool


class TradingAgentsWarnings(BaseModel):
    model_config = ConfigDict(extra="allow")  # forward-compatible
    structured_output: list[str] = Field(default_factory=list)


class TradingAgentsRunnerResult(BaseModel):
    """Strict contract for run_auto_trader_research.py JSON output.

    Invariant pins (advisory_only / execution_allowed) are Literal-typed so
    Pydantic itself rejects any payload that claims execution authorization.
    """
    model_config = ConfigDict(extra="ignore")

    status: Literal["ok"]
    symbol: str = Field(min_length=1, max_length=64)
    as_of_date: date
    decision: str
    advisory_only: Literal[True]
    execution_allowed: Literal[False]
    analysts: list[str] = Field(min_length=1)
    llm: TradingAgentsLLM
    config: TradingAgentsConfigSnapshot
    warnings: TradingAgentsWarnings
    final_trade_decision: str
    raw_state_keys: list[str]
```

**Why `Literal[True]` / `Literal[False]`:** any deviation (e.g. an upstream change
flipping `execution_allowed` to `True`) is caught at validation time and raises
`ValidationError`, which the service surfaces as `AdvisoryInvariantViolation`
**before** any DB write.

**Error payload contract:** the runner can also exit with `status != "ok"` in the
future. Today only `"ok"` is emitted, but the service treats any payload that
fails this model — including a payload with `status="error"` — as a runner failure
and raises `TradingAgentsRunnerError` without any DB write.

---

## 7. Service design (`app/services/tradingagents_research_service.py`)

### 7.1 Public surface

```python
class TradingAgentsNotConfigured(RuntimeError): ...
class TradingAgentsRunnerError(RuntimeError):
    """Subprocess exited non-zero, timed out, produced no JSON, or produced JSON
    that fails the contract. Includes a redacted summary suitable for logs."""
class AdvisoryInvariantViolation(RuntimeError):
    """Reserved name; today this is raised by Pydantic ValidationError on the
    Literal-pinned fields and re-wrapped here so callers can catch it explicitly."""


async def run_tradingagents_research(
    *,
    symbol: str,
    instrument_type: InstrumentType,
    as_of_date: date | None = None,
    analysts: Sequence[str] | None = None,
) -> TradingAgentsRunnerResult:
    """Invoke the runner subprocess and return a validated, advisory-only result.
    Does NOT touch the DB."""


async def ingest_tradingagents_research(
    db: AsyncSession,
    *,
    user_id: int,
    symbol: str,
    instrument_type: InstrumentType,
    as_of_date: date | None = None,
    analysts: Sequence[str] | None = None,
) -> tuple[TradingDecisionSession, TradingDecisionProposal]:
    """run_tradingagents_research + map onto a single session/proposal.
    Caller owns commit."""
```

### 7.2 Subprocess invocation rules

- Use `asyncio.create_subprocess_exec(*argv, stdout=PIPE, stderr=PIPE, env=filtered_env)`.
- **Never** use `shell=True`, `os.system`, or string interpolation into a shell.
- `argv` is a `list[str]` built explicitly:
  ```python
  argv = [
      tradingagents_python,
      tradingagents_runner_path,
      "--symbol", symbol,
      "--date", as_of_date.isoformat(),
      "--analysts", ",".join(analysts),
      "--base-url", settings.tradingagents_base_url,
      "--model", settings.tradingagents_model,
      "--max-debate-rounds", str(settings.tradingagents_max_debate_rounds),
      "--max-risk-discuss-rounds", str(settings.tradingagents_max_risk_discuss_rounds),
      "--max-recur-limit", str(settings.tradingagents_max_recur_limit),
      "--output-language", settings.tradingagents_output_language,
  ]
  if settings.tradingagents_checkpoint_enabled:
      argv.append("--checkpoint-enabled")
  ```
- Validate caller-supplied `symbol` against `^[A-Za-z0-9._/-]{1,32}$` before adding to
  argv. Reject anything else with `ValueError` (defense-in-depth even though
  `create_subprocess_exec` does not invoke a shell).
- Validate each analyst name against `^[a-z_]{1,32}$`.
- `cwd = settings.tradingagents_repo_path` so the runner's relative file lookups work.

### 7.3 Timeout + cleanup

```python
proc = await asyncio.create_subprocess_exec(...)
try:
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=settings.tradingagents_subprocess_timeout_sec,
    )
except asyncio.TimeoutError:
    proc.kill()
    await proc.wait()
    raise TradingAgentsRunnerError("tradingagents runner timed out")
if proc.returncode != 0:
    raise TradingAgentsRunnerError(
        f"tradingagents runner exited with {proc.returncode}"
    )
```

`stderr` is captured for debug logging at `logger.debug(...)` only — **never**
echoed at INFO/ERROR level in full because the runner can include arbitrary
content. Truncate to 4 KiB before logging and strip any line containing
`"key"`, `"token"`, `"secret"`, `"authorization"` (case-insensitive) before write.

### 7.4 JSON parsing

```python
try:
    payload = json.loads(stdout.decode("utf-8"))
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise TradingAgentsRunnerError("runner produced non-JSON stdout") from exc
try:
    result = TradingAgentsRunnerResult.model_validate(payload)
except ValidationError as exc:
    raise TradingAgentsRunnerError(
        f"runner output failed advisory contract: {exc.errors(include_url=False)}"
    ) from exc
```

The `from exc` chain is preserved; the wrapped message **does not** include the
raw payload (which may contain rationale text that ought not be repeated in error
logs uncritically).

### 7.5 DB mapping

```python
session_obj = await trading_decision_service.create_decision_session(
    db,
    user_id=user_id,
    source_profile="tradingagents",
    strategy_name=f"tradingagents:{result.llm.model}:{','.join(result.analysts)}",
    market_scope=_market_scope_for(instrument_type),  # "kr" / "us" / "crypto"
    market_brief={
        "advisory_only": True,
        "execution_allowed": False,
        "llm": result.llm.model_dump(),
        "config": result.config.model_dump(),
        "warnings": result.warnings.model_dump(),
        "raw_state_keys": result.raw_state_keys,
    },
    generated_at=datetime.combine(result.as_of_date, time.min, tzinfo=UTC),
    notes=(
        "TradingAgents advisory research; advisory-only. "
        "No execution, watch alert, or paper trade is authorized by this row."
    ),
)

[proposal] = await trading_decision_service.add_decision_proposals(
    db,
    session_id=session_obj.id,
    proposals=[
        ProposalCreate(
            symbol=result.symbol,
            instrument_type=instrument_type,
            proposal_kind=ProposalKind.other,   # advisory output, no actionable kind yet
            side="none",                         # researcher does not size the trade
            original_payload={
                "advisory_only": True,
                "execution_allowed": False,
                "decision": result.decision,
                "final_trade_decision": result.final_trade_decision,
                "warnings": result.warnings.model_dump(),
                "llm": result.llm.model_dump(),
                "config": result.config.model_dump(),
                "as_of_date": result.as_of_date.isoformat(),
            },
            original_rationale=result.decision[:4000],
        ),
    ],
)
```

Notes on this mapping:

- `proposal_kind=other` is intentional: ROB-1 enum allows it, and we deliberately do
  **not** infer `enter`/`add`/`trim` from the LLM's free-form decision text in this
  PR. Inference (and any side that follows from it) is a product question.
- `side="none"` keeps the row out of accidental `side IN ('buy','sell')` queries
  used by execution-adjacent code paths.
- All `original_*` numeric fields are left `None`. The advisory does not size or
  price the trade.
- `market_brief` carries the full advisory-only invariant flags at the **session**
  level so any UI listing this session can refuse execution affordances even before
  rendering proposal detail.

### 7.6 Optional run log

If `settings.tradingagents_memory_log_path` is set, the service additionally writes
the validated `result.model_dump_json()` to
`{memory_log_path}/{as_of_date}/{symbol}-{session_uuid}.json`. The path is built
with `Path(...).resolve()` and asserted to remain inside the configured base via
`is_relative_to`. No env values are written. This step is best-effort: an OSError
is logged at WARNING and does **not** abort the DB transaction (the row already
captures the same payload in `original_payload`).

---

## 8. Tests

All under `tests/services/`. Each test file is independent.

### 8.1 `test_tradingagents_research_service.py` (unit, no DB)

Patch `asyncio.create_subprocess_exec` with a `unittest.mock.AsyncMock` that
returns a fake process whose `communicate()` is preprogrammed.

| Test | Setup | Assertion |
|---|---|---|
| `test_runner_ok_returns_validated_result` | stdout = canned valid JSON, returncode=0 | returns `TradingAgentsRunnerResult` with `advisory_only is True` and `execution_allowed is False` |
| `test_runner_nonzero_exit_raises` | returncode=1 | `TradingAgentsRunnerError`, no DB call attempted |
| `test_runner_timeout_kills_and_raises` | `communicate()` raises `asyncio.TimeoutError` via `wait_for` patch | `TradingAgentsRunnerError("tradingagents runner timed out")`, `proc.kill()` was called, `proc.wait()` was awaited |
| `test_runner_non_json_stdout_raises` | stdout = `b"<<not json>>"` | `TradingAgentsRunnerError` |
| `test_runner_status_error_rejected` | stdout JSON has `"status": "error"` | `TradingAgentsRunnerError` (Literal mismatch) |
| `test_runner_advisory_only_false_rejected` | stdout JSON has `"advisory_only": false` | `TradingAgentsRunnerError`; **no DB write** verifiable because no DB session is even constructed |
| `test_runner_execution_allowed_true_rejected` | stdout JSON has `"execution_allowed": true` | `TradingAgentsRunnerError` |
| `test_warnings_structured_output_preserved` | stdout JSON has 2 entries in `warnings.structured_output` | result.warnings.structured_output equals the input list |
| `test_symbol_argv_validation_rejects_shell_metachars` | call with `symbol="AAPL; rm -rf /"` | `ValueError`, subprocess never invoked |
| `test_settings_missing_repo_path_raises` | `settings.tradingagents_repo_path = None` | `TradingAgentsNotConfigured` |
| `test_filtered_env_does_not_leak_unrelated_vars` | os.environ contains a junk var; subprocess invoked | the `env=` kwarg passed to `create_subprocess_exec` does **not** contain that key |

### 8.2 `test_tradingagents_research_service_integration.py` (DB-integration)

Marker: `@pytest.mark.integration`. Reuses the `_create_user` / `_cleanup_user`
helpers used by `tests/models/test_trading_decision_service.py`.

| Test | Assertion |
|---|---|
| `test_ingest_creates_session_and_single_proposal` | `source_profile == "tradingagents"`; one proposal, `proposal_kind == ProposalKind.other`, `side == "none"` |
| `test_ingest_persists_advisory_invariants_in_market_brief_and_payload` | `session.market_brief["advisory_only"] is True`, `session.market_brief["execution_allowed"] is False`, `proposal.original_payload["advisory_only"] is True`, `proposal.original_payload["execution_allowed"] is False` |
| `test_ingest_preserves_warnings_structured_output` | `session.market_brief["warnings"]["structured_output"]` equals canned list |
| `test_ingest_does_not_create_action_or_counterfactual_or_outcome` | After ingestion, querying `trading_decision_actions`, `trading_decision_counterfactuals`, `trading_decision_outcomes` for the proposal returns 0 rows |
| `test_ingest_runner_failure_rolls_back` | runner raises → caller-side `db.rollback()` works; assert no session row exists |
| `test_ingest_does_not_touch_user_response_fields` | `proposal.user_response == "pending"`, `responded_at is None`, all `user_*` fields None |

### 8.3 `test_tradingagents_research_service_safety.py`

Clone the subprocess pattern from
`tests/models/test_trading_decision_service.py:test_service_module_does_not_import_execution_paths`,
swapping the target to `app.services.tradingagents_research_service`.

`_FORBIDDEN_PREFIXES` extends the existing list with:

```python
"app.services.watch_alerts",
"app.services.paper_trading_service",
"app.services.openclaw_client",
"app.services.crypto_trade_cooldown_service",
```

Plus the existing forbidden set (kis*, upbit*, brokers, order_service,
fill_notification, execution_event, redis_token_manager, kis_websocket*,
app.tasks).

The test imports the new service in a clean subprocess and asserts none of
these modules ended up in `sys.modules` as a transitive consequence.

### 8.4 Test fixtures

`tests/fixtures/tradingagents/runner_ok_nvda.json` — captured (or hand-written)
runner output for NVDA on a fixed date; mirrors §6 exactly.
`tests/fixtures/tradingagents/runner_invariant_violation.json` — same shape but
`execution_allowed=true`; used by §8.1 to assert rejection.

---

## 9. Forbidden-import / forbidden-call boundary

The new service module **must not**:

- Import from any module whose path starts with: `app.services.kis*`,
  `app.services.upbit*`, `app.services.brokers`, `app.services.order_service`,
  `app.services.fill_notification`, `app.services.execution_event`,
  `app.services.redis_token_manager`, `app.services.kis_websocket*`,
  `app.services.watch_alerts`, `app.services.paper_trading_service`,
  `app.services.openclaw_client`, `app.tasks`.
- Call `place_order`, `manage_watch_alerts`, or any function that creates rows in
  `trading_decision_actions`, `trading_decision_counterfactuals`, or
  `trading_decision_outcomes`.
- Write to Redis (no `redis.Redis(...).set(...)`, no token-manager touchpoints).
- Read or echo `os.environ` values into log records, exceptions, or persisted rows.
  Specifically: `OPENAI_API_KEY`, `TRADINGAGENTS_*`, `KIS_*`, `UPBIT_*`,
  `GOOGLE_API_KEY*`, `TELEGRAM_TOKEN`, `OPENDART_API_KEY`, `KRX_*` must never appear
  in stdout, stderr, exception messages, or `original_payload`/`market_brief` JSON.

§8.3 enforces the import boundary. §8.1's
`test_filtered_env_does_not_leak_unrelated_vars` enforces the env boundary.
The remaining behaviors (no execution-side writes) are guaranteed by §8.2's
`test_ingest_does_not_create_action_or_counterfactual_or_outcome`.

---

## 10. Out-of-scope follow-ups (proposed Linear children of ROB-9)

The following are **explicitly deferred** to follow-up issues. None of them are
implemented in this PR and the design must not pre-empt them in ways that imply
an execution path is acceptable.

| Follow-up | Description | Reason it is not in this PR |
|---|---|---|
| ROB-10 (proposed) — API endpoint | `POST /trading/api/research/tradingagents` accepting `{symbols: [...], instrument_type, as_of_date?}` and returning the persisted `SessionDetail` | Needs auth + rate-limiting design, plus product call on whether viewers can trigger remote LLM compute |
| Decision Workspace UI surface | Render `source_profile="tradingagents"` proposals in the existing inbox + detail pages | Out of UI track; ROB-7 owns the workspace UI |
| Scheduler / batch CLI | Nightly run over a curated symbol list | Needs ops decision on shim availability and cost budget |
| Discord / Telegram delivery | Auto-post advisories | Needs the UI surface first so users can ack/reject from a single source of truth |
| Action wiring | Allow accept → live order from a tradingagents proposal | **Product decision required.** Today the design assumes advisories are read-only references and *cannot* drive execution. Re-opening this requires a separate Linear issue with explicit safety review. |
| Inference of `proposal_kind` (`enter`/`add`/`trim`) from `decision` text | Useful for filtering | Not in this PR — would create the appearance of an actionable signal where none has been validated |

---

## 11. Implementation order (TDD-first, bite-sized)

Each step is a single commit. Run the test suite after every code-touching step.

1. **Settings additions** — extend `Settings` per §5. Add a unit test that parses
   the new env vars and confirms `tradingagents_runner_path` resolution.
2. **Schema** — write `app/schemas/tradingagents_research.py`. Test:
   `model_validate` accepts the canned ok-payload and rejects the
   invariant-violation payload.
3. **Service skeleton** — empty class, custom exception types, public function
   signatures. Test: importing the module raises nothing and the safety test
   (§8.3) passes.
4. **Subprocess invocation (success path)** — implement `run_tradingagents_research`
   with a stubbed `create_subprocess_exec`. Test §8.1 ok case + warnings
   preservation.
5. **Subprocess invocation (failure paths)** — non-zero exit, timeout (kill+wait),
   non-JSON stdout, validation errors. Tests §8.1 failure cases.
6. **Argv & env hardening** — symbol/analyst regex validation, env filtering.
   Tests §8.1 metachar rejection + env filter.
7. **DB mapping** — implement `ingest_tradingagents_research`. Tests §8.2.
8. **Optional memory-log writer** — implement under `tradingagents_memory_log_path`
   guard; test that without the setting nothing is written, with the setting
   exactly one file appears under the expected path.
9. **Safety test** — add the forbidden-import boundary test (§8.3) and verify it
   passes.
10. **Docs touch-up** — add a 5-line stub to `CLAUDE.md` under "환경 변수" listing
    the new `tradingagents_*` envs and pointing at this plan.

---

## 12. Self-review checklist (run before opening the PR)

- [ ] No `app/services/kis*`, `app/services/upbit*`, `app/services/brokers`,
      `app/services/order_service`, `app/services/watch_alerts`,
      `app/services/paper_trading_service` imports anywhere in the diff.
- [ ] No `place_order`, `manage_watch_alerts`, broker construction, Redis
      write, Paperclip call.
- [ ] No `TradingDecisionAction`, `TradingDecisionCounterfactual`, or
      `TradingDecisionOutcome` row created by the new service or tests.
- [ ] `advisory_only=True` and `execution_allowed=False` are present at three
      layers: Pydantic `Literal` pin, `session.market_brief`,
      `proposal.original_payload`.
- [ ] No `os.environ` values appear in any log record, exception message,
      `original_payload`, `market_brief`, or memory-log file. Grep the diff for
      `KEY`, `SECRET`, `TOKEN`, `os.environ` and confirm.
- [ ] No `subprocess.run(..., shell=True)` and no f-string interpolation into
      argv.
- [ ] All §8 tests pass locally:
      `uv run pytest tests/services/test_tradingagents_research_service*.py -v`
- [ ] `make lint && make typecheck` pass.
- [ ] PR description links Linear ROB-9 and explicitly states **"advisory-only,
      no execution path"** in the summary.

---

**End of plan.** Implementation should not begin until this plan is reviewed.
