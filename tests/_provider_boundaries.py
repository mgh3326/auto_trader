"""ROB-1296 provider seams: stop external calls at the call-root, not the socket.

The broad transport fixture in ``conftest.py`` is a fail-closed backstop — it
guarantees the suite *cannot* reach a network. These seams are the actual fix:
they stop each provider call one layer above httpx, so a default ``not live``
run never even builds an outbound request.

Every seam raises ``httpx.ConnectError`` / ``requests.ConnectionError`` — the
exact exception a blocked connect already produced — so every ``except
httpx.HTTPError``, retry and fail-open branch behaves exactly as before. Nothing
here fabricates a successful payload; a test that needs real provider data must
stub that provider itself.

Seams sit at the narrowest function that owns the request, because these
providers construct ``httpx.AsyncClient`` inline and expose no injectable client.

``SEAM_TARGETS`` lists each seam's defining module *and* every module that
aliased it via ``from x import y`` at import time. The list is explicit rather
than discovered per test: ``install`` runs once per test item across ~20k items,
so scanning ``sys.modules`` there would be a real cost. The same reasoning is why
``tests._mcp_tooling_support._patch_runtime_attr`` keeps a fixed module list.
``tests/test_rob1296_provider_boundaries.py`` recomputes the bindings from a
fully-imported ``app`` package and fails if this contract ever misses an alias.

Deliberately *not* seamed: ``fundamentals_sources_naver._fetch_binance_prices``
and ``_fetch_exchange_rate_usd_krw``. ``TestGetKimchiPremium`` drives both
through a monkeypatched ``httpx.AsyncClient``, so replacing the functions would
shadow the very behaviour under test; its one test that left them live stubs
them directly instead.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Final

MESSAGE: Final = (
    "External provider calls are disabled during pytest (ROB-1296). Stub this "
    "provider at its call site, or request allow_external_providers."
)
OPT_OUT_FIXTURE: Final = "allow_external_providers"
SEAM_MARKER: Final = "__rob1296_seam__"
"""Attribute stamped on every replacement, naming the seam it stands in for."""


def seam_marker(value: object) -> str | None:
    """Return the seam name a replacement stands in for, else ``None``."""

    return getattr(value, SEAM_MARKER, None)


# (attribute, modules binding it) — defining module first is not required; every
# listed module is rebound to the same replacement.
SEAM_TARGETS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    # USD/KRW rate: Toss primary, open.er-api.com fallback.
    ("_fetch_toss_usd_krw_quote", ("app.services.exchange_rate_service",)),
    ("_fetch_open_er_api_usd_krw_quote", ("app.services.exchange_rate_service",)),
    # Upbit: every public and private REST call funnels through these two.
    (
        "_request_json",
        (
            "app.services.brokers.upbit.client",
            "app.services.brokers.upbit.public_trades",
        ),
    ),
    ("_request_with_auth", ("app.services.brokers.upbit.client",)),
    (
        "fetch_orderbook",
        ("app.services.market_data.service", "app.services.upbit_orderbook"),
    ),
    # KRX public data portal.
    ("_fetch_max_working_date", ("app.services.krx",)),
    # Naver Finance scrapes.
    (
        "_fetch_html",
        (
            "app.services.naver_finance",
            "app.services.naver_finance.company",
            "app.services.naver_finance.investor",
            "app.services.naver_finance.news",
            "app.services.naver_finance.parser",
            "app.services.naver_finance.valuation",
        ),
    ),
    (
        "_fetch_html_with_client",
        (
            "app.services.naver_finance",
            "app.services.naver_finance.investor",
            "app.services.naver_finance.parser",
        ),
    ),
    (
        "_fetch_kr_snapshot",
        ("app.services.naver_finance", "app.services.naver_finance.investor"),
    ),
    (
        "fetch_valuation",
        ("app.services.naver_finance", "app.services.naver_finance.valuation"),
    ),
    (
        "fetch_sector_peers",
        ("app.services.naver_finance", "app.services.naver_finance.valuation"),
    ),
    # yfinance / Yahoo. These go out over curl_cffi (libcurl), which no other
    # layer can see: not an httpx transport, not a ``requests`` adapter, and its
    # ``connect(2)`` happens in C below the socket guard. All four are fail-open
    # enrichment paths, so raising the transport error they already handle keeps
    # behaviour identical. (The session *builder* is deliberately not seamed --
    # see SYNC_SEAM_TARGETS.)
    ("_collect_yfinance_snapshot", ("app.mcp_server.tooling.analysis_analyze",)),
    (
        "_fetch_valuation_yfinance",
        (
            "app.mcp_server.tooling.analysis_analyze",
            "app.mcp_server.tooling.fundamentals._valuation",
            "app.mcp_server.tooling.fundamentals_sources_yfinance",
        ),
    ),
    (
        "_fetch_investment_opinions_yfinance",
        (
            "app.mcp_server.tooling.analysis_analyze",
            "app.mcp_server.tooling.fundamentals._valuation",
            "app.mcp_server.tooling.fundamentals_sources_yfinance",
        ),
    ),
    (
        "_fetch_investment_opinions_yfinance_screen",
        ("app.mcp_server.tooling.fundamentals_sources_yfinance",),
    ),
    # Yahoo market-data client (also curl_cffi under the hood).
    ("fetch_ohlcv", ("app.services.brokers.yahoo.client",)),
    ("fetch_52w_high_date", ("app.services.brokers.yahoo.client",)),
    ("fetch_price", ("app.services.brokers.yahoo.client",)),
    (
        "fetch_fast_info",
        ("app.services.brokers.yahoo.client", "app.services.market_data.service"),
    ),
    ("fetch_prepost_quote", ("app.services.brokers.yahoo.client",)),
    ("fetch_fundamental_info", ("app.services.brokers.yahoo.client",)),
    # CoinGecko / alternative.me / Binance public.
    (
        "fetch_btc_dominance",
        (
            "app.mcp_server.tooling.fundamentals_sources_indices",
            "app.services.external.btc_dominance",
        ),
    ),
    (
        "fetch_alternative_me_fear_greed",
        (
            "app.services.crypto_insight_snapshots.builder",
            "app.services.external.crypto_insights",
        ),
    ),
    (
        "fetch_coingecko_global",
        (
            "app.services.crypto_insight_snapshots.builder",
            "app.services.external.crypto_insights",
        ),
    ),
    (
        "fetch_binance_funding_rates",
        (
            "app.services.crypto_insight_snapshots.builder",
            "app.services.external.crypto_insights",
        ),
    ),
)

# Synchronous seams. Same contract as ``SEAM_TARGETS`` but the replacement is a
# plain function, because these are called outside a coroutine.
#
# Empty on purpose. yfinance/Yahoo was the obvious candidate -- it goes out over
# curl_cffi (libcurl), which no other layer could see -- but seaming
# ``build_yfinance_tracing_session`` breaks the many tests that already stub
# ``yfinance.Ticker``/``download`` correctly and merely need a session *object*
# to hand through. curl_cffi exposes a single Python chokepoint
# (``Session.request``), so it is intercepted at the transport backstop instead,
# which blocks and counts without preventing construction.
SYNC_SEAM_TARGETS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = ()

# Methods patched on a class rather than a module.
ASYNC_METHOD_SEAMS: Final = (
    ("app.services.krx", "KRXSession", "fetch_data"),
    ("app.services.krx", "KRXSession", "fetch_resource"),
    (
        "app.mcp_server.tooling.screening.market_cap_cache",
        "MarketCapCache",
        "_fetch_market_caps",
    ),
    ("app.services.brokers.binance.rest_client", "BinancePublicRestClient", "_send"),
)

# Finnhub ships a synchronous ``requests``-backed client.
FINNHUB_CLIENT_FACTORIES: Final = (
    ("app.services.finnhub_news", "_get_finnhub_client"),
    ("app.services.market_events.finnhub_helpers", "_get_finnhub_client"),
    ("app.mcp_server.tooling.fundamentals_sources_finnhub", "_get_finnhub_client"),
)

# ``StockDetailProviders`` is a frozen, slotted dataclass and the default
# instance is bound as a default argument, so neither the module attribute nor
# the class attribute is reachable. The instance field is mutated in place and
# restored by the returned undo callable.
_FROZEN_PROVIDER_SEAM: Final = (
    "app.services.invest_view_model.stock_detail_service",
    "DEFAULT_STOCK_DETAIL_PROVIDERS",
    "recent_trades",
)


class _OfflineFinnhubClient:
    """Stand-in whose every call fails the way an unreachable Finnhub does."""

    def __getattr__(self, name: str):
        import requests

        def _blocked(*_args, **_kwargs):
            raise requests.ConnectionError(f"{MESSAGE} [api.finnhub.io.{name}]")

        return _blocked


def install(monkeypatch) -> Callable[[], None]:
    """Patch every provider seam to fail as an unreachable host would.

    Returns an undo callable for the one seam ``monkeypatch`` cannot revert.
    """

    import httpx

    def _async_blocked(label: str, *, seam: str | None = None):
        async def _blocked(*_args, **_kwargs):
            raise httpx.ConnectError(f"{MESSAGE} [{label}]")

        # Stable marker so the completeness contract can spot a *stale*
        # replacement, not just a module still holding the original. A module
        # imported for the first time while the seams were installed aliases the
        # replacement at import time; monkeypatch restores the modules it patched,
        # but not that late consumer, which would otherwise be invisible to an
        # original-identity scan.
        _blocked.__dict__[SEAM_MARKER] = seam if seam is not None else label
        return _blocked

    # Import every target *before* patching anything. A consumer that does
    # ``from x import y`` at module load and is first imported after ``x`` was
    # already patched binds the replacement permanently: ``monkeypatch`` records
    # that replacement as the "original" and restores it, leaving a stale seam
    # behind for the rest of the session.
    for _, module_paths in SEAM_TARGETS:
        for module_path in module_paths:
            importlib.import_module(module_path)
    for _, module_paths in SYNC_SEAM_TARGETS:
        for module_path in module_paths:
            importlib.import_module(module_path)
    for module_path, class_name, _ in ASYNC_METHOD_SEAMS:
        _ = class_name
        importlib.import_module(module_path)
    for module_path, _ in FINNHUB_CLIENT_FACTORIES:
        importlib.import_module(module_path)

    for attribute, module_paths in SEAM_TARGETS:
        replacement = _async_blocked(attribute, seam=attribute)
        for module_path in module_paths:
            module = importlib.import_module(module_path)
            if hasattr(module, attribute):
                monkeypatch.setattr(module, attribute, replacement)

    def _sync_blocked(label: str, *, seam: str):
        def _blocked(*_args, **_kwargs):
            raise httpx.ConnectError(f"{MESSAGE} [{label}]")

        _blocked.__dict__[SEAM_MARKER] = seam
        return _blocked

    for attribute, module_paths in SYNC_SEAM_TARGETS:
        replacement = _sync_blocked(attribute, seam=attribute)
        for module_path in module_paths:
            module = importlib.import_module(module_path)
            if hasattr(module, attribute):
                monkeypatch.setattr(module, attribute, replacement)

    for module_path, class_name, method in ASYNC_METHOD_SEAMS:
        module = importlib.import_module(module_path)
        owner = getattr(module, class_name, None)
        if owner is not None and hasattr(owner, method):
            monkeypatch.setattr(owner, method, _async_blocked(f"{class_name}.{method}"))

    # KIS exposes an injectable client factory, so give it a MockTransport that
    # fails the way an unreachable host does. Patching the factory also flips
    # ``_current_http_client_builder_token``, which invalidates any shared client
    # a previous test cached instead of leaving a real one in place.
    kis_base = importlib.import_module("app.services.brokers.kis.base")
    kis_client_class = getattr(kis_base, "BaseKISClient", None)
    if kis_client_class is not None:

        def _kis_transport(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"{MESSAGE} [{request.url.host}]", request=request)

        def _build_offline_kis_client(self, timeout: float) -> object:
            _ = self
            return httpx.AsyncClient(
                timeout=timeout, transport=httpx.MockTransport(_kis_transport)
            )

        monkeypatch.setattr(
            kis_client_class, "_build_http_client", _build_offline_kis_client
        )

    for module_path, attribute in FINNHUB_CLIENT_FACTORIES:
        module = importlib.import_module(module_path)
        if hasattr(module, attribute):
            monkeypatch.setattr(module, attribute, lambda: _OfflineFinnhubClient())

    return _install_frozen_provider_seam(monkeypatch, _async_blocked)


def _install_frozen_provider_seam(monkeypatch, async_blocked) -> Callable[[], None]:
    """Rebind the recent-trades provider on both routes callers can reach it by.

    ``StockDetailProviders`` is frozen and slotted, the shared default instance is
    bound as a default argument, and callers also build fresh instances that pick
    the field default straight out of the generated ``__init__``. So the module
    attribute is reachable from neither: the live instance is mutated in place
    (restored by the returned undo), and ``__init__.__defaults__`` is rewritten so
    newly constructed instances get the seam too.
    """

    import dataclasses

    module_path, instance_name, field = _FROZEN_PROVIDER_SEAM
    module = importlib.import_module(module_path)
    instance = getattr(module, instance_name, None)
    if instance is None or not hasattr(instance, field):
        return lambda: None

    replacement = async_blocked(f"{instance_name}.{field}")
    owner = type(instance)

    init_defaults = owner.__init__.__defaults__ or ()
    init_fields = [f.name for f in dataclasses.fields(owner) if f.init]
    offset = len(init_fields) - len(init_defaults)
    if field in init_fields:
        index = init_fields.index(field) - offset
        if 0 <= index < len(init_defaults):
            rewritten = list(init_defaults)
            rewritten[index] = replacement
            monkeypatch.setattr(owner.__init__, "__defaults__", tuple(rewritten))

    original = getattr(instance, field)
    object.__setattr__(instance, field, replacement)

    def _undo() -> None:
        object.__setattr__(instance, field, original)

    return _undo
