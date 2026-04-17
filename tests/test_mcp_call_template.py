import json
import socket
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

TEMPLATE_PATH = Path("scripts/templates/mcp_call.sh.tmpl")


def test_mcp_call_template_disables_sse_buffering_and_has_timeout() -> None:
    template = TEMPLATE_PATH.read_text()

    assert "curl -fsS -N --max-time 15 -X POST" in template
    assert '-H "Connection: close"' in template
    assert "SSE" in template
    assert "SIGPIPE" in template


def test_mcp_call_template_exits_after_first_sse_data_line(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_length = int(self.headers.get("content-length", "0"))
            if content_length:
                self.rfile.read(content_length)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'data: {"jsonrpc":"2.0","id":100,"result":{"content":[]}}\n\n'
            )
            self.wfile.flush()
            time.sleep(30)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        template = TEMPLATE_PATH.read_text()
        endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
        rendered = (
            template.replace("${MCP_ENDPOINT}", endpoint)
            .replace("${MCP_AUTH_TOKEN}", "dummy-token")
            .replace("${MCP_SESSION_ID}", "dummy-session")
            .replace("${PAPERCLIP_AGENT_ID}", "dummy-agent")
        )
        helper = tmp_path / "mcp_call.sh"
        helper.write_text(rendered)
        helper.chmod(0o700)

        start = time.monotonic()
        result = subprocess.run(
            [
                "bash",
                str(helper),
                "get_sector_peers",
                json.dumps({"symbol": "HSY", "market": "us"}),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        elapsed = time.monotonic() - start
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "jsonrpc": "2.0",
        "id": 100,
        "result": {"content": []},
    }
    assert elapsed < 5


def test_mcp_call_template_fast_fail_does_not_use_unbound_coproc_pid(
    tmp_path: Path,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]

    template = TEMPLATE_PATH.read_text()
    rendered = (
        template.replace("${MCP_ENDPOINT}", f"http://127.0.0.1:{closed_port}/mcp")
        .replace("${MCP_AUTH_TOKEN}", "dummy-token")
        .replace("${MCP_SESSION_ID}", "dummy-session")
        .replace("${PAPERCLIP_AGENT_ID}", "dummy-agent")
    )
    helper = tmp_path / "mcp_call.sh"
    helper.write_text(rendered)
    helper.chmod(0o700)

    start = time.monotonic()
    result = subprocess.run(
        [
            "bash",
            str(helper),
            "get_sector_peers",
            json.dumps({"symbol": "HSY", "market": "us"}),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    elapsed = time.monotonic() - start

    assert result.returncode != 0
    assert elapsed < 5
    assert "CURL_STREAM_PID: unbound variable" not in result.stderr
