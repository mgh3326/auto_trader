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
- Patches `socket.socket.connect`, `connect_ex`, `sendto` and `sendmsg`, plus
  `socket.getaddrinfo` and `gethostbyname`. This covers direct synchronous calls,
  asyncio's socket connect/send paths, and name resolution — which happens
  *before* `connect` and would otherwise leave the machine on its own.
- Allows only TCP/UDP loopback literals (`127.0.0.0/8`, `::1`, `localhost`) and
  these exact UNIX service paths: `/tmp/.s.PGSQL.5432`,
  `/private/tmp/.s.PGSQL.5432`, `/var/run/postgresql/.s.PGSQL.5432`, and
  `/var/run/redis/redis-server.sock`. An arbitrary `AF_UNIX` path is denied.
- Lets an explicitly marked `live` item cross its intentional boundary **only
  when the operator also passed `--run-live`**, while retaining the default-deny
  policy during collection/import. `integration` grants nothing (ROB-1296).
- Installs itself in every Python child — direct, `sh -c`/`bash -lc`,
  `uv run python`, `executable=` overrides, `shell=True`, and `multiprocessing`
  `fork`/`spawn` — including children started with `env={}`. The child's
  *exemption* travels out of band (see below), never as an environment value.
  Direct `curl`/`wget`/`ssh`-family launchers, Python `-S`/`-E`/`-I`, and
  commands that strip the guard's startup state before an interpreter are
  rejected before they start.
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

### Three layers, and what each opt-out actually releases

Hermeticity is enforced at three depths. They are not redundant — each catches
what the one below it cannot express.

| Layer | What it is | Where |
| --- | --- | --- |
| **Provider seams** | The actual fix. Each external provider is stopped at the narrowest function that owns its request, so a default run never even builds the request. | `tests/_provider_boundaries.py` |
| **External HTTP backstop** | Fail-closed net for a provider nobody seamed. Patches httpx's real-network transports, the `requests` adapter, and `curl_cffi`'s session classes. | `tests/_external_http_boundary.py` |
| **Socket guard** | Last resort, below every library. Refuses the syscall and the DNS lookup. | `tests/_socket_guard.py` |

Opting out of one layer never opts out of the ones beneath it:

| Opt-out | Provider seams | HTTP backstop | Socket guard |
| --- | --- | --- | --- |
| `allow_external_providers` | released | **still on** | **still on** |
| `allow_external_http` | still on | released | **still on** |
| `allow_tvscreener_http` / `allow_kis_daily_candle_fetch` | that one seam released | still on | **still on** |
| `live` marker **and** `--run-live` | released | released | released |

So a boundary test can drive a real client against a mocked transport without
being able to reach a network, and only an operator-armed `live` item gets the
whole way out. Reach for the narrowest opt-out that makes the test honest.

Which one a test needs follows from what it is testing:

- Testing *the provider client itself* against a mocked HTTP layer (see
  `tests/test_naver_finance.py`, `tests/test_upbit_retry.py`) →
  `allow_external_providers`.
- Testing *the transport boundary*, with its own `AsyncHTTPTransport` stand-in →
  `allow_external_http`.
- Testing anything else → no opt-out. A blocked call means the test is
  under-mocked; mock the provider at its call site.

### The seam alias contract

`SEAM_TARGETS` lists each seam's defining module **and** every module that
aliased it via `from x import y` at import time. Patching only the definition
leaves consumers such as `market_data.service.fetch_orderbook` pointing at the
real function, and a half-patched seam is worse than none — the leak just moves
to whichever consumer was missed.

The list is explicit rather than discovered per test: the fixture runs once per
item across ~20k items, so a `sys.modules` sweep there would be a real cost (the
same reason `tests._mcp_tooling_support._patch_runtime_attr` keeps a fixed module
list). `tests/test_rob1296_provider_boundaries.py` is what makes that trade safe:
it imports the whole `app` package and recomputes the bindings, opting *out* of
the seams so it compares original identities, and matching on identity **or** the
`__rob1296_seam__` marker so a consumer first imported during a seamed test — and
therefore holding a stale replacement — is caught too. Removing a known alias
makes it fail and name the module.

### Suite-wide boundary fixtures

Some external calls are reached from deep inside a fan-out that individual tests
never see — a best-effort enrichment, a cache-miss fallback, an OAuth refresh.
`tests/conftest.py` blocks those at the boundary with autouse fixtures, each
paired with an opt-out fixture a boundary test can request:

| Boundary | Opt out with |
| --- | --- |
| All external providers, at their call-roots | `allow_external_providers` |
| All external HTTP, via httpx's real transports, the `requests` adapter, and `curl_cffi` | `allow_external_http` |
| TradingView scanner HTTP | `allow_tvscreener_http` |
| KIS daily-candle cache-miss fallback (`fetch_kr_daily_unclamped`) | `allow_kis_daily_candle_fetch` |

Every layer raises the exception an unreachable host already produced —
`httpx.ConnectError` / `requests.ConnectionError` — so every `except
httpx.HTTPError`, retry and fail-open branch behaves exactly as it did before.
No payload is ever invented. `ASGITransport` (FastAPI `TestClient`),
`WSGITransport`, `MockTransport`, respx and custom transports are untouched, and
loopback stays reachable.

### Why `curl_cffi` needs its own interception

`curl_cffi` binds libcurl. It is **not** an httpx transport and **not** a
`requests` adapter, and libcurl issues `connect(2)` from C — below the Python
`socket` methods the guard patches. So a client built on it slips past all three
layers at once, and does it *silently*: every counter reads zero.

That is not hypothetical. `yfinance` (and this repo's
`SentryTracingCurlSession`) use it, and an audit of the first ROB-1296 landing
found **29 non-live tests making 107 real requests to
`query1.finance.yahoo.com`** while the guard reported `blocked_attempts=0` and
the HTTP counter reported `0 blocked requests`.

It is now intercepted at `curl_cffi.requests.Session.request` (and
`AsyncSession`), the single Python chokepoint every curl_cffi request passes
through, and the Yahoo client's own entry points are seamed at layer 1.

The general lesson is worth keeping: **a client that reaches the network through
a native extension is invisible to all three layers.** When adding an HTTP
dependency, check whether it goes out through Python sockets. If it does not, it
needs its own chokepoint here — and until it has one, its traffic will not
appear in any counter.

### The counter, and the contract it enforces

Every request the HTTP backstop blocks is counted by host and printed in the
terminal summary:

```
ROB-1296 external HTTP boundary: 1 blocked requests across 1 hosts -- example.invalid=1
```

**The contract is that a full `-m "not live"` run shows zero real provider
hosts.** Anything other than this suite's own `.invalid` probes means a provider
call escaped its seam — treat a new host, or a rising count, as a regression and
add the seam. Do not assert on the counts of real provider hosts in a test:
that would make an existing leak a required baseline.
`tests/test_rob1296_external_http_boundary.py` pins the mechanics against a
dedicated `.invalid` probe instead.

The backstop is not a substitute for mocking. It stops the suite from
*attempting* an external request; a provider call whose result the test actually
depends on must still be stubbed at its call site.

### Child processes carry the parent's policy

A child must not get a *different* answer than the item that created it, and it
must not be able to *claim* an answer of its own.

The exemption therefore never travels in the environment. A boolean any test
could export would be exactly the general bypass this guard exists to prevent —
`os.environ[...] = "1"` followed by a `spawn` would have bought a child real
network access. Instead:

- **`subprocess` children** get a one-shot `os.pipe()` per launch. The parent
  writes its in-memory decision, closes the write end, and passes only the read
  descriptor (merged into the caller's `pass_fds`); `AUTO_TRADER_TEST_SOCKET_GUARD_POLICY_FD`
  names the descriptor, never the decision. `sitecustomize` reads it once, closes
  it and pops the variable. Inheriting a live descriptor is something a parent
  grants, not something a child asserts.
- **`multiprocessing` `fork`** inherits the installed state in memory.
- **`multiprocessing` `spawn`** takes the decision from `spawn`'s preparation
  data, and its bootstrap is rewritten (`get_command_line`) to bake the absolute
  project root in and call `install()` before `spawn_main` — so installation does
  not depend on any environment the child inherits.

Fail-closed at every step: an absent channel means no exemption; a channel that
is present but unreadable or malformed is a hard failure, never a silent
downgrade; and `prepare` requires the key to be present and a real `bool`.

Two consequences worth knowing:

- **Integrity is not policy.** `-E`/`-I`/`-S`, `env -i`, unsetting or overriding
  `PYTHONPATH`/the guard switch before an interpreter — all are refused *whatever
  the parent's policy*, including an armed `live` item. The exemption is scoped
  to one test item, and an ungovernable child outlives it. Only the
  network-client deny list (`curl`/`wget`/`ssh`-family) is exemptible, because
  that is a policy question rather than an integrity one.
- **`multiprocessing` `forkserver` is unsupported and refused at the boundary.**
  Its server interpreter boots through a path the guard does not control and then
  forks every child, so no child policy can be guaranteed. An earlier version let
  it proceed and leaned on the `AF_UNIX` broker socket being blocked, but "the
  socket was refused" is not evidence that a child would have had the right
  policy. ROB-1296 never required it and nothing here selects it — all three
  `get_context` call sites ask for `spawn`. Use `spawn` or `fork`.

### Known fail-closed edges

These are deliberate. They are recorded here so a future reader finds a decision
rather than a mystery, and none of them is a reason to widen an allowlist.

- **`python -I` / `-E` / `-S` children** are rejected: each discards the
  `sitecustomize` startup hook and would run unguarded. A test that wants an
  isolated interpreter for *import provenance* should scrub `sys.path` inside the
  child instead — see `tests/services/research/test_research_contracts_wheel.py`,
  which keeps the guard installed, removes the checkout before importing, and
  then sweeps every loaded `app.*` / `research_contracts.*` module for wheel
  provenance. That is strictly stronger than `-I`: it also catches the transitive
  imports a hand-picked `__file__` check would miss.
- **DNS is blocked, not just connections.** `socket.getaddrinfo` and
  `gethostbyname` fail closed for non-loopback names, because resolution happens
  *before* `connect` — letting it through would emit a real query even though the
  connection itself is refused. `None`/empty hosts (passive local binds) and
  loopback are allowed, so local PostgreSQL/Redis are unaffected.
- **Non-Python child environments are not rewritten**, so a native binary with
  its own network stack is outside what Python can intercept. Direct
  `curl`/`wget`/`ssh`-family launchers are rejected by name before they start.
- **`multiprocessing` `forkserver`** — see above; refused outright.
