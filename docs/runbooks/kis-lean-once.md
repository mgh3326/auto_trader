# KR lean `--once` shadow runner

This is a manual, KR-only vertical slice. It is not registered with TaskIQ,
cron, Prefect, or any supervisor. The current strategy is synthetic and always
returns `NO_ORDER`.

```bash
uv run python scripts/kis_lean_once.py --symbol 005930 --events /tmp/kr-lean-events.jsonl
```

The command emits JSONL for decision, order intent, KIS pre-submit, fill,
position/reconcile, and Discord. The pre-submit stage is structurally
shadow-only: it has inspection but no broker client or submit operation, and
blocks until account ownership is confirmed. It also records the existing
`watch_auto_execute_mock` surface as a competing writer concern; this runner
does not import, call, or modify that surface.

Discord output is represented by an observable event sink in this slice. Both
successful shadow completion and failure/stop paths set `must_notify=true`.
Actual webhook delivery belongs to the operational shell contract and is not
added here.
