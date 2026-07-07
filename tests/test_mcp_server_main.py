import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


class _FakeFastMCP:
    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs
        self.run = MagicMock()
        self.add_middleware = MagicMock()


def _load_env_utils_module() -> ModuleType:
    env_utils_path = (
        Path(__file__).resolve().parents[1] / "app" / "mcp_server" / "env_utils.py"
    )
    spec = importlib.util.spec_from_file_location(
        "app.mcp_server.env_utils", env_utils_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_main_module(
    monkeypatch: pytest.MonkeyPatch,
    *,
    account_read: bool = False,
) -> tuple[ModuleType, _FakeFastMCP, MagicMock, object]:
    main_path = Path(__file__).resolve().parents[1] / "app" / "mcp_server" / "main.py"

    fake_fastmcp = ModuleType("fastmcp")
    fake_fastmcp.__dict__["FastMCP"] = _FakeFastMCP

    fake_mcp_package = ModuleType("app.mcp_server")
    fake_mcp_package.__path__ = []

    fake_config = ModuleType("app.core.config")
    fake_config.__dict__["settings"] = SimpleNamespace(
        LOG_LEVEL="INFO",
        mcp_caller_agent_id_fallback=None,
    )

    fake_auth = ModuleType("app.mcp_server.auth")
    fake_auth.__dict__["build_auth_provider"] = MagicMock(return_value="auth-provider")

    fake_sentry_middleware = ModuleType("app.mcp_server.sentry_middleware")
    fake_sentry_middleware.__dict__["McpToolCallSentryMiddleware"] = MagicMock(
        return_value="middleware"
    )

    fake_caller_identity_middleware = ModuleType(
        "app.mcp_server.caller_identity_middleware"
    )
    fake_caller_identity_middleware.__dict__["CallerIdentityMiddleware"] = MagicMock(
        return_value="caller-identity-middleware"
    )

    fake_tooling = ModuleType("app.mcp_server.tooling")
    register_all_tools = MagicMock()
    fake_tooling.__dict__["register_all_tools"] = register_all_tools

    fake_monitoring = ModuleType("app.monitoring.sentry")
    fake_monitoring.__dict__["capture_exception"] = MagicMock()
    fake_monitoring.__dict__["init_sentry"] = MagicMock()

    account_read_profile = object()
    resolved_profile = account_read_profile if account_read else "profile"
    fake_profiles = ModuleType("app.mcp_server.profiles")
    fake_profiles.__dict__["McpProfile"] = SimpleNamespace(
        ACCOUNT_READ=account_read_profile
    )
    fake_profiles.__dict__["resolve_mcp_profile"] = MagicMock(
        return_value=resolved_profile
    )

    # ROB-469: main.py now imports the lifecycle module (unauth /health route +
    # startup/shutdown lifespan logging). Stub it like the other dependencies so
    # main()'s transport/shutdown logic is tested in isolation.
    fake_lifecycle = ModuleType("app.mcp_server.lifecycle")
    fake_lifecycle.__dict__["build_server_lifespan"] = MagicMock(
        return_value="server-lifespan"
    )
    fake_lifecycle.__dict__["register_health_route"] = MagicMock()

    # ROB-469 PR2: main.py imports ToolTimeoutMiddleware. The fake
    # app.mcp_server package has __path__ = [], so without this stub the
    # import fails in isolation (it previously relied on sys.modules caching
    # from an earlier test file). Use the real class so the middleware-ordering
    # assertion (type(calls[2]).__name__ == "ToolTimeoutMiddleware") still holds.
    from app.mcp_server.timeout_middleware import ToolTimeoutMiddleware as _RealTTM

    fake_timeout_middleware = ModuleType("app.mcp_server.timeout_middleware")
    fake_timeout_middleware.__dict__["ToolTimeoutMiddleware"] = _RealTTM

    env_utils_module = _load_env_utils_module()

    monkeypatch.setitem(sys.modules, "fastmcp", fake_fastmcp)
    monkeypatch.setitem(sys.modules, "app.mcp_server", fake_mcp_package)
    monkeypatch.setitem(sys.modules, "app.core.config", fake_config)
    monkeypatch.setitem(sys.modules, "app.mcp_server.auth", fake_auth)
    monkeypatch.setitem(sys.modules, "app.mcp_server.env_utils", env_utils_module)
    monkeypatch.setitem(
        sys.modules, "app.mcp_server.sentry_middleware", fake_sentry_middleware
    )
    monkeypatch.setitem(
        sys.modules,
        "app.mcp_server.caller_identity_middleware",
        fake_caller_identity_middleware,
    )
    monkeypatch.setitem(sys.modules, "app.mcp_server.tooling", fake_tooling)
    monkeypatch.setitem(sys.modules, "app.monitoring.sentry", fake_monitoring)
    monkeypatch.setitem(sys.modules, "app.mcp_server.profiles", fake_profiles)
    monkeypatch.setitem(sys.modules, "app.mcp_server.lifecycle", fake_lifecycle)
    monkeypatch.setitem(
        sys.modules, "app.mcp_server.timeout_middleware", fake_timeout_middleware
    )

    spec = importlib.util.spec_from_file_location("app.mcp_server.main", main_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Use monkeypatch.setitem (NOT a raw `sys.modules[...] =`) so the fake module is
    # restored/removed on teardown. A raw assignment paired with a no-op
    # delitem(raising=False) leaked this fake `main` into sys.modules for the whole
    # session, poisoning other tests that import the real app.mcp_server.main.
    monkeypatch.setitem(sys.modules, "app.mcp_server.main", module)
    spec.loader.exec_module(module)
    return module, module.mcp, fake_monitoring.capture_exception, account_read_profile


@pytest.mark.unit
class TestMcpServerMain:
    def test_registers_caller_identity_middleware_after_sentry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, mcp, _, _ = _load_main_module(monkeypatch)

        calls = [call.args[0] for call in mcp.add_middleware.call_args_list]
        # Sentry (outermost) then CallerIdentity, then ROB-469 PR2's
        # ToolTimeoutMiddleware added LAST so it is innermost (wraps the tool) while
        # Sentry stays outermost and captures the timeout ToolError.
        assert calls[:2] == ["middleware", "caller-identity-middleware"]
        assert type(calls[2]).__name__ == "ToolTimeoutMiddleware"
        assert len(calls) == 3

    def test_non_integer_log_level_falls_back_to_info(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module, mcp, _, _ = _load_main_module(monkeypatch)
        module.settings.LOG_LEVEL = "BASIC_FORMAT"

        module.main()

        mcp.run.assert_called_once_with(
            transport="streamable-http",
            host="0.0.0.0",
            port=8765,
            path="/mcp",
            uvicorn_config={"timeout_graceful_shutdown": 10},
        )

    def test_streamable_http_uses_default_shutdown_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_TYPE", "streamable-http")
        monkeypatch.delenv("MCP_GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)

        module, mcp, _, _ = _load_main_module(monkeypatch)

        module.main()

        mcp.run.assert_called_once_with(
            transport="streamable-http",
            host="0.0.0.0",
            port=8765,
            path="/mcp",
            uvicorn_config={"timeout_graceful_shutdown": 10},
        )

    def test_sse_honors_explicit_shutdown_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_TYPE", "sse")
        monkeypatch.setenv("MCP_GRACEFUL_SHUTDOWN_TIMEOUT", "27")

        module, mcp, _, _ = _load_main_module(monkeypatch)

        module.main()

        mcp.run.assert_called_once_with(
            transport="sse",
            host="0.0.0.0",
            port=8765,
            path="/mcp",
            uvicorn_config={"timeout_graceful_shutdown": 27},
        )

    def test_stdio_does_not_pass_uvicorn_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_TYPE", "stdio")
        monkeypatch.setenv("MCP_GRACEFUL_SHUTDOWN_TIMEOUT", "33")

        module, mcp, _, _ = _load_main_module(monkeypatch)

        module.main()

        mcp.run.assert_called_once_with(transport="stdio")

    def test_stdio_does_not_parse_invalid_shutdown_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_TYPE", "stdio")
        monkeypatch.setenv("MCP_GRACEFUL_SHUTDOWN_TIMEOUT", "invalid")

        module, _, _, _ = _load_main_module(monkeypatch)

        module.main()

    def test_unsupported_mcp_type_still_raises_and_captures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_TYPE", "invalid")

        module, _, capture_exception, _ = _load_main_module(monkeypatch)

        with pytest.raises(ValueError, match="Unsupported MCP_TYPE: invalid"):
            module.main()

        capture_exception.assert_called_once()

    @pytest.mark.parametrize("transport", ["streamable-http", "sse"])
    def test_refuses_to_boot_when_env_fallback_set_on_http_transport(
        self, monkeypatch: pytest.MonkeyPatch, transport: str
    ) -> None:
        monkeypatch.setenv("MCP_TYPE", transport)

        module, mcp, capture_exception, _ = _load_main_module(monkeypatch)
        module.settings.mcp_caller_agent_id_fallback = "trader-agent-id"

        with pytest.raises(
            RuntimeError,
            match=(
                "MCP_CALLER_AGENT_ID is only allowed for stdio/local dev transports"
            ),
        ):
            module.main()

        mcp.run.assert_not_called()
        capture_exception.assert_called_once()

    def test_boot_ok_when_env_fallback_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_TYPE", "sse")

        module, mcp, capture_exception, _ = _load_main_module(monkeypatch)
        module.settings.mcp_caller_agent_id_fallback = None

        module.main()

        mcp.run.assert_called_once_with(
            transport="sse",
            host="0.0.0.0",
            port=8765,
            path="/mcp",
            uvicorn_config={"timeout_graceful_shutdown": 10},
        )
        capture_exception.assert_not_called()

    def test_boot_ok_when_stdio_and_env_fallback_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_TYPE", "stdio")

        module, mcp, capture_exception, _ = _load_main_module(monkeypatch)
        module.settings.mcp_caller_agent_id_fallback = "trader-agent-id"

        module.main()

        mcp.run.assert_called_once_with(transport="stdio")
        capture_exception.assert_not_called()

    def test_account_read_profile_requires_auth_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)

        with pytest.raises(
            RuntimeError,
            match="MCP_PROFILE=account_read requires non-empty MCP_AUTH_TOKEN",
        ):
            _load_main_module(monkeypatch, account_read=True)

    def test_account_read_profile_accepts_auth_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_AUTH_TOKEN", "account-read-token")
        module, _, _, _ = _load_main_module(monkeypatch, account_read=True)

        module.main()
