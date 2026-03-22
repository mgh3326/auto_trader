"""CLI for capturing chart screenshots and composing hybrid technical analysis images.

This script captures a chart screenshot from TradingView or Upbit, then composes
a complete technical analysis image using the hybrid mode of StockAnalysisPreset.

Usage:
    uv run python blog/tools/capture_and_compose.py BINANCE:BTCUSDT \
        --interval D \
        --theme dark \
        --data-json /path/to/analysis.json \
        --output-dir blog/images
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def main() -> int:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="Capture chart screenshot and compose hybrid technical analysis image"
    )
    parser.add_argument(
        "symbol",
        help="Trading symbol (e.g., BINANCE:BTCUSDT) or Upbit market (e.g., KRW-BTC)",
    )
    parser.add_argument(
        "--interval",
        default="D",
        help="Chart interval (default: D). Options: D, 240, 60, 15, 5, 1",
    )
    parser.add_argument(
        "--theme",
        default="dark",
        choices=["dark", "light"],
        help="Chart theme (default: dark)",
    )
    parser.add_argument(
        "--source",
        default="tradingview",
        choices=["tradingview", "upbit"],
        help="Chart source (default: tradingview)",
    )
    parser.add_argument(
        "--data-json",
        type=Path,
        required=True,
        help="Path to JSON file containing analysis data for composition",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("blog/images"),
        help="Output directory for generated files (default: blog/images)",
    )
    parser.add_argument(
        "--keep-svg",
        action="store_true",
        help="Keep the hybrid SVG file (default: delete after PNG conversion)",
    )

    args = parser.parse_args()

    # Validate data JSON file
    if not args.data_json.exists():
        print(f"Error: Data JSON file not found: {args.data_json}", file=sys.stderr)
        return 1

    # Load analysis data
    try:
        with open(args.data_json, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in data file: {e}", file=sys.stderr)
        return 1

    # Import here to avoid slow imports if just showing help
    from blog.tools.presets.stock_analysis import StockAnalysisPreset
    from blog.tools.screenshot_capture import ScreenshotCapture

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    capture = ScreenshotCapture(output_dir=output_dir)
    screenshot_path: Path | None = None

    try:
        # Capture screenshot
        print(f"Capturing {args.source} chart for {args.symbol}...")
        if args.source == "tradingview":
            screenshot_path = capture.capture_tradingview(
                symbol=args.symbol,
                interval=args.interval,
                theme=args.theme,
            )
        else:  # upbit
            screenshot_path = capture.capture_upbit_chart(
                market=args.symbol,
                interval=args.interval,
                theme=args.theme,
            )

        print(f"Screenshot saved: {screenshot_path}")
        print(f"Size: {screenshot_path.stat().st_size} bytes")

        # Create preset with screenshot
        print("\nGenerating hybrid technical analysis images...")
        preset = StockAnalysisPreset(
            symbol=args.symbol.replace(":", "_").replace("-", "_"),
            data=data,
            output_dir=output_dir,
            screenshot_path=screenshot_path,
        )

        # Generate PNGs (which includes hybrid technical image)
        if args.keep_svg:
            # Just generate SVGs, don't convert to PNG
            svg_paths = preset.generate_svgs()
            print("\nGenerated SVG files:")
            for path in svg_paths:
                print(f"  - {path}")
        else:
            # Generate PNGs (hybrid SVG will be auto-deleted after conversion)
            png_paths = asyncio.run(preset.generate_pngs())
            print("\nGenerated PNG files:")
            for path in png_paths:
                print(f"  - {path}")

        print("\nDone!")
        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    finally:
        # Always close browser
        capture.close()


if __name__ == "__main__":
    sys.exit(main())
