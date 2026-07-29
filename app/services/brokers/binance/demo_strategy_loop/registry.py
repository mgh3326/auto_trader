"""ROB-1145 — strategy plugin registry for the Binance Demo strategy loop.

Before this module the plugin was hardcoded at the CLI call site
(``strategy=NullStrategy()`` in ``scripts/binance_demo_strategy_loop.py``),
so swapping a plugin meant editing code. The registry turns that into a
``--strategy <key>`` selector.

Deliberate properties:

* **``NullStrategy`` stays the default.** ``DEFAULT_STRATEGY_KEY`` is
  ``"null"``; a caller that passes nothing keeps the ROB-993 behaviour of
  never emitting a signal. The selector adds a way to opt IN to a
  signal-emitting plugin, it never changes the default posture.
* **Fail closed on an unknown key.** ``build_strategy`` raises
  ``UnknownStrategyKey`` rather than falling back to some plugin — a typo
  must stop the run, not silently trade a different strategy.
* **No safety dial.** A plugin only proposes ``(symbol, side)``. Leg
  notional ``[6, 10]`` USDT, the one-concurrent-position and
  two-consecutive-stop-loss caps, 1x leverage, the reduceOnly close and
  the ``demo-fapi.binance.com`` host allowlist are all enforced
  downstream in ``sizing``/``kill_switch``/``execution`` and are not
  reachable from here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .strategy import LastBarDirectionStrategy, NullStrategy, StrategyPlugin

DEFAULT_STRATEGY_KEY = "null"

_FACTORIES: Mapping[str, Callable[[], StrategyPlugin]] = {
    "null": NullStrategy,
    "last-bar-direction": LastBarDirectionStrategy,
}


class UnknownStrategyKey(ValueError):
    """Raised for a ``--strategy`` key that is not registered."""


def available_strategy_keys() -> tuple[str, ...]:
    """Registered keys, default first then the rest sorted (stable for
    ``--help`` output and for argparse ``choices``)."""
    rest = sorted(key for key in _FACTORIES if key != DEFAULT_STRATEGY_KEY)
    return (DEFAULT_STRATEGY_KEY, *rest)


def build_strategy(key: str | None = None) -> StrategyPlugin:
    """Instantiate the registered plugin for ``key``.

    ``None`` resolves to :data:`DEFAULT_STRATEGY_KEY` (``NullStrategy``).
    An unregistered key raises :class:`UnknownStrategyKey` — never a
    silent fallback.
    """
    resolved = DEFAULT_STRATEGY_KEY if key is None else key.strip().lower()
    try:
        factory = _FACTORIES[resolved]
    except KeyError as exc:
        raise UnknownStrategyKey(
            f"unknown strategy {key!r} — registered keys: "
            f"{', '.join(available_strategy_keys())}"
        ) from exc
    return factory()
