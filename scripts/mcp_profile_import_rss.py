"""Measure the boot-time memory of one MCP profile, without starting a server.

ROB-1436: each MCP unit is its own Python process. This script runs, in a fresh
child interpreter per profile, what ``app.mcp_server.main`` does before it
starts serving: install the lazy-import switch for the profile, import the
tooling package and run ``register_all_tools``. It reports the process memory
at that point (RSS on Linux, physical footprint on macOS), which heavy third-party packages were really executed, and the RSS
after those packages are first touched (what a unit holds once a tool has used
them). ``--eager`` skips the lazy-import switch, which is the behaviour before
ROB-1436, so one run gives a before/after pair from the same tree.

No server, network, database or broker is touched; tools are registered into
an in-memory recorder and never called. The child gets fixed placeholder
settings only and never reads a real env or credential file.

Usage:
    uv run python -m scripts.mcp_profile_import_rss h3-us-paper kiwoom
    uv run python -m scripts.mcp_profile_import_rss --compare --repeat 3 h3-us-paper
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

HEAVY_MODULES = (
    "pandas",
    "numpy",
    "yfinance",
    "scipy",
    "finnhub",
    "pandas_market_calendars",
    "exchange_calendars",
)

_PLACEHOLDER_ENV = {
    "KIS_APP_KEY": "DUMMY",
    "KIS_APP_SECRET": "DUMMY",
    "KIS_ACCOUNT_NO": "00000000-00",
    "TELEGRAM_TOKEN": "DUMMY",
    "TELEGRAM_CHAT_IDS": "1",
    "OPENDART_API_KEY": "DUMMY",
    "UPBIT_ACCESS_KEY": "DUMMY",
    "UPBIT_SECRET_KEY": "DUMMY",
    "DATABASE_URL": "postgresql+asyncpg://placeholder:placeholder@127.0.0.1:1/none",
    "REDIS_URL": "redis://127.0.0.1:1/0",
    "SECRET_KEY": "Placeholder_Secret_Key_12345_Placeholder_Key",
    "SENTRY_DSN": "",
    "ENVIRONMENT": "test",
    "MCP_AUTH_TOKEN": "",
}

_CHILD = r"""
import json, os, sys
from typing import Any, cast

profile_name, heavy, eager = sys.argv[1], sys.argv[2].split(","), sys.argv[3] == "1"


class _Recorder:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        name = kwargs.get("name")
        direct = args[0] if args and callable(args[0]) else None
        if name is None and args and isinstance(args[0], str):
            name = args[0]

        def register(function):
            self.tools[name or function.__name__] = function
            return function

        return register(direct) if direct is not None else register


def _rss_mb():
    # Linux (the NCP host): resident set size from /proc. macOS: the
    # physical footprint (what Activity Monitor shows). Plain RSS on macOS
    # leaves out compressed pages, so under memory pressure it under-reports
    # and a before/after pair is meaningless.
    try:
        with open("/proc/self/statm") as handle:
            pages = int(handle.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 1)
    except OSError:
        pass
    import ctypes

    class _RusageInfoV2(ctypes.Structure):
        _fields_ = [
            ("uuid", ctypes.c_uint8 * 16),
            *[(f"f{i}", ctypes.c_uint64) for i in range(7)],
            ("phys_footprint", ctypes.c_uint64),
            *[(f"g{i}", ctypes.c_uint64) for i in range(20)],
        ]

    info = _RusageInfoV2()
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    if libproc.proc_pid_rusage(os.getpid(), 2, ctypes.byref(info)) != 0:
        raise OSError("proc_pid_rusage failed")
    return round(info.phys_footprint / (1024 * 1024), 1)


from app.mcp_server.profiles import McpProfile

profile = McpProfile(profile_name)
from app.mcp_server import lazy_imports

deferred_at_install = () if eager else lazy_imports.install_lazy_imports(profile)
from app.mcp_server.tooling import register_all_tools

recorder = _Recorder()
register_all_tools(cast(Any, recorder), profile=profile)
boot_rss = _rss_mb()
executed = sorted(
    m for m in heavy if m in sys.modules and not lazy_imports.is_deferred(m)
)
still_deferred = sorted(m for m in heavy if lazy_imports.is_deferred(m))
for name in still_deferred:
    getattr(sys.modules[name], "__version__", None)  # first real use
print(json.dumps({
    "profile": profile_name,
    "mode": "eager" if eager else "lazy",
    "boot_rss_mb": boot_rss,
    "after_first_use_rss_mb": _rss_mb(),
    "tool_count": len(recorder.tools),
    "heavy_executed_at_boot": executed,
    "heavy_deferred_at_boot": still_deferred,
}))
"""


def measure(profile: str, *, eager: bool = False) -> dict[str, object]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV"}
    }
    env.update(_PLACEHOLDER_ENV)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD,
            profile,
            ",".join(HEAVY_MODULES),
            "1" if eager else "0",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"profile {profile} measurement failed:\n{completed.stderr[-2000:]}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _median(rows: list[dict[str, object]], key: str) -> float:
    values = [row[key] for row in rows]
    assert all(isinstance(value, int | float) for value in values)
    return round(statistics.median(float(value) for value in values), 1)  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("profiles", nargs="+", help="MCP_PROFILE values")
    parser.add_argument("--json", action="store_true", help="print JSON lines")
    parser.add_argument(
        "--compare", action="store_true", help="measure eager (before) and lazy"
    )
    parser.add_argument(
        "--eager", action="store_true", help="skip the lazy-import switch"
    )
    parser.add_argument("--repeat", type=int, default=1, help="runs per mode")
    args = parser.parse_args(argv)
    modes = [True, False] if args.compare else [args.eager]
    for profile in args.profiles:
        for eager in modes:
            rows = [measure(profile, eager=eager) for _ in range(args.repeat)]
            if args.json:
                for row in rows:
                    print(json.dumps(row))
                continue
            row = rows[-1]
            print(
                f"{profile:<16} {row['mode']:<5} "
                f"boot_rss={_median(rows, 'boot_rss_mb'):>6} MB "
                f"after_first_use={_median(rows, 'after_first_use_rss_mb'):>6} MB "
                f"(median of {len(rows)}) tools={row['tool_count']:>3} "
                f"executed={','.join(row['heavy_executed_at_boot']) or '-'} "
                f"deferred={','.join(row['heavy_deferred_at_boot']) or '-'}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
