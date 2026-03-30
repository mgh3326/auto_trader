# Autoresearch Orchestrator Design

## Goal

Add a multi-round autoresearch orchestrator under `backtest/` that repeatedly hands off strategy edits to an AI agent, runs the existing single-round experiment runner, and stops safely under defined resource and failure limits.

## Scope

- Create `backtest/orchestrator.py`
- Update `backtest/program.md` for orchestrator-driven autonomous loops
- Add focused tests for orchestrator helper logic

Out of scope:

- Changing `backtest/run_experiment.py` interfaces
- Modifying `backtest/prepare.py`, `backtest/backtest.py`, or `backtest/fetch_data.py`
- Letting the orchestrator edit `backtest/strategy.py` directly

## Architecture

The orchestrator will be a standalone CLI entrypoint that wraps `uv run backtest/run_experiment.py --description ...` inside a bounded loop.

Two modes share the same control flow:

1. `manual`
   - The orchestrator waits for a new `HEAD` commit to appear.
   - Once a new commit is detected, it derives the experiment description from `--description` or the latest commit subject and runs one experiment round.
2. `auto`
   - The orchestrator invokes an external AI CLI first.
   - The AI CLI reads `backtest/program.md`, modifies only `backtest/strategy.py`, and creates one commit.
   - The orchestrator verifies that `HEAD` advanced before it runs one experiment round.

The orchestrator does not duplicate keep/revert logic. `run_experiment.py` remains the only place that decides whether to keep or revert a strategy commit and the only place that appends experiment results to `results.tsv`.

## CLI Design

The orchestrator CLI will support:

- `--mode {manual,auto}` with default `manual`
- `--rounds` for total valid experiment rounds
- `--timeout` for total wall-clock runtime
- `--max-consecutive-reverts` for safety pausing
- `--ai-cli` for the external agent executable path, default `claude`
- `--description` as an optional override for experiment descriptions
- `--poll-interval` as an internal usability option, default around 2 seconds
- `--ai-timeout` for the agent subprocess in `auto` mode

Description resolution order:

1. Explicit `--description`
2. `git log -1 --format=%s`
3. Fail fast with a clear error if neither is available

## Round Lifecycle

Each loop iteration will:

1. Check shutdown/timeout conditions
2. Check disk free space and warn about dirty git state
3. Acquire a new commit
   - `manual`: poll `git rev-parse HEAD` until it differs from the last processed commit
   - `auto`: run the AI CLI, then verify that `HEAD` changed
4. Resolve the description string
5. Execute `uv run backtest/run_experiment.py --description <text>`
6. Read the latest appended row from `results.tsv`
7. Update in-memory stats and print per-round summary

Only completed experiment rounds count toward `--rounds`.

## State Tracking

The orchestrator keeps only in-memory control state:

- `initial_best_score`
- `current_best_score`
- `best_experiment_id`
- `kept_count`
- `reverted_count`
- `crashed_count`
- `skipped_count`
- `consecutive_reverts`
- `start_time`
- `last_processed_head`
- `shutdown_requested`

After each `run_experiment.py` call, the orchestrator reads the last `results.tsv` row to obtain:

- experiment id
- cv score
- status
- description

`current_best_score` and `best_experiment_id` advance only on `keep`/`kept`.

## Safety Rules

- Stop before starting a round if free disk space is below 500 MB
- Warn when `git status --porcelain` is non-empty
- Exit with code `3` when `consecutive_reverts > max_consecutive_reverts`
- Stop when total elapsed time exceeds `--timeout`
- Handle `SIGINT` gracefully by setting a shutdown flag and printing the final summary after the current experiment finishes

## Manual Mode Behavior

`manual` mode is long-running. It waits for a new commit after each processed round:

- Save the last processed `HEAD`
- Poll every few seconds until `HEAD` changes
- Print `Waiting for new commit... (Ctrl+C to stop)` while polling
- Continue checking timeout and shutdown conditions during polling

This allows an external AI coding agent to operate asynchronously while the orchestrator only handles the experiment execution loop.

## Auto Mode Behavior

In `auto` mode, the orchestrator calls the configured AI CLI with a prompt that instructs the agent to:

- Read `backtest/program.md`
- Use the current best score and round number as context
- Apply exactly one experimental idea
- Modify only `backtest/strategy.py`
- Commit once with a descriptive message

If the AI CLI exits without producing a new commit:

- Treat the attempt as `skip`, not `crash`
- Do not increment `consecutive_reverts`
- Do not count it as a completed experiment round
- Log the reason and continue unless overall timeout or shutdown stops the loop

If the AI CLI times out or fails in a way that makes further progress unsafe, the orchestrator exits with a clear error message.

## Logging and Reporting

Per-round output will include:

- current round number
- current best score
- experiment id
- score delta from best
- keep/revert/crash result
- cumulative keep/revert/crash counts

Final summary will include:

- attempted valid rounds
- kept, reverted, crashed, skipped counts and percentages
- score improvement from initial to final best
- best experiment id
- total wall-clock time

## Documentation Updates

`backtest/program.md` will gain an `Orchestrator Usage` section that documents:

- `manual` and `auto` command examples
- the commit-subject description fallback
- the manual waiting behavior
- the `strategy.py`-only edit rule
- operating safeguards and expected loop behavior

## Testing Strategy

Tests should focus on deterministic helper behavior rather than subprocess-heavy full integration:

- parsing best score from `results.tsv`
- parsing the latest result row
- description fallback rules
- summary/duration formatting
- status-to-counter updates
- skip semantics for auto-mode no-commit outcomes

Runtime verification should additionally cover:

- `uv run backtest/orchestrator.py --mode manual --rounds 3`
- `uv run backtest/orchestrator.py --mode auto --rounds 1 --ai-cli claude`
- revert-limit exit path
- graceful `Ctrl+C` shutdown
