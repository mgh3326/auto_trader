# Hermetic test socket guard (ROB-1265 / ROB-1880)

`tests._socket_guard_plugin` is loaded by pytest's `-p` option before test
collection and module import. It fails closed on outbound socket operations in
the default (`not live`) suite. The guard exists because the full suite was
found to silently reach real external hosts (Finnhub, open.er-api.com,
CoinGecko, Upbit, KIS live) from tests that never intended to — leaks that are
invisible in a networked CI runner make offline runs nondeterministic and can
touch real broker/API endpoints.

## What it does

- Installs before collection/import, including `pytest --noconftest`. A
  missing or replaced intercept is a pytest failure, never a warning or skip.
- Patches `socket.socket.connect`, `connect_ex`, `sendto`, and `sendmsg`. This
  covers direct synchronous calls plus asyncio's socket connect/send paths.
- Allows only TCP/UDP loopback literals (`127.0.0.0/8`, `::1`, `localhost`) and
  these exact UNIX service paths: `/tmp/.s.PGSQL.5432`,
  `/private/tmp/.s.PGSQL.5432`, `/var/run/postgresql/.s.PGSQL.5432`, and
  `/var/run/redis/redis-server.sock`. An arbitrary `AF_UNIX` path is denied.
- Lets an explicitly marked `integration` or `live` item cross its intentional
  boundary while retaining the default-deny policy during collection/import.
- Propagates an environment switch and dedicated `sitecustomize` directory into
  child Python processes, including children started with `env={}`. Direct
  `curl`/`wget`/`ssh`-family launchers and Python `-S`/`-E`/`-I` startup-bypass
  flags are rejected before they start.
- Raises `ExternalSocketBlocked` immediately for any other address instead of
  letting the test hang on or perform a real network round trip.

## Exemptions

Tests marked `@pytest.mark.integration` or `@pytest.mark.live` are exempt
during that test item's setup/call/teardown — those are the two marker families
the repo already uses for intentional boundaries (DB, or `--run-live` network).
Do **not** add a new opt-out marker for "this test happens to hit the network";
that is exactly the failure mode this guard exists to catch. Fix the test
instead:

1. Find the leaf provider function the code path actually calls (e.g.
   `_get_finnhub_client`, `_fetch_company_profile_finnhub`,
   `upbit_service.fetch_multiple_current_prices`, `httpx.AsyncClient`).
2. Patch it at the module that resolves the name at call time. Because many
   modules do `from ... import _fetch_company_profile_finnhub` locally,
   patching only the defining module is not enough — patch every consuming
   module, or use
   `tests._mcp_tooling_support._patch_runtime_attr(monkeypatch, name, value)`.
3. Re-run the test with the guard active (it is on by default) to confirm no
   `ExternalSocketBlocked` is raised.

## Diagnosing a trip

The raised `ExternalSocketBlocked` message includes the blocked `(host, port)`
— that is the fastest way to identify which external service the test
under-mocked. If the traceback lands inside `asyncio`/`anyio` internals with no
application frames (common for `httpx.AsyncClient` happy-eyeballs connection
attempts), fall back to `pytest -k <test_name> -v` in isolation to confirm the
single test that trips it.

## CI evidence and subprocess scope

CI passes `--socket-guard-report` on every xdist shard. The plugin aggregates
worker reports at the controller and writes JSON containing guard activation,
worker count, and per-operation blocked-attempt counters; the workflow uploads
that JSON even when pytest fails.

The child-process protection is a Python test-harness boundary, not an OS
network namespace. Standard `subprocess.Popen` callers (and therefore
`run`/`check_*`/asyncio subprocess helpers) receive guarded startup state for
direct Python children, while direct common network-client launchers are
rejected. Non-Python child environments are deliberately not rewritten. A test
that deliberately invokes a native executable through a lower-level `exec*`
syscall or embeds a network stack in an otherwise allowlisted native binary is
outside what Python can intercept; such a test must be treated as an
integration/live boundary rather than an offline test.
