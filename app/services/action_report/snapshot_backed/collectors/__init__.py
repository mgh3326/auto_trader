"""ROB-273 — production snapshot collectors and registry assembly.

All collectors here are **read-only by contract**:

* No broker order create / cancel / modify.
* No watch alert activation.
* No scheduler / TaskIQ / Prefect registration.
* No external HTTP that could trigger remote mutation.

Required-kind collectors (portfolio, journal, watch_context, market) query
local DB state populated by upstream sync flows. Optional-kind collectors
are either thin DB readers or fail-open stubs that return ``unavailable``
without raising.
"""
