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
) -> tuple[ModuleType, _FakeFastMCP, MagicMock]:
    main_path = Path(__file__).resolve().parents[1] / "app" / "mcp_server" / "main.py"

    fake_fastmcp = ModuleType("fastmcp")
    fake_fastmcp.__dict__["FastMCP"] = _FakeFastMCP

    fake_mcp_package = ModuleType("app.mcp_server")
    fake_mcp_package.__path__ = []

    fake_config = ModuleType("app.core.config")
    fake_config.__dict__["settings"] = SimpleNamespace(LOG_LEVEL="INFO")

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
    monkeypatch.delitem(sys.modules, "app.mcp_server.main", raising=False)

    spec = importlib.util.spec_from_file_location("app.mcp_server.main", main_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.mcp_server.main"] = module
    spec.loader.exec_module(module)
    return module, module.mcp, fake_monitoring.capture_exception


@pytest.mark.unit
class TestMcpServerMain:
    def test_registers_caller_identity_middleware_after_sentry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, mcp, _ = _load_main_module(monkeypatch)

        assert [call.args[0] for call in mcp.add_middleware.call_args_list] == [
            "middleware",
            "caller-identity-middleware",
        ]

    def test_streamable_http_uses_default_shutdown_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_TYPE", "streamable-http")
        monkeypatch.delenv("MCP_GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)

        module, mcp, _ = _load_main_module(monkeypatch)

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

        module, mcp, _ = _load_main_module(monkeypatch)

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

        module, mcp, _ = _load_main_module(monkeypatch)

        module.main()

        mcp.run.assert_called_once_with(transport="stdio")

    def test_stdio_does_not_parse_invalid_shutdown_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_TYPE", "stdio")
        monkeypatch.setenv("MCP_GRACEFUL_SHUTDOWN_TIMEOUT", "invalid")

        module, _, _ = _load_main_module(monkeypatch)

        module.main()

    def test_unsupported_mcp_type_still_raises_and_captures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_TYPE", "invalid")

        module, _, capture_exception = _load_main_module(monkeypatch)

        with pytest.raises(ValueError, match="Unsupported MCP_TYPE: invalid"):
            module.main()

        capture_exception.assert_called_once()
