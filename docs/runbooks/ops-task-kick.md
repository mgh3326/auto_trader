# NCP TaskIQ task-kick API

`at-api` exposes a deliberately narrow, token-only TaskIQ dispatch surface for
external automation. The NCP worker holds runtime credentials and executes the
registered task; a Prefect flow or node only holds the ops token and never needs
broker API keys.

## Configuration

Set `OPS_TASK_KICK_TOKEN` in the existing operator-managed runtime secret file.
The request header is `X-Ops-Task-Token` by default and can only be renamed with
`OPS_TASK_KICK_TOKEN_HEADER`. Do not put either value in the repository. An
unset token returns 403; a missing or mismatched supplied token returns 401.
Session cookies and user permissions are not an alternative authentication path.

Only the static registry in `app/services/ops_task_kick/registry.py` can be
kicked. It excludes order, watch, proposal, Telegram, and ledger-record tasks.
`kis_live.reconcile_periodic` is accepted only with an empty parameter object;
the API injects `dry_run=true` and offers no apply parameter.

## Kick and poll

Use one `Idempotency-Key` for one intended run. The API stores its task ID in
Redis with `SET NX EX 3600`; a duplicate request returns the same ID with
`deduplicated: true`.

```bash
TASK_ID="$(
  curl --fail-with-body --silent --show-error \
    -X POST 'https://at.example/trading/api/ops/tasks/build_invest_screener_snapshots/kick' \
    -H "X-Ops-Task-Token: $OPS_TASK_KICK_TOKEN" \
    -H 'Idempotency-Key: prefect-screener-20260902-1' \
    -H 'Content-Type: application/json' \
    --data '{"market":"kr","all_symbols":true,"commit":false}' \
    | jq -r '.task_id'
)"

curl --fail-with-body --silent --show-error \
  "https://at.example/trading/api/ops/tasks/runs/${TASK_ID}" \
  -H "X-Ops-Task-Token: $OPS_TASK_KICK_TOKEN"
```

The run endpoint returns `pending`, `done`, or `error` when the configured
TaskIQ result backend can answer. It returns explicit `unknown` when no usable
result backend exists or its status cannot be read; `unknown` is not success.
Returned results are shape-only summaries and never include task output,
environment values, secrets, or broker responses.

## Prefect conversion pattern

Replace a local CLI invocation with one `requests.post` call, retain its
returned `task_id`, then poll the run endpoint at a bounded interval and bounded
deadline. Treat `error`, `unknown`, or a deadline expiry as flow failure; only
`done` is completion. This keeps execution and all market/broker credentials in
the NCP worker while Prefect remains a token-authenticated trigger.
