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

    def _async_blocked(label: str):
        async def _blocked(*_args, **_kwargs):
            raise httpx.ConnectError(f"{MESSAGE} [{label}]")

        return _blocked

    for attribute, module_paths in SEAM_TARGETS:
        replacement = _async_blocked(attribute)
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
