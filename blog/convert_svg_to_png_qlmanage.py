#!/usr/bin/env python3
"""
macOS qlmanage를 사용하여 SVG를 PNG로 변환
브라우저 렌더링과 동일한 결과 보장
"""

import subprocess
from pathlib import Path
import os


def convert_svg_to_png_qlmanage(svg_path: str, png_path: str):
    """qlmanage를 사용하여 SVG를 PNG로 변환"""
    svg_file = Path(svg_path)
    png_file = Path(png_path)

    # qlmanage는 원본 파일과 같은 디렉토리에 썸네일을 생성
    # -t: 썸네일 생성, -s: 크기 (기본 1200px), -o: 출력 디렉토리
    cmd = [
        'qlmanage',
        '-t',
        '-s', '1200',
        '-o', str(svg_file.parent),
        str(svg_path)
    ]

    subprocess.run(cmd, capture_output=True, check=True)

    # qlmanage가 생성한 파일명: 원본명.png
    generated_png = svg_file.parent / f"{svg_file.name}.png"

    if generated_png.exists():
        # 원하는 이름으로 변경
        if png_file.exists():
            png_file.unlink()
        generated_png.rename(png_file)
        print(f"✓ {svg_file.name} → {png_file.name}")
    else:
        print(f"✗ {svg_file.name} 변환 실패")


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

    print("SVG → PNG 변환 시작 (qlmanage)...\n")

    for svg_file in svg_files:
        svg_path = images_dir / svg_file
        png_file = svg_file.replace(".svg", ".png")
        png_path = images_dir / png_file

        if svg_path.exists():
            try:
                convert_svg_to_png_qlmanage(str(svg_path), str(png_path))
            except Exception as e:
                print(f"✗ {svg_file} 변환 실패: {e}")
        else:
            print(f"✗ {svg_file} 파일을 찾을 수 없습니다.")

    print("\n변환 완료!")


if __name__ == "__main__":
    main()
