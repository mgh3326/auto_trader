#!/usr/bin/env python3
"""
SVG 파일을 PNG로 변환하는 스크립트
"""

from svglib.svglib import svg2rlg
from reportlab.graphics import renderPM
from pathlib import Path

def convert_svg_to_png(svg_path: str, png_path: str, scale: float = 1.0):
    """SVG 파일을 PNG로 변환"""
    drawing = svg2rlg(svg_path)
    renderPM.drawToFile(drawing, png_path, fmt="PNG", dpi=96 * scale)
    print(f"✓ {Path(svg_path).name} → {Path(png_path).name}")


def main():
    images_dir = Path("blog/images")

    # 변환할 SVG 파일 목록
    svg_files = [
        "upbit_system_overview.svg",
        "bitcoin_prompt_structure.svg",
        "bitcoin_chart_analysis.svg",
        "exchange_api_comparison.svg",
        "unified_trading_system.svg",
    ]

    print("SVG → PNG 변환 시작...\n")

    for svg_file in svg_files:
        svg_path = images_dir / svg_file
        png_file = svg_file.replace(".svg", ".png")
        png_path = images_dir / png_file

        if svg_path.exists():
            convert_svg_to_png(str(svg_path), str(png_path))
        else:
            print(f"✗ {svg_file} 파일을 찾을 수 없습니다.")

    print("\n변환 완료!")


if __name__ == "__main__":
    main()
