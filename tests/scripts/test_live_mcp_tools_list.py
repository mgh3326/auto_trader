"""Task 975: the read-only tools/list smoke, against a local fake MCP server."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts import live_mcp_tools_list as smoke

TOKENS = {
    "MCP_LIVE_KR_AUTH_TOKEN": "fake-kr-token-not-a-secret",
    "MCP_LIVE_US_AUTH_TOKEN": "fake-us-token-not-a-secret",
    "MCP_LIVE_CRYPTO_AUTH_TOKEN": "fake-crypto-token-not-a-secret",
}


class FakeMcp:
    def __init__(
        self, token: str, tools: list[str], *, sse: bool, page: int = 0
    ) -> None:
        self.token, self.tools, self.sse, self.page = token, tools, sse, page
        self.methods: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.methods.append(body["method"])
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self.send_response(401)
                    self.end_headers()
                    return
                if body["method"] == "initialize":
                    result: dict = {"protocolVersion": smoke.PROTOCOL_VERSION}
                elif body["method"] == "tools/list":
                    assert self.headers.get("Mcp-Session-Id") == "sess-1"
                    start = int((body.get("params") or {}).get("cursor") or 0)
                    end = start + fake.page if fake.page else len(fake.tools)
                    result = {"tools": [{"name": n} for n in fake.tools[start:end]]}
                    if end < len(fake.tools):
                        result["nextCursor"] = str(end)
                else:
                    self.send_response(202)
                    self.send_header("Mcp-Session-Id", "sess-1")
                    self.end_headers()
                    return
                payload = json.dumps(
                    {"jsonrpc": "2.0", "id": body["id"], "result": result}
                )
                self.send_response(200)
                self.send_header("Mcp-Session-Id", "sess-1")
                if fake.sse:
                    data = f"event: message\ndata: {payload}\n\n".encode()
                    self.send_header("Content-Type", "text/event-stream")
                else:
                    data = payload.encode()
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, FakeMcp]]:
    for name, value in TOKENS.items():
        monkeypatch.setenv(name, value)
    made: dict[str, FakeMcp] = {}
    yield made
    for server in made.values():
        server.close()


def _start(
    made: dict[str, FakeMcp], profile: str, tools: list[str], **kw: object
) -> FakeMcp:
    unit = next(u for u in smoke.UNITS if u.profile == profile)
    made[profile] = FakeMcp(TOKENS[unit.token_env], tools, **kw)  # type: ignore[arg-type]
    return made[profile]


def _args(made: dict[str, FakeMcp]) -> list[str]:
    args: list[str] = []
    for profile, server in made.items():
        args += ["--endpoint", f"{profile}={server.url}", "--only", profile]
    return args


def test_units_match_the_deploy_ports_and_token_names() -> None:
    assert [(u.profile, u.port, u.token_env) for u in smoke.UNITS] == [
        ("live-kr", 8773, "MCP_LIVE_KR_AUTH_TOKEN"),
        ("live-us", 8774, "MCP_LIVE_US_AUTH_TOKEN"),
        ("live-crypto", 8775, "MCP_LIVE_CRYPTO_AUTH_TOKEN"),
    ]
    assert smoke.TAILNET_HOST == "100.122.100.56"


@pytest.mark.parametrize("sse", [True, False], ids=["sse", "json"])
def test_every_profile_matching_its_manifest_passes_and_is_read_only(
    servers: dict[str, FakeMcp], capsys: pytest.CaptureFixture[str], sse: bool
) -> None:
    for unit in smoke.UNITS:
        selected, _ = smoke.manifest_selection(unit.profile)
        _start(servers, unit.profile, sorted(selected), sse=sse, page=7)
    assert smoke.main(_args(servers)) == 0
    out = capsys.readouterr()
    for unit in smoke.UNITS:
        assert f"{unit.profile}\t{servers[unit.profile].url}\tMATCH\t" in out.out
        methods = servers[unit.profile].methods
        assert set(methods) == {"initialize", "notifications/initialized", "tools/list"}
        assert methods[:2] == ["initialize", "notifications/initialized"]
    for token in TOKENS.values():
        assert token not in out.out + out.err


def test_gates_off_surface_is_reported_as_such(
    servers: dict[str, FakeMcp], capsys: pytest.CaptureFixture[str]
) -> None:
    selected, gated = smoke.manifest_selection("live-kr")
    assert gated
    _start(servers, "live-kr", sorted(selected - gated), sse=True)
    assert smoke.main(_args(servers)) == 0
    assert "\tMATCH_GATES_OFF\t" in capsys.readouterr().out


def test_default_profile_surface_is_a_mismatch(
    servers: dict[str, FakeMcp], capsys: pytest.CaptureFixture[str]
) -> None:
    selected, _ = smoke.manifest_selection("live-us")
    _start(
        servers,
        "live-us",
        sorted(selected | {"place_order", "kis_live_place_order"}),
        sse=True,
    )
    assert smoke.main(_args(servers)) == 1
    out = capsys.readouterr().out
    assert "\tMISMATCH\t" in out
    assert (
        "extra (served, not in manifest): ['kis_live_place_order', 'place_order']"
        in out
    )


def test_other_markets_profile_is_a_mismatch(
    servers: dict[str, FakeMcp], capsys: pytest.CaptureFixture[str]
) -> None:
    crypto, _ = smoke.manifest_selection("live-crypto")
    _start(servers, "live-kr", sorted(crypto), sse=True)
    assert smoke.main(_args(servers)) == 1
    assert "\tMISMATCH\t" in capsys.readouterr().out


def test_rejected_token_is_an_error_without_the_token(
    servers: dict[str, FakeMcp],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected, _ = smoke.manifest_selection("live-kr")
    _start(servers, "live-kr", sorted(selected), sse=True)
    monkeypatch.setenv("MCP_LIVE_KR_AUTH_TOKEN", "wrong-token-value-xyz")
    assert smoke.main(_args(servers)) == 1
    out = capsys.readouterr()
    assert "ERROR\tinitialize: HTTP 401" in out.out
    assert "wrong-token-value-xyz" not in out.out + out.err
    assert servers["live-kr"].methods == ["initialize"]


def test_missing_token_env_is_an_error_and_sends_nothing(
    servers: dict[str, FakeMcp],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _start(servers, "live-us", [], sse=True)
    monkeypatch.delenv("MCP_LIVE_US_AUTH_TOKEN")
    assert smoke.main(_args(servers)) == 1
    assert "MCP_LIVE_US_AUTH_TOKEN is not set" in capsys.readouterr().out
    assert servers["live-us"].methods == []


def test_unreachable_endpoint_is_an_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_LIVE_CRYPTO_AUTH_TOKEN", "t")
    code = smoke.main(
        ["--only", "live-crypto", "--endpoint", "live-crypto=http://127.0.0.1:9/mcp"]
    )
    assert code == 1
    assert "ERROR\tinitialize:" in capsys.readouterr().out


def test_default_endpoints_are_the_tailnet_frontends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_list(url: str, token: str) -> list[str]:
        seen.append(url)
        raise smoke.SmokeError("stop")

    for name, value in TOKENS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(smoke, "list_tools", fake_list)
    assert smoke.main([]) == 1
    assert seen == [
        "http://100.122.100.56:8773/mcp",
        "http://100.122.100.56:8774/mcp",
        "http://100.122.100.56:8775/mcp",
    ]
