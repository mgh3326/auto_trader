"""Locks invariants for ROB-285:

1. The Binance public-adapter package lives at exactly one path:
   app/services/brokers/binance/.
2. The package source contains no signed-endpoint surface (no method names
   matching the Binance signed-endpoint vocabulary, no X-MBX-APIKEY header
   constants).

Pre-existing string references to "binance" (e.g., fundamentals/news/research
handlers that mention Binance as a venue name) are recorded in
``ALLOWED_LEGACY_FILES`` and tracked explicitly. New code that references
Binance at the HTTP-client level must live inside the public-adapter package.

If this test starts failing, a future PR either added a parallel Binance
location or introduced signed-endpoint code. Extend the ALLOWED set with
explicit justification in the PR description, or roll back the change.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

# Allowed package directories for new Binance-related code in this PR.
# - app/services/brokers/binance: the public adapter (REST + WS) + the
#   spot_demo signed sub-package (ROB-296) + the demo ledger sub-package
#   (ROB-298). The legacy testnet sub-package was deleted in ROB-298.
# - app/services/instrument_health: write surface for crypto_instrument_health
#   (mentions Binance in docstrings as the first consumer; the service is
#   generic and could later be consumed by other crypto adapters).
ALLOWED_PACKAGE_PATHS: frozenset[str] = frozenset(
    {
        "app/services/brokers/binance",
        "app/services/instrument_health",
    }
)

# Pre-existing files (audit performed 2026-05-20 prior to ROB-285) that
# reference "binance" as a string/venue name only — no HTTP client behavior
# beyond the legacy fundamentals/news endpoints. New parallel Binance code
# must live inside ``ALLOWED_PACKAGE_PATHS`` and must not be added here.
ALLOWED_LEGACY_FILES: frozenset[str] = frozenset(
    {
        "app/mcp_server/tooling/fundamentals_handlers.py",
        "app/mcp_server/tooling/fundamentals_sources_binance.py",
        "app/mcp_server/tooling/fundamentals_sources_naver.py",
        "app/mcp_server/tooling/fundamentals/_crypto.py",
        # ROB-443: crypto screener snapshot funding-rate enrichment. No new Binance
        # HTTP code — it imports the existing public funding fetcher
        # (_fetch_funding_rate_batch in fundamentals_sources_binance, already
        # allow-listed); the Binance reference here is only that import path.
        "app/services/invest_crypto_screener_snapshots/derivatives.py",
        "app/models/research_backtest.py",
        "app/schemas/research_backtest.py",
        "app/services/crypto_insight_snapshots/builder.py",
        "app/services/crypto_news_relevance_service.py",
        "app/services/daily_candles/repository.py",
        "app/services/external/crypto_insights.py",
        "app/services/invest_crypto_naver_adapter/adapter.py",
        "app/services/invest_view_model/market_dashboard_service.py",
        "app/services/invest_view_model/market_parity_service.py",
        "app/services/market_events/taxonomy.py",
        "app/services/news_payload_normalizer.py",
        "app/services/news_radar_classifier.py",
        "app/services/research_backtest_parser.py",
        "app/utils/symbol_mapping.py",
        # ROB-285 additions outside the broker package — referenced because
        # the docstring or sentry tag mentions "Binance" as the first
        # consumer / source of these flows.
        "app/models/crypto_instrument_health.py",
        # ROB-298 — the Spot Demo order ledger ORM model lives under
        # app/models/. The model is registered in app/models/__init__.py
        # (one-line import); both files are tracked here so the audit
        # doesn't flag them as "unexpected Binance locations". The legacy
        # binance_testnet_order_ledger.py file was deleted in ROB-298.
        "app/models/binance_demo_order_ledger.py",
        "app/models/__init__.py",
        # ROB-313 / ROB-315 — the scalp_trade_analytics ORM model lives under
        # app/models/. It is analytics-only persistence (no HTTP/WS, no signed
        # surface); "Binance" appears only in its docstring as the venue whose
        # Demo scalping round-trips it records. Same precedent as
        # binance_demo_order_ledger.py above. The model is registered in
        # app/models/__init__.py (one-line import).
        "app/models/scalp_trade_analytics.py",
        # ROB-315 — the scalping review-loop ORM model. Analytics/review
        # persistence only (no HTTP/WS, no signed surface); references Binance
        # solely via the ``binance_demo`` account-scope literal + docstring.
        "app/models/scalping_reviews.py",
        # ROB-307 PR4 — Demo scalping scheduler orchestration. These
        # *orchestrate* the in-package Demo adapters (executor/clients) and
        # perform no signed HTTP themselves (the HMAC chokepoints stay inside
        # spot_demo/ + futures_demo/). They reference "Binance" only via
        # imports + the BINANCE_DEMO_SCALPING_* env-flag names.
        "app/jobs/binance_demo_scalping_runner.py",
        "app/tasks/binance_demo_scalping_tasks.py",
        # Phase 2 — Demo scalping daily review + buy&hold benchmark automation.
        # Orchestration only: the job rolls scalp_trade_analytics into the review
        # draft and computes the benchmark via the in-package adapters; the flow
        # is a thin Prefect wrapper; config holds the default-off env flag. No
        # signed HTTP/WS — references "Binance" via imports + the
        # BINANCE_DEMO_SCALPING_REVIEW_FLOW_ENABLED flag name only.
        "app/jobs/binance_demo_scalping_review.py",
        "app/flows/binance_demo_scalping_review_flow.py",
        "app/core/config.py",
        # ROB-323 / ROB-325 — the operator Naver remote-debug audit's Chrome
        # CDP host allowlist. Contains NO Binance HTTP/WS/signed surface; it is
        # a strict 127.0.0.1:9222 allowlist and references "binance" only in a
        # docstring naming binance/spot_demo/host_allowlist as the design
        # precedent it mirrors (strict-equality, no wildcard). String reference
        # only — same class as the other entries here.
        "app/services/action_report/remote_debug_audit/host_allowlist.py",
    }
)

# Symbol regex matches the function/method names Binance uses for signed
# endpoints. Adding any of these as a `def ...(` in the public adapter is a
# scope breach.
SIGNED_SYMBOL_RE = re.compile(
    r"\b(account|order|all_orders|my_trades|user_data_stream|"
    r"open_orders|cancel_order|transfer|asset|withdraw|deposit)\b\s*\(",
    re.IGNORECASE,
)


def _repo_root() -> pathlib.Path:
    # tests/services/brokers/binance/test_audit_no_signed_endpoints.py
    # parents[0]=binance, [1]=brokers, [2]=services, [3]=tests, [4]=repo root
    return pathlib.Path(__file__).resolve().parents[4]


def test_only_one_binance_package_path_exists() -> None:
    repo_root = _repo_root()
    result = subprocess.run(
        ["grep", "-rln", "-i", "binance", "--include=*.py", "app/"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    paths = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    unexpected: set[str] = set()
    for path in paths:
        # Path is allowed if it starts with one of the allowed package paths,
        # or if it's an explicitly-tracked legacy file.
        if any(
            path.startswith(allowed + "/") or path == allowed
            for allowed in ALLOWED_PACKAGE_PATHS
        ):
            continue
        if path in ALLOWED_LEGACY_FILES:
            continue
        unexpected.add(path)
    assert not unexpected, (
        f"Unexpected Binance code locations: {sorted(unexpected)}. "
        "ROB-285 invariant: new Binance HTTP/WS code lives in "
        "app/services/brokers/binance/ ONLY. If you intentionally added a new "
        "file referencing Binance, extend ALLOWED_LEGACY_FILES (for string "
        "references) or ALLOWED_PACKAGE_PATHS (for adapter code) in this test "
        "and justify in the PR description."
    )


def test_no_signed_endpoint_surface_in_binance_public_package() -> None:
    """ROB-285 public-adapter invariant — extended by ROB-286 and ROB-296.

    The signed-endpoint vocabulary (``order``, ``cancel_order``, etc.) is
    permitted ONLY inside the isolated signed sub-packages:
      * ``app/services/brokers/binance/testnet/`` (ROB-286 — Spot Testnet, deleted in ROB-298)
      * ``app/services/brokers/binance/spot_demo/`` (ROB-296 — Spot Demo)
      * ``app/services/brokers/binance/futures_demo/`` (ROB-298 PR 2 — Futures Demo)
    Anywhere else under ``app/services/brokers/binance/`` is the
    read-only public adapter and must not gain signed surface.
    """
    repo_root = _repo_root()
    pkg = repo_root / "app" / "services" / "brokers" / "binance"
    if not pkg.exists():
        # Until Task 4 introduces the package, this is fine.
        return
    isolated_signed_pkgs = (pkg / "testnet", pkg / "spot_demo", pkg / "futures_demo")
    offenders: list[tuple[pathlib.Path, int, str]] = []
    for py_file in pkg.rglob("*.py"):
        # ROB-286 + ROB-296 + ROB-298 PR 2: skip the isolated signed sub-packages
        # — signed methods legitimately live there.
        skip = False
        for signed_pkg in isolated_signed_pkgs:
            try:
                py_file.relative_to(signed_pkg)
                skip = True
                break
            except ValueError:
                continue
        if skip:
            continue
        for lineno, line in enumerate(py_file.read_text().splitlines(), 1):
            if SIGNED_SYMBOL_RE.search(line) and "def " in line:
                offenders.append((py_file, lineno, line.strip()))
    assert not offenders, (
        f"Signed-endpoint method names found in Binance public adapter: "
        f"{offenders}. ROB-285 public adapter must not expose signed-endpoint "
        "surface. Signed methods are allowed ONLY inside binance/testnet/, "
        "binance/spot_demo/, or binance/futures_demo/. If a name collision is "
        "unavoidable, rename or justify in PR description and update "
        "SIGNED_SYMBOL_RE."
    )


def test_no_api_key_header_constants_in_binance_public_package() -> None:
    """ROB-285 invariant — extended by ROB-286 and ROB-296.

    The ``X-MBX-APIKEY`` header constant is permitted ONLY inside the
    isolated signed sub-packages:
      * ``app/services/brokers/binance/testnet/`` (ROB-286 — Spot Testnet, deleted in ROB-298)
      * ``app/services/brokers/binance/spot_demo/`` (ROB-296 — Spot Demo)
      * ``app/services/brokers/binance/futures_demo/`` (ROB-298 PR 2 — Futures Demo)
    Anywhere else under ``app/services/brokers/binance/`` is the
    read-only public adapter and must not reference this header.
    """
    repo_root = _repo_root()
    pkg = repo_root / "app" / "services" / "brokers" / "binance"
    if not pkg.exists():
        return
    isolated_signed_pkgs = (pkg / "testnet", pkg / "spot_demo", pkg / "futures_demo")
    forbidden = "X-MBX-APIKEY"
    offenders: list[str] = []
    for py_file in pkg.rglob("*.py"):
        # ROB-286 + ROB-296 + ROB-298 PR 2: signed transports may legitimately
        # reference the header.
        skip = False
        for signed_pkg in isolated_signed_pkgs:
            try:
                py_file.relative_to(signed_pkg)
                skip = True
                break
            except ValueError:
                continue
        if skip:
            continue
        if forbidden in py_file.read_text():
            offenders.append(str(py_file))
    assert not offenders, (
        f"X-MBX-APIKEY header constant found in: {offenders}. "
        "Public adapter must never construct API-key headers. The header "
        "is permitted only inside app/services/brokers/binance/testnet/, "
        "app/services/brokers/binance/spot_demo/, or "
        "app/services/brokers/binance/futures_demo/. The transport event hook "
        "checks for this header at request time as defense in depth; the "
        "source itself must not reference it."
    )
