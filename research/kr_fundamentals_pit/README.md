# KR fundamentals PIT

Manual, research-only OpenDART history; no database, scheduler or app imports.
Use an artifact root outside the repository and a private environment variable.

## Data contract

`fetch` caches successful corp-code, disclosure-list and financial responses without
credentials. A run is identified by its requested filing window and `--run-id`;
rerun the same ID to resume within a new call budget. Use a new ID to refresh.
Disclosure searches cover each calendar month through the explicit coverage end,
including amendments. A statement is accepted only when every response row has
the latest listed receipt number. CFS is preferred; OFS is used only when CFS
reports no data. API errors are failures, not missing financial values.

`build` publishes immutable JSONL partitions by run ID and appends a JSONL
manifest entry containing SHA-256, row counts, coverage window, budget used,
and missing reports with reasons. Fact rows carry symbol, corporation, business
year, report code and period, account, amount, currency, receipt number,
filing history, amendment flag, statement basis, and offset-aware `available_at`.
The latter is 23:59:59.999999 KST on the latest original/amended receipt date,
with `available_at_basis=latest_amendment_rcept_date_eod`. A new run can add a
new version after a later amendment; existing partitions remain unchanged.
`as_of` selects the latest eligible version per symbol and period and refuses
queries later than the published coverage end. A newer filing with no matching
statement invalidates older values after its receipt day. The end date must be
before the current KST day, so an incomplete day is never published as complete.
Missing statement metrics are null
with a reason. Values are exact decimal strings; a disclosed zero remains zero.

The statement endpoint cannot select a historical receipt. Values before an
amendment are unavailable; they are not reconstructed. For older amendments
only the latest version is available, conservatively dated at the latest receipt.
The receipt-specific XBRL API is a possible future source, outside this scope.
Current corp-code mapping and filing stock codes are not a historical listing universe.
Non-calendar fiscal quarters that cannot be mapped unambiguously are reported
missing. Annual income
amounts are annual, not inferred fourth-quarter values. Shares are provided only
when an unambiguous supported account exists.

## Commands

```sh
python -m research.kr_fundamentals_pit.cli fetch --root /path/outside/repo --key-env DART_KEY --start 2015-01-01 --end 2025-12-31 --run-id first --budget 1000
python -m research.kr_fundamentals_pit.cli build --root /path/outside/repo --key-env DART_KEY --start 2015-01-01 --end 2025-12-31 --run-id first
```

The service generally limits requests to 20,000 per day, but individual limits
may differ. The budget is per invocation, not a shared daily quota: the operator
must keep all invocations and other clients within the applicable daily limit.
Incomplete fetches stop cleanly and must be resumed before build.
