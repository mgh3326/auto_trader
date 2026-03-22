"""Tests for screenshot capture and image composition utilities."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestImageComposer:
    """Tests for ImageComposer utility class."""

    def test_png_embeds_as_base64(self, tmp_path: Path) -> None:
        """Existing PNG file should embed as data:image/png;base64,..."""
        from blog.tools.image_composer import ImageComposer

        # Create a small valid PNG file
        png_path = tmp_path / "test.png"
        # Minimal valid PNG: 1x1 pixel, transparent
        png_data = bytes([
            0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,  # PNG signature
            0x00, 0x00, 0x00, 0x0D,  # IHDR length
            0x49, 0x48, 0x44, 0x52,  # IHDR
            0x00, 0x00, 0x00, 0x01,  # width: 1
            0x00, 0x00, 0x00, 0x01,  # height: 1
            0x08, 0x06, 0x00, 0x00, 0x00,  # 8-bit RGBA
            0x1F, 0x15, 0xC4, 0x89,  # IHDR CRC
            0x00, 0x00, 0x00, 0x0A,  # IDAT length
            0x49, 0x44, 0x41, 0x54,  # IDAT
            0x78, 0x9C, 0x63, 0x60, 0x00, 0x00, 0x00, 0x02, 0x00, 0x01,
            0x73, 0x75, 0x01, 0x18,  # IDAT data + CRC
            0x00, 0x00, 0x00, 0x00,  # IEND length
            0x49, 0x45, 0x4E, 0x44,  # IEND
            0xAE, 0x42, 0x60, 0x82,  # IEND CRC
        ])
        png_path.write_bytes(png_data)

        result = ImageComposer.embed_png(png_path, x=10, y=20, width=100, height=100)

        assert 'data:image/png;base64,' in result
        # Verify it's valid base64
        encoded = result.split('data:image/png;base64,')[1].split('"')[0]
        decoded = base64.b64decode(encoded)
        assert decoded == png_data

    def test_clip_path_emits_deterministic_id(self, tmp_path: Path) -> None:
        """clipPath should have deterministic ID based on position and size."""
        from blog.tools.image_composer import ImageComposer

        png_path = tmp_path / "test.png"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\n")  # Minimal PNG signature

        result = ImageComposer.embed_png(png_path, x=10, y=20, width=100, height=100)

        expected_id = "clip-10-20-100-100"
        assert f'id="{expected_id}"' in result

    def test_border_toggle(self, tmp_path: Path) -> None:
        """Border toggle should control border rectangle presence."""
        from blog.tools.image_composer import ImageComposer

        png_path = tmp_path / "test.png"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\n")

        with_border = ImageComposer.embed_png(
            png_path, x=10, y=20, width=100, height=100, border=True
        )
        without_border = ImageComposer.embed_png(
            png_path, x=10, y=20, width=100, height=100, border=False
        )

        # With border should have a rect element after the image
        assert "stroke=" in with_border
        # Without border should not have stroke
        assert "stroke=" not in without_border

    def test_shadow_toggle(self, tmp_path: Path) -> None:
        """Shadow toggle should control shadow filter presence."""
        from blog.tools.image_composer import ImageComposer

        png_path = tmp_path / "test.png"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\n")

        with_shadow = ImageComposer.embed_png(
            png_path, x=10, y=20, width=100, height=100, shadow=True
        )
        without_shadow = ImageComposer.embed_png(
            png_path, x=10, y=20, width=100, height=100, shadow=False
        )

        # With shadow should have filter attribute
        assert "filter=" in with_shadow
        # Without shadow should not have filter
        assert "filter=" not in without_shadow

    def test_missing_png_returns_comment(self, tmp_path: Path) -> None:
        """Missing PNG file should return SVG comment instead of raising."""
        from blog.tools.image_composer import ImageComposer

        missing_path = tmp_path / "nonexistent.png"

        result = ImageComposer.embed_png(missing_path, x=10, y=20, width=100, height=100)

        assert result.startswith("<!--")
        assert "not found" in result.lower() or "missing" in result.lower()

    def test_create_hybrid_technical_structure(self, tmp_path: Path) -> None:
        """Hybrid technical layout should include screenshot, indicator, and support/resistance fragments."""
        from blog.tools.image_composer import ImageComposer

        screenshot_path = tmp_path / "screenshot.png"
        screenshot_path.write_bytes(b"\x89PNG\r\n\x1a\n")

        indicator_fragment = '<rect x="880" y="95" width="480" height="350" fill="blue"/>'
        support_resistance_fragment = '<rect x="60" y="480" width="1300" height="280" fill="green"/>'

        result = ImageComposer.create_hybrid_technical(
            screenshot_path=screenshot_path,
            indicator_fragment=indicator_fragment,
            support_resistance_fragment=support_resistance_fragment,
            company_name="Test Corp",
            width=1400,
            height=800,
        )

        # Should contain the embedded PNG
        assert "data:image/png;base64," in result
        # Should contain the indicator fragment
        assert indicator_fragment in result
        # Should contain the support/resistance fragment
        assert support_resistance_fragment in result
        # Should contain the company name in title
        assert "Test Corp" in result


class TestScreenshotCaptureUnit:
    """Unit tests for ScreenshotCapture using mocks."""

    def test_mcporter_call_invokes_subprocess(self) -> None:
        """_mcporter_call should invoke subprocess.run with correct arguments."""
        from blog.tools.screenshot_capture import ScreenshotCapture

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout='{"instance_id": "test-123"}',
                returncode=0,
            )

            capture = ScreenshotCapture()
            result = capture._mcporter_call("stealth_browser.spawn_browser")

            mock_run.assert_called_once()
            call_args = mock_run.call_args
            assert "mcporter" in str(call_args[0][0])
            assert "call" in str(call_args[0][0])
            assert "stealth_browser.spawn_browser" in str(call_args[0][0])
            assert result == {"instance_id": "test-123"}

    def test_ensure_browser_caches_instance_id(self) -> None:
        """_ensure_browser should cache the browser instance ID."""
        from blog.tools.screenshot_capture import ScreenshotCapture

        with patch.object(ScreenshotCapture, "_mcporter_call") as mock_call:
            mock_call.return_value = {"instance_id": "cached-id"}

            capture = ScreenshotCapture()

            # First call should spawn browser
            capture._ensure_browser()
            assert capture._browser_instance == "cached-id"
            assert mock_call.call_count == 1

            # Second call should use cached ID
            capture._ensure_browser()
            assert capture._browser_instance == "cached-id"
            assert mock_call.call_count == 1  # No additional calls

    def test_close_calls_close_method_once(self) -> None:
        """close() should call the close method once and clear cached ID."""
        from blog.tools.screenshot_capture import ScreenshotCapture

        with patch.object(ScreenshotCapture, "_mcporter_call") as mock_call:
            mock_call.return_value = {"status": "closed"}

            capture = ScreenshotCapture()
            capture._browser_instance = "test-id"

            capture.close()

            mock_call.assert_called_once()
            assert capture._browser_instance is None

    def test_close_is_idempotent(self) -> None:
        """close() should be safe to call multiple times."""
        from blog.tools.screenshot_capture import ScreenshotCapture

        capture = ScreenshotCapture()
        capture._browser_instance = None

        # Should not raise
        capture.close()
        capture.close()

    def test_capture_tradingview_builds_correct_url(self) -> None:
        """capture_tradingview should build the expected TradingView embed URL."""
        from blog.tools.screenshot_capture import ScreenshotCapture

        with patch.object(ScreenshotCapture, "_ensure_browser"), \
             patch.object(ScreenshotCapture, "_navigate") as mock_nav, \
             patch.object(ScreenshotCapture, "_take_screenshot") as mock_screenshot:

            mock_screenshot.return_value = b"fake_png_data"

            capture = ScreenshotCapture(output_dir=Path("/tmp"))

            with patch.object(Path, "write_bytes") as mock_write:
                mock_write.return_value = None
                capture.capture_tradingview("BINANCE:BTCUSDT", interval="D", theme="dark")

            # Should navigate to TradingView embed URL
            mock_nav.assert_called_once()
            url_arg = mock_nav.call_args[0][0]
            assert "tradingview.com" in url_arg
            assert "BINANCE:BTCUSDT" in url_arg
            assert "interval=D" in url_arg or "D" in url_arg


@pytest.mark.skipif(
    not Path("/tmp/.stealth_browser_running").exists(),
    reason="stealth_browser service not running",
)
class TestScreenshotCaptureIntegration:
    """Integration tests for ScreenshotCapture - requires stealth_browser service."""

    def test_real_browser_lifecycle(self, tmp_path: Path) -> None:
        """Full browser lifecycle: spawn, navigate, screenshot, close."""
        from blog.tools.screenshot_capture import ScreenshotCapture

        capture = ScreenshotCapture(output_dir=tmp_path)

        try:
            path = capture.capture_tradingview("BINANCE:BTCUSDT", interval="D", theme="dark")

            assert path.exists()
            assert path.stat().st_size > 1000  # Should be a real PNG, not empty
        finally:
            capture.close()

    def test_upbit_chart_capture(self, tmp_path: Path) -> None:
        """Capture Upbit chart screenshot."""
        from blog.tools.screenshot_capture import ScreenshotCapture

        capture = ScreenshotCapture(output_dir=tmp_path)

        try:
            path = capture.capture_upbit_chart("KRW-BTC", interval="240", theme="dark")

            assert path.exists()
            assert path.stat().st_size > 1000
        finally:
            capture.close()
