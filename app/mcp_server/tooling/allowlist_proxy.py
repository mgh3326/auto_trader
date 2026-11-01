"""Registration proxy that physically registers only the allowed tool names."""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any, TypeVar, cast

_F = TypeVar("_F", bound=Callable[..., Any])


class AllowlistedMCP:
    """Run existing group registrars while dropping every unlisted name."""

    def __init__(self, inner: Any, allowed_names: Collection[str]) -> None:
        self._inner = inner
        self._allowed_names = allowed_names

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[_F], _F]:
        name = kwargs.get("name")
        if name is None and args:
            name = args[0]
        if str(name) in self._allowed_names:
            return cast(Callable[[_F], _F], self._inner.tool(*args, **kwargs))

        def decorator(func: _F) -> _F:
            return func

        return decorator

    def list_tools(self) -> Any:
        lister = getattr(self._inner, "list_tools", None)
        return [] if lister is None else lister()


__all__ = ["AllowlistedMCP"]
