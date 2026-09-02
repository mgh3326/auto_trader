"""The NCP websocket units run websocket_monitor.py from the API image.

The first NCP deployment of at-upbit-ws/at-kis-ws crash-looped with
"can't open file '/app/websocket_monitor.py'" because Dockerfile.api only
copied app/, alembic/ and research_contracts/. Pin the COPY so the image keeps
shipping the root-level monitor script.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_api_copies_websocket_monitor() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile.api").read_text()
    assert "COPY websocket_monitor.py ./" in dockerfile
    assert (REPO_ROOT / "websocket_monitor.py").is_file()
