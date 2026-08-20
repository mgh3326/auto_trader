"""ROB-1296 — the provider seams cover every alias, and only what they should.

``tests/_provider_boundaries.py`` hard-codes which modules bind each seam,
because discovering them per test would mean scanning ``sys.modules`` ~20k times
a run. The trade is that a hard-coded list can go stale: a new ``from x import
y`` alias, or a lazily imported consumer, would silently keep calling the real
provider. ``test_seam_targets_cover_every_binding_module`` is what makes the
trade safe — it imports the whole ``app`` package and recomputes the bindings.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import types
import warnings

import httpx
import pytest

from tests import _provider_boundaries as provider_boundaries

# Modules that legitimately cannot import in a plain test environment, listed
# individually with a reason. Anything else failing is reported, because a
# consumer we could not import is a consumer we could not check.
_KNOWN_OPTIONAL_IMPORTS = {
    # Needs `rob941_funding_sidecar` from the research/ tree, which is not on the
    # path for the app test suite.
    "app.services.rob974_h6b_materializer",
}
_KNOWN_OPTIONAL_IMPORT_PREFIXES = ("app.flows",)  # Prefect is not a dependency


def _import_entire_app() -> list[str]:
    """Materialise every alias, including ones only a lazy import creates.

    Returns the modules that failed to import so the caller can decide whether
    the sweep was complete enough to trust.
    """

    warnings.filterwarnings("ignore")
    import app

    failures: list[str] = []
    for module in pkgutil.walk_packages(app.__path__, prefix="app."):
        try:
            importlib.import_module(module.name)
        except Exception as error:  # noqa: BLE001 - collected, not swallowed
            if module.name in _KNOWN_OPTIONAL_IMPORTS or module.name.startswith(
                _KNOWN_OPTIONAL_IMPORT_PREFIXES
            ):
                continue
            failures.append(f"{module.name}: {type(error).__name__}: {error}")
    return failures


def _all_seam_targets() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Async and sync seams share one alias contract -- both can go stale."""

    return (
        *provider_boundaries.SEAM_TARGETS,
        *provider_boundaries.SYNC_SEAM_TARGETS,
    )


def _undeclared_holders() -> dict[str, list[str]]:
    """Modules holding a seam's function that ``SEAM_TARGETS`` does not declare.

    Two kinds of holder count, and missing either one is a false green:

    * a module still bound to the **original** function — the plain stale-alias
      case, where the seam simply never reached that consumer;
    * a module bound to a **replacement** — a consumer first imported *while* the
      seams were installed, which aliased the stand-in at import time.
      ``monkeypatch`` restores the modules it patched, not that late consumer, so
      the stale replacement survives teardown and an original-identity scan walks
      straight past it.

    Detection is therefore identity-or-marker, which makes it independent of the
    order tests happened to run in.
    """

    undeclared: dict[str, list[str]] = {}
    for attribute, module_paths in _all_seam_targets():
        declared = set(module_paths)
        originals = {
            path: getattr(importlib.import_module(path), attribute, None)
            for path in sorted(declared)
        }
        original = originals[sorted(declared)[0]]

        holders = sorted(
            name
            for name, module in list(sys.modules.items())
            if module is not None
            and name.startswith("app.")
            and (
                getattr(module, attribute, None) is original
                or provider_boundaries.seam_marker(getattr(module, attribute, None))
                == attribute
            )
        )
        extra = [name for name in holders if name not in declared]
        if extra:
            undeclared[attribute] = extra
    return undeclared


def test_declared_seam_modules_share_one_original_identity(
    allow_external_providers,
) -> None:
    """Every declared alias of a seam must be the *same* object.

    If two declared modules held different functions, patching them would only
    look complete: one of them would be a different provider entirely.
    """

    _ = allow_external_providers
    _import_entire_app()

    mismatched: dict[str, dict[str, str]] = {}
    for attribute, module_paths in _all_seam_targets():
        values = {
            path: getattr(importlib.import_module(path), attribute, None)
            for path in module_paths
        }
        first = values[module_paths[0]]
        if any(value is not first for value in values.values()):
            mismatched[attribute] = {
                path: f"{getattr(value, '__module__', '?')}.{getattr(value, '__qualname__', value)}"
                for path, value in values.items()
            }
    assert mismatched == {}


def test_seam_targets_cover_every_binding_module(allow_external_providers) -> None:
    """No module may hold a seam's function while its siblings are seamed.

    A half-patched seam is worse than none: the leak just moves to whichever
    consumer was missed. If this fails, add the reported module to
    ``SEAM_TARGETS`` -- do not widen an allowlist or drop the seam.

    This test **opts out of the provider fixture on purpose** so the declared
    modules hold their originals. See ``_undeclared_holders`` for why detection
    matches on identity *or* marker.
    """

    _ = allow_external_providers
    failures = _import_entire_app()
    assert failures == [], f"could not import app modules to check: {failures}"

    for attribute, module_paths in _all_seam_targets():
        for module_path in module_paths:
            value = getattr(importlib.import_module(module_path), attribute, None)
            assert value is not None
            assert provider_boundaries.seam_marker(value) is None, (
                f"{module_path}.{attribute} is still a seam replacement; this "
                "test must run with the provider fixture opted out"
            )

    missing = _undeclared_holders()
    assert missing == {}, (
        "SEAM_TARGETS is missing consumer aliases; add them to "
        f"tests/_provider_boundaries.py: {missing}"
    )


def test_detector_reports_a_late_consumer_holding_a_stale_replacement(
    allow_external_providers,
) -> None:
    """Negative control for the marker half of the detector.

    Synthesises the exact shape the identity-only scan missed: an ``app.*``
    module that is not declared and holds a *replacement* rather than the
    original, as a module first imported during a seamed test would.
    """

    _ = allow_external_providers
    _import_entire_app()
    assert _undeclared_holders() == {}

    async def _stale_replacement(*_args, **_kwargs):  # pragma: no cover - never run
        raise AssertionError("unreachable")

    _stale_replacement.__dict__[provider_boundaries.SEAM_MARKER] = "fetch_orderbook"

    module_name = "app._rob1296_synthetic_late_consumer"
    synthetic = types.ModuleType(module_name)
    synthetic.fetch_orderbook = _stale_replacement
    sys.modules[module_name] = synthetic
    try:
        assert _undeclared_holders() == {"fetch_orderbook": [module_name]}
    finally:
        del sys.modules[module_name]

    assert _undeclared_holders() == {}


def test_install_stamps_the_seam_marker_on_every_declared_module() -> None:
    """Ties ``install`` to the detector: real replacements must carry the marker.

    Runs *without* the opt-out, so the seams are live. If ``install`` ever stopped
    stamping, ``_undeclared_holders`` would lose its stale-replacement branch
    silently.
    """

    for attribute, module_paths in _all_seam_targets():
        for module_path in module_paths:
            value = getattr(importlib.import_module(module_path), attribute, None)
            assert provider_boundaries.seam_marker(value) == attribute, (
                f"{module_path}.{attribute} is not a marked seam replacement"
            )


def test_declared_seam_modules_all_expose_their_attribute() -> None:
    """Catch a renamed or relocated seam instead of silently patching nothing."""

    stale = [
        f"{module_path}.{attribute}"
        for attribute, module_paths in provider_boundaries.SEAM_TARGETS
        for module_path in module_paths
        if not hasattr(importlib.import_module(module_path), attribute)
    ]
    assert stale == []


@pytest.mark.asyncio
async def test_defining_module_and_consumer_alias_share_the_replacement() -> None:
    """Both routes to a seamed function must be blocked, not just the definition.

    ``fetch_orderbook`` is the concrete case that motivated the alias list:
    ``market_data.service`` imported it at module load, so patching only
    ``upbit_orderbook`` left the live one reachable.
    """

    from app.services import upbit_orderbook
    from app.services.market_data import service as market_data_service

    assert market_data_service.fetch_orderbook is upbit_orderbook.fetch_orderbook

    for candidate in (
        upbit_orderbook.fetch_orderbook,
        market_data_service.fetch_orderbook,
    ):
        with pytest.raises(httpx.ConnectError, match="External provider calls"):
            await candidate("KRW-BTC")


@pytest.mark.asyncio
async def test_seams_raise_the_error_callers_already_handle() -> None:
    """The seam must not change which ``except`` clause a caller lands in."""

    from app.services import exchange_rate_service

    with pytest.raises(httpx.ConnectError) as excinfo:
        await exchange_rate_service._fetch_open_er_api_usd_krw_quote()

    assert isinstance(excinfo.value, httpx.TransportError)
    assert isinstance(excinfo.value, httpx.HTTPError)


@pytest.mark.asyncio
async def test_frozen_provider_default_is_seamed_on_both_routes() -> None:
    """The shared instance *and* freshly built ones must carry the seam.

    ``StockDetailProviders`` is frozen and slotted and its default instance is a
    default argument, so a caller can reach the real provider either through the
    module-level singleton or by constructing its own.
    """

    from app.services.invest_view_model import stock_detail_service

    fresh = stock_detail_service.StockDetailProviders()
    for providers in (
        stock_detail_service.DEFAULT_STOCK_DETAIL_PROVIDERS,
        fresh,
    ):
        with pytest.raises(httpx.ConnectError, match="External provider calls"):
            await providers.recent_trades("KRW-BTC", "crypto")


@pytest.mark.asyncio
async def test_finnhub_client_stub_fails_like_an_unreachable_host() -> None:
    import requests

    from app.services import finnhub_news

    client = finnhub_news._get_finnhub_client()
    with pytest.raises(requests.ConnectionError, match="External provider calls"):
        client.company_news("AAPL", _from="2026-01-01", to="2026-01-02")


@pytest.mark.asyncio
async def test_kis_client_factory_yields_an_offline_transport() -> None:
    from app.services.brokers.kis.base import BaseKISClient

    client = BaseKISClient._build_http_client(object(), 1.0)
    try:
        with pytest.raises(httpx.ConnectError, match="External provider calls"):
            await client.get("https://openapi.koreainvestment.com/probe")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_opt_out_restores_the_real_seam(
    allow_external_providers,
) -> None:
    """With the opt-out the real function is reachable again.

    Reaching it is still safe: the transport backstop and the socket guard both
    remain in force, so this asserts identity rather than performing a request.
    """

    _ = allow_external_providers
    from app.services import upbit_orderbook

    assert upbit_orderbook.fetch_orderbook.__module__ == "app.services.upbit_orderbook"


def test_opt_out_still_cannot_reach_the_network(allow_external_providers) -> None:
    _ = allow_external_providers
    from tests import _socket_guard as socket_guard

    assert socket_guard.is_socket_address_permitted(("203.0.113.1", 443)) is False
