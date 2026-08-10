"""Credential/host view that binds a KIS client to the **mock (VTS)** account.

Why this is a module of its own, rather than a local class in the one place
that needs it
--------------------------------------------------------------------------
``BaseKISClient`` reads its host, credentials and token from
``self._settings`` under the *live* field names (``kis_app_key``,
``kis_base_url``, ``kis_account_no``, ``kis_access_token``). A subclass that
wants to talk to the KIS mock (VTS) account must therefore override
``_settings`` with a view that maps those names onto the ``kis_mock_*``
fields. Two such views already exist —
``app.services.brokers.kis.client._KISSettingsView`` (used by
``KISClient(is_mock=True)``) and
``app.services.invest_home_readers._KISMockSettingsProxy`` — and a third
consumer, the B0-X KR lane, cannot reuse either:

* ``app.services.brokers.kis.client`` is on that lane's AST denylist
  (``tests/scripts/b0x/kr/test_no_live_kis_order_imports.py``) because it
  imports the order clients at module scope, and
* the lane's own AST guard (``tests/scripts/b0x/kr/
  test_submission_ast_guard.py``, prohibition 3) rejects ``getattr()`` with a
  non-literal attribute name — which is exactly how the delegating
  ``__getattr__`` in this idiom is written. The guard is doing its job; the
  idiom simply cannot live inside ``scripts/b0x/kr/**``.

So the view lives here instead. This module is deliberately placed *outside*
``app.services.brokers.kis.`` — it holds no order surface, only a credential
mapping — which is also why the lane may import it without the order-surface
allowlist being widened.

What this class does **not** do: fall back to live credentials. Every mapped
property returns the mock field and nothing else. An unset mock credential
surfaces as an empty value that the caller's own configuration gate rejects;
it never silently degrades into a live-account call. That failure mode is not
hypothetical — the bug this module was written to fix was a KIS mock lane
whose client inherited ``BaseKISClient``'s live defaults and sent a mock
``tr_id`` to the live host.
"""

from __future__ import annotations

from typing import Any

__all__ = ["KISMockSettingsView"]


class KISMockSettingsView:
    """Expose the ``kis_mock_*`` settings under the live KIS field names.

    Anything not explicitly mapped below is delegated unchanged to the wrapped
    settings object (rate-limit tuning, timeouts, and similar account-neutral
    configuration).
    """

    def __init__(self, real_settings: Any) -> None:
        self._real = real_settings

    # -- account-bound fields: mock only, never a live fallback -------------

    @property
    def kis_app_key(self) -> str:
        return str(self._real.kis_mock_app_key or "")

    @property
    def kis_app_secret(self) -> str:
        return str(self._real.kis_mock_app_secret or "")

    @property
    def kis_account_no(self) -> str | None:
        return self._real.kis_mock_account_no

    @property
    def kis_base_url(self) -> str:
        return str(self._real.kis_mock_base_url or "")

    @property
    def kis_access_token(self) -> str | None:
        return self._real.kis_mock_access_token

    @kis_access_token.setter
    def kis_access_token(self, value: str | None) -> None:
        self._real.kis_mock_access_token = value

    # -- everything else is account-neutral --------------------------------

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)
