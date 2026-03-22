"""Screenshot capture utility backed by the configured Playwright MCP server.

This module captures real chart screenshots through `mcporter` by driving the
configured `playwright` MCP server. The MCP surface is page-oriented rather than
instance-oriented, so this adapter manages a single logical session and always
persists screenshots to disk before reading them back.

Repro commands for the mcporter CLI contract:
    mcporter call --help
    mcporter list
    mcporter list playwright --schema
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class ScreenshotCapture:
    """Capture chart screenshots through the Playwright MCP server."""

    DEFAULT_SERVER = "playwright"
    DEFAULT_VIEWPORT_WIDTH = 1400
    DEFAULT_VIEWPORT_HEIGHT = 900
    DEFAULT_WAIT_SECONDS = 5
    PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

    def __init__(
        self,
        output_dir: Path | None = None,
        timeout: int = 30,
        server_name: str | None = None,
    ) -> None:
        self.output_dir = output_dir or Path(__file__).parent.parent / "images"
        self.output_dir.mkdir(exist_ok=True)
        self.timeout = timeout
        self._server_name = server_name or self.DEFAULT_SERVER
        self._browser_ready = False

    def _mcporter_call(self, tool: str, params: dict[str, Any] | None = None) -> str:
        """Call mcporter and return the raw stdout payload."""
        cmd = ["mcporter", "call", tool, "--output", "raw"]
        if params:
            cmd.extend(["--args", json.dumps(params)])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or "Unknown error"
            if "unknown server" in error_msg.lower():
                raise RuntimeError(
                    f"MCP server unavailable for selector '{tool}': {error_msg}"
                )
            raise RuntimeError(f"mcporter call failed for '{tool}': {error_msg}")

        output = result.stdout.strip()
        if "### Error" in output or "isError: true" in output:
            raise RuntimeError(f"mcporter call reported error for '{tool}': {output}")

        return output

    def _ensure_browser(self) -> None:
        """Mark the page-backed session as ready once."""
        if not self._browser_ready:
            self._browser_ready = True

    def _resize_browser(self, width: int, height: int) -> None:
        """Resize the active Playwright viewport."""
        self._ensure_browser()
        self._mcporter_call(
            f"{self._server_name}.browser_resize",
            {"width": width, "height": height},
        )

    def _navigate(self, url: str, wait_for: str | None = None) -> None:
        """Navigate to a chart URL and optionally wait for text."""
        self._ensure_browser()
        self._mcporter_call(f"{self._server_name}.browser_navigate", {"url": url})
        if wait_for:
            self._mcporter_call(
                f"{self._server_name}.browser_wait_for",
                {"text": wait_for},
            )

    def _wait_for_chart_ready(
        self,
        selector: str | None = None,
        timeout: int = DEFAULT_WAIT_SECONDS,
    ) -> None:
        """Wait for the chart page to stabilize.

        The Playwright MCP surface available through mcporter supports waiting by
        text or elapsed time, but not CSS selectors. For embedded third-party
        chart pages we therefore use a bounded time wait.
        """
        self._ensure_browser()
        if selector:
            self._mcporter_call(
                f"{self._server_name}.browser_wait_for",
                {"text": selector},
            )
            return

        wait_seconds = max(1, min(timeout, self.DEFAULT_WAIT_SECONDS))
        self._mcporter_call(
            f"{self._server_name}.browser_wait_for",
            {"time": wait_seconds},
        )

    def _take_screenshot(self, output_path: Path, full_page: bool = False) -> bytes:
        """Save a screenshot to disk and return its PNG bytes."""
        self._ensure_browser()
        self._mcporter_call(
            f"{self._server_name}.browser_take_screenshot",
            {
                "type": "png",
                "filename": str(output_path),
                "fullPage": full_page,
            },
        )

        png_data = output_path.read_bytes()
        if not png_data.startswith(self.PNG_SIGNATURE):
            raise RuntimeError(f"Screenshot output is not a valid PNG: {output_path}")

        return png_data

    def capture_tradingview(
        self,
        symbol: str,
        interval: str = "D",
        theme: str = "dark",
        width: int = 800,
        height: int = 400,
    ) -> Path:
        """Capture a TradingView widget screenshot."""
        url = (
            f"https://www.tradingview.com/widgetembed/?symbol={symbol}"
            f"&interval={interval}&theme={theme}"
            f"&width={width}&height={height}"
        )
        output_path = (
            self.output_dir / f"screenshot_{symbol.replace(':', '_')}_{interval}.png"
        )

        try:
            self._navigate(url)
            self._resize_browser(
                self.DEFAULT_VIEWPORT_WIDTH,
                self.DEFAULT_VIEWPORT_HEIGHT,
            )
            self._wait_for_chart_ready()
            self._take_screenshot(output_path)
            return output_path
        except Exception:
            if output_path.exists() and output_path.stat().st_size == 0:
                output_path.unlink()
            raise

    def capture_upbit_chart(
        self,
        market: str = "KRW-BTC",
        interval: str = "240",
        theme: str = "dark",
    ) -> Path:
        """Capture an Upbit chart screenshot."""
        del theme  # Current Playwright capture path does not theme Upbit pages directly.

        url = f"https://upbit.com/exchange?code=CRIX.UPBIT.{market}"
        output_path = (
            self.output_dir
            / f"screenshot_upbit_{market.replace('-', '_')}_{interval}.png"
        )

        try:
            self._navigate(url)
            self._resize_browser(
                self.DEFAULT_VIEWPORT_WIDTH,
                self.DEFAULT_VIEWPORT_HEIGHT,
            )
            self._wait_for_chart_ready()
            self._take_screenshot(output_path)
            return output_path
        except Exception:
            if output_path.exists() and output_path.stat().st_size == 0:
                output_path.unlink()
            raise

    def close(self) -> None:
        """Close the active Playwright page when one was prepared."""
        if not self._browser_ready:
            return

        try:
            self._mcporter_call(f"{self._server_name}.browser_close")
        except Exception:
            pass
        finally:
            self._browser_ready = False

    def __enter__(self) -> ScreenshotCapture:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
