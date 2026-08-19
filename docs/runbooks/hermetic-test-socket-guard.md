# Hermetic test socket guard (ROB-1265 / ROB-1880 / ROB-1296)

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
- Lets an explicitly marked `live` item cross its intentional boundary **only
  when the operator also passed `--run-live`**, while retaining the default-deny
  policy during collection/import. `integration` grants nothing (ROB-1296).
- Propagates an environment switch and dedicated `sitecustomize` directory into
  child Python processes, including children started with `env={}`. Direct
  `curl`/`wget`/`ssh`-family launchers and Python `-S`/`-E`/`-I` startup-bypass
  flags are rejected before they start.
- Raises `ExternalSocketBlocked` immediately for any other address instead of
  letting the test hang on or perform a real network round trip.

## Exemptions

There is exactly one exemption, and it needs **both** halves:

| `integration` | `live` | `--run-live` | external sockets |
| --- | --- | --- | --- |
| – | – | – | blocked |
| ✓ | – | – | blocked |
| ✓ | – | ✓ | blocked |
| – | ✓ | – | item is skipped by `tests/conftest.py`; blocked |
| ✓ | ✓ | – | item is skipped by `tests/conftest.py`; blocked |
| – | ✓ | ✓ | **allowed** |
| ✓ | ✓ | ✓ | **allowed** |

ROB-1296 removed the blanket `integration` exemption. It covered ~3,200 items in
the default `-m "not live"` gate — every one of them free to reach a real
external host from CI. Integration tests need loopback PostgreSQL/Redis, and the
address allowlist permits that with no marker at all, so the marker was never
the mechanism keeping DB tests working. `pytest --run-live` alone is likewise
inert: without the `live` marker on the item, nothing is exempt.

`tests/test_rob1296_live_only_socket_guard.py` pins this table end to end, in
real nested pytest sessions, without emitting a packet.

Do **not** add a new opt-out marker for "this test happens to hit the network";
that is exactly the failure mode this guard exists to catch. There is no
environment-variable bypass, and adding one is out of bounds. Fix the test
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
`live` boundary (run under `--run-live`) rather than an offline test.

### Suite-wide boundary fixtures

Some external calls are reached from deep inside a fan-out that individual tests
never see — a best-effort enrichment, a cache-miss fallback, an OAuth refresh.
`tests/conftest.py` blocks those at the boundary with autouse fixtures, each
paired with an opt-out fixture a boundary test can request:

| Boundary | Opt out with |
| --- | --- |
| All external HTTP, via httpx's real transports and the `requests` adapter | `allow_external_http` |
| TradingView scanner HTTP | `allow_tvscreener_http` |
| KIS daily-candle cache-miss fallback (`fetch_kr_daily_unclamped`) | `allow_kis_daily_candle_fetch` |

The external-HTTP boundary (`tests/_external_http_boundary.py`) is the broad one,
and it replaced what would otherwise have been seven per-provider stubs (KIS,
Upbit, KRX, Naver, CoinGecko, Binance, the USD/KRW feed). It patches **only**
`httpx.AsyncHTTPTransport` / `httpx.HTTPTransport` and
`requests.adapters.HTTPAdapter` — the transports that actually speak to a
network. `ASGITransport` (FastAPI `TestClient`), `WSGITransport`,
`MockTransport`, respx and any custom `AsyncBaseTransport` are untouched, and
loopback stays reachable. It raises `httpx.ConnectError` /
`requests.ConnectionError`, the same types a blocked connect already produces,
so no `except httpx.HTTPError` or retry branch changes behaviour.

Every blocked request is counted by host and printed in the terminal summary:

```
ROB-1296 external HTTP boundary: 1 blocked requests across 1 hosts -- example.invalid=1
```

Treat a **rising** count, or a new host, as a newly under-mocked provider —
that line is the reason a leak cannot reappear silently. Do not assert on the
counts of real provider hosts in a test: that would make an existing leak a
required baseline. `tests/test_rob1296_external_http_boundary.py` pins the
mechanics against a dedicated `.invalid` probe instead.

The boundary is a backstop, not a substitute for mocking. It stops the suite
from *attempting* an external request; a provider call whose result the test
actually depends on must still be stubbed at its call site.

Each raises the same class of error the caller already handles, so observable
behaviour is unchanged — only the socket disappears. Prefer a module-level
autouse fixture (see `tests/test_mcp_place_order.py`,
`tests/mcp_server/tooling/test_live_order_ledger.py`) when the boundary matters
to one file rather than the whole suite. Do not "fix" a leak by asserting a
success the test never verified: model the outcome the test already relies on.

### Known fail-closed edges

These are deliberate. They are recorded here so a future reader finds a decision
rather than a mystery, and none of them is a reason to widen an allowlist.

- **`multiprocessing` `forkserver`** brokers children over an `AF_UNIX` socket in
  a per-process temp directory. Admitting it would mean allowlisting a directory
  *prefix*, and "any `AF_UNIX` path is local enough" is precisely the bypass
  ROB-1880 closed. Nothing here uses it: all three `get_context` call sites ask
  for `spawn`. `spawn` and `fork` children are guarded and covered by tests.
- **`python -I` / `-E` / `-S` children** are rejected, because each one discards
  the `sitecustomize` startup hook and would run unguarded. A test that wanted an
  isolated interpreter for *import provenance* should assert each module's
  `__file__` instead — see `tests/services/research/test_research_contracts_wheel.py`,
  which is a stronger check than `-I` ever was.
- **DNS resolution is not intercepted.** `socket.getaddrinfo` resolves inside
  libc, below the Python `socket.socket` methods the guard patches, so a
  hostname lookup can still leave the machine. The *connection* is still blocked:
  `socket.create_connection(("example.com", 443))` resolves and then trips the
  guard at `connect`. Treat the guard as blocking connections, not lookups.
- **Non-Python child environments are not rewritten**, so a native binary with
  its own network stack is outside what Python can intercept. Direct
  `curl`/`wget`/`ssh`-family launchers are rejected by name before they start.
