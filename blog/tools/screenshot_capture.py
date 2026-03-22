"""Screenshot capture utility using stealth_browser MCP service.

This module provides ScreenshotCapture class for capturing chart screenshots
via an external stealth_browser service accessed through mcporter.

Repro commands for mcporter CLI contract:
    mcporter call --help
    mcporter list
    mcporter list <server-name> --schema

Note: mcporter uses `--args <json>` for JSON payloads, not `--params`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class ScreenshotCapture:
    """Captures chart screenshots using stealth_browser via mcporter.

    Uses subprocess calls to mcporter CLI for browser automation.
    Manages browser lifecycle and ensures proper cleanup.
    """

    DEFAULT_SERVER = "playwright"
    DEFAULT_VIEWPORT_WIDTH = 1400
    DEFAULT_VIEWPORT_HEIGHT = 900

    def __init__(
        self,
        output_dir: Path | None = None,
        timeout: int = 30,
        server_name: str | None = None,
    ) -> None:
        """Initialize screenshot capture.

        Args:
            output_dir: Directory to save screenshots (default: blog/images)
            timeout: Default timeout for mcporter calls in seconds
            server_name: MCP server name (default: "playwright")
        """
        self.output_dir = output_dir or Path(__file__).parent.parent / "images"
        self.output_dir.mkdir(exist_ok=True)
        self.timeout = timeout
        self._server_name = server_name or self.DEFAULT_SERVER
        self._browser_instance: str | None = None

    def _mcporter_call(self, tool: str, params: dict[str, Any] | None = None) -> Any:
        """Call mcporter CLI and return parsed JSON result.

        Uses `--args <json>` for JSON payloads per mcporter contract.

        Args:
            tool: Tool name (e.g., "playwright.browser_navigate")
            params: Optional parameters for the tool

        Returns:
            Parsed JSON response from mcporter

        Raises:
            RuntimeError: If mcporter call fails or server is unavailable
        """
        cmd = ["mcporter", "call", tool]
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
            # Provide clear error when MCP server is unavailable
            if "unknown server" in error_msg.lower():
                raise RuntimeError(
                    f"MCP server unavailable for selector '{tool}': {error_msg}"
                )
            raise RuntimeError(f"mcporter call failed for '{tool}': {error_msg}")

        # mcporter outputs the result to stdout
        # It may have multiple lines, so we look for JSON
        output = result.stdout.strip()

        # Try to extract JSON from the output
        # mcporter typically outputs: "Result: {...}" or just the JSON
        if "Result:" in output:
            json_part = output.split("Result:", 1)[1].strip()
        else:
            json_part = output

        try:
            return json.loads(json_part)
        except json.JSONDecodeError:
            # If it's not JSON, return the raw output
            return output

    def _ensure_browser(self) -> str:
        """Ensure browser is spawned and return instance ID.

        Passes required spawn options: headless=False, viewport size.

        Returns:
            Browser instance ID
        """
        if self._browser_instance is not None:
            return self._browser_instance

        # Use browser_navigate with required spawn options for this phase
        spawn_params = {
            "headless": False,
            "viewport_width": self.DEFAULT_VIEWPORT_WIDTH,
            "viewport_height": self.DEFAULT_VIEWPORT_HEIGHT,
        }
        result = self._mcporter_call(
            f"{self._server_name}.browser_navigate", spawn_params
        )
        # Result may be a dict with instance_id or the ID directly
        if isinstance(result, dict):
            self._browser_instance = result.get("instance_id") or result.get("id")
        else:
            self._browser_instance = str(result)

        if not self._browser_instance:
            raise RuntimeError("Failed to spawn browser: no instance ID returned")

        return self._browser_instance

    def _navigate(self, url: str, wait_for: str | None = None) -> None:
        """Navigate browser to URL.

        Args:
            url: URL to navigate to
            wait_for: Optional selector to wait for before returning
        """
        instance_id = self._ensure_browser()
        params: dict[str, Any] = {"instance_id": instance_id, "url": url}
        if wait_for:
            params["wait_for"] = wait_for

        self._mcporter_call(f"{self._server_name}.browser_navigate", params)

    def _wait_for_chart_ready(
        self, selector: str | None = None, timeout: int = 10
    ) -> None:
        """Wait for chart to be ready using polling.

        Args:
            selector: Optional CSS selector to wait for
            timeout: Maximum time to wait in seconds
        """
        import time

        # Simple bounded polling approach
        # In production, stealth_browser may have a proper wait primitive
        start_time = time.time()
        poll_interval = 0.5

        while time.time() - start_time < timeout:
            try:
                # Try to check if page is ready
                instance_id = self._ensure_browser()
                result = self._mcporter_call(
                    f"{self._server_name}.browser_evaluate",
                    {
                        "instance_id": instance_id,
                        "script": "document.readyState",
                    },
                )
                if result == "complete" or (
                    isinstance(result, dict) and result.get("result") == "complete"
                ):
                    # Additional wait for any chart rendering
                    time.sleep(1)
                    return
            except Exception:
                pass

            time.sleep(poll_interval)

        # If we reach here, just do a bounded sleep as fallback
        time.sleep(2)

    def _take_screenshot(
        self, selector: str | None = None, full_page: bool = False
    ) -> bytes:
        """Take a screenshot and return PNG bytes.

        Args:
            selector: Optional CSS selector to screenshot specific element
            full_page: Whether to capture full page

        Returns:
            PNG image data as bytes
        """
        instance_id = self._ensure_browser()
        params: dict[str, Any] = {
            "instance_id": instance_id,
            "full_page": full_page,
        }
        if selector:
            params["selector"] = selector

        result = self._mcporter_call(
            f"{self._server_name}.browser_take_screenshot", params
        )

        # Result should contain base64-encoded PNG
        if isinstance(result, dict):
            # Try various possible response formats
            if "data" in result:
                import base64

                return base64.b64decode(result["data"])
            elif "image" in result:
                import base64

                return base64.b64decode(result["image"])
            elif "bytes" in result:
                return result["bytes"]
            else:
                # Return the dict as JSON bytes for debugging
                return json.dumps(result).encode()
        elif isinstance(result, str):
            # Assume base64 string
            import base64

            return base64.b64decode(result)
        else:
            return bytes(result)

    def capture_tradingview(
        self,
        symbol: str,
        interval: str = "D",
        theme: str = "dark",
        width: int = 800,
        height: int = 400,
    ) -> Path:
        """Capture TradingView chart screenshot.

        Uses TradingView embed/widget URL for reliable chart capture.

        Args:
            symbol: TradingView symbol (e.g., "BINANCE:BTCUSDT")
            interval: Chart interval (D, 240, 60, 15, 5, 1)
            theme: Chart theme (dark, light)
            width: Chart width
            height: Chart height

        Returns:
            Path to the saved PNG file
        """
        # Build TradingView embed URL (widget format for reliable capture)
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
            self._wait_for_chart_ready()

            png_data = self._take_screenshot()
            output_path.write_bytes(png_data)

            return output_path
        except Exception:
            # Clean up if we created an empty file
            if output_path.exists() and output_path.stat().st_size == 0:
                output_path.unlink()
            raise

    def capture_upbit_chart(
        self,
        market: str = "KRW-BTC",
        interval: str = "240",
        theme: str = "dark",
    ) -> Path:
        """Capture Upbit chart screenshot.

        Args:
            market: Upbit market code (e.g., "KRW-BTC")
            interval: Chart interval (240, 60, 15, 5, 1)
            theme: Chart theme (dark, light)

        Returns:
            Path to the saved PNG file
        """
        # Build Upbit chart URL
        url = f"https://upbit.com/exchange?code=CRIX.UPBIT.{market}"

        output_path = (
            self.output_dir / f"screenshot_upbit_{market.replace('-', '_')}_{interval}.png"
        )

        try:
            self._navigate(url)
            self._wait_for_chart_ready()

            png_data = self._take_screenshot()
            output_path.write_bytes(png_data)

            return output_path
        except Exception:
            if output_path.exists() and output_path.stat().st_size == 0:
                output_path.unlink()
            raise

    def close(self) -> None:
        """Close browser instance and cleanup resources."""
        if self._browser_instance is None:
            return

        try:
            self._mcporter_call(
                f"{self._server_name}.browser_close",
                {"instance_id": self._browser_instance},
            )
        except Exception:
            # Ignore errors during cleanup
            pass
        finally:
            self._browser_instance = None

    def __enter__(self) -> ScreenshotCapture:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit - ensure browser is closed."""
        self.close()
