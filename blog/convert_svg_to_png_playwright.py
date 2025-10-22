#!/usr/bin/env python3
"""
Playwright를 사용하여 SVG를 브라우저에서 렌더링한 후 PNG로 변환
한글 폰트 완벽 지원
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright


async def convert_svg_to_png(svg_path: Path, png_path: Path, scale: float = 2.0):
    """
    Playwright로 SVG를 PNG로 변환

    Args:
        svg_path: SVG 파일 경로
        png_path: PNG 저장 경로
        scale: 스케일 배율 (2.0 = 2배 해상도)
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        # SVG 파일 읽기
        svg_content = svg_path.read_text(encoding='utf-8')

        # HTML 페이지 생성 (SVG를 inline으로 포함)
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{
                    margin: 0;
                    padding: 0;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    background: transparent;
                }}
                svg {{
                    display: block;
                }}
            </style>
        </head>
        <body>
            {svg_content}
        </body>
        </html>
        """

        # HTML 설정
        await page.set_content(html_content)

        # SVG 요소 찾기
        svg_element = await page.query_selector('svg')

        if svg_element:
            # SVG의 원래 크기 가져오기
            box = await svg_element.bounding_box()

            if box:
                # 뷰포트 크기 설정
                await page.set_viewport_size({
                    'width': int(box['width']),
                    'height': int(box['height'])
                })

                # 스크린샷 (고해상도)
                await svg_element.screenshot(
                    path=str(png_path),
                    type='png',
                    scale='device',  # 디바이스 픽셀 비율 사용
                )

                print(f"✓ {svg_path.name} → {png_path.name} ({int(box['width'] * scale)}x{int(box['height'] * scale)}px)")
            else:
                print(f"✗ {svg_path.name}: bounding box를 찾을 수 없습니다")
        else:
            print(f"✗ {svg_path.name}: SVG 요소를 찾을 수 없습니다")

        await browser.close()


async def main():
    images_dir = Path("blog/images")

    # 변환할 SVG 파일 목록
    svg_files = [
        "upbit_system_overview.svg",
        "bitcoin_prompt_structure.svg",
        "bitcoin_chart_analysis.svg",
        "exchange_api_comparison.svg",
        "unified_trading_system.svg",
    ]

    print("🌐 Playwright를 사용한 SVG → PNG 변환 시작...\n")
    print("브라우저 렌더링으로 한글 폰트 완벽 지원!\n")

    for svg_file in svg_files:
        svg_path = images_dir / svg_file
        png_file = svg_file.replace(".svg", ".png")
        png_path = images_dir / png_file

        if svg_path.exists():
            try:
                await convert_svg_to_png(svg_path, png_path, scale=2.0)
            except Exception as e:
                print(f"✗ {svg_file} 변환 실패: {e}")
        else:
            print(f"✗ {svg_file} 파일을 찾을 수 없습니다.")

    print("\n✅ 변환 완료!")
    print(f"📁 저장 위치: {images_dir.absolute()}")


if __name__ == "__main__":
    asyncio.run(main())
