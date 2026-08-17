# Hermetic test socket guard (ROB-1265)

`tests/conftest.py::_block_external_sockets` is an autouse fixture that fails
closed on any outbound TCP connect to a non-loopback address during the
default (`not live`) `pytest` run. It exists because the full offline suite
was found to silently reach real external hosts (Finnhub, open.er-api.com,
CoinGecko, Upbit, KIS live) from tests that never intended to — the leaks
were invisible in CI (network present, so the calls just succeeded) but made
an offline/sandboxed run nondeterministic and, in the worst case, capable of
touching real broker/API endpoints.

## What it does

- Patches `socket.socket.connect` for the duration of each test.
- Loopback addresses (`127.0.0.1`, `::1`, `localhost`) and UNIX domain socket
  paths (local Postgres/Redis) are always allowed.
- Any other address raises `ExternalSocketBlocked` immediately instead of
  letting the test hang on/actually perform a real network round trip.

## Exemptions

Tests marked `@pytest.mark.integration` or `@pytest.mark.live` are exempt —
those are the two marker families the repo already uses for tests that
intentionally cross a real boundary (DB, or `--run-live` network). Do **not**
add a new opt-out marker for "this test happens to hit the network"; that is
exactly the failure mode this guard exists to catch. Fix the test instead:

1. Find the leaf provider function the code path actually calls (e.g.
   `_get_finnhub_client`, `_fetch_company_profile_finnhub`,
   `upbit_service.fetch_multiple_current_prices`, `httpx.AsyncClient`).
2. Patch it at the module that resolves the name at call time. Because many
   of these modules do `from ... import _fetch_company_profile_finnhub`
   locally, patching the *original* defining module is not enough — patch
   every module that imported it, or use
   `tests._mcp_tooling_support._patch_runtime_attr(monkeypatch, name, value)`
   which patches the attribute across every module in `_PATCH_MODULES`.
3. Re-run the test with the guard active (it is on by default) to confirm no
   `ExternalSocketBlocked` is raised.

## Diagnosing a trip

The raised `ExternalSocketBlocked` message includes the blocked
`(host, port)` — that is the fastest way to identify which external service
the test under-mocked. If the traceback lands inside `asyncio`/`anyio`
internals with no application frames (common for `httpx.AsyncClient`'s
happy-eyeballs connection attempts, which run as a separate anyio task), fall
back to `pytest -k <test_name> -v` in isolation to confirm which single test
trips it.
