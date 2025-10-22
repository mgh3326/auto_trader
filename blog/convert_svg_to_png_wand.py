#!/usr/bin/env python3
"""
Wand (ImageMagick)를 사용하여 SVG를 PNG로 변환
"""

from wand.image import Image
from pathlib import Path


def convert_svg_to_png_wand(svg_path: str, png_path: str, width: int = 1200):
    """Wand를 사용하여 SVG를 PNG로 변환"""
    with Image(filename=svg_path, resolution=150) as img:
        img.format = 'png'
        # 너비 조정
        if width:
            aspect_ratio = img.height / img.width
            img.resize(width, int(width * aspect_ratio))
        img.save(filename=png_path)
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

    print("SVG → PNG 변환 시작 (Wand)...\n")

    for svg_file in svg_files:
        svg_path = images_dir / svg_file
        png_file = svg_file.replace(".svg", ".png")
        png_path = images_dir / png_file

        if svg_path.exists():
            try:
                convert_svg_to_png_wand(str(svg_path), str(png_path))
            except Exception as e:
                print(f"✗ {svg_file} 변환 실패: {e}")
        else:
            print(f"✗ {svg_file} 파일을 찾을 수 없습니다.")

    print("\n변환 완료!")


if __name__ == "__main__":
    main()
