"""
배포(Deployment) SVG를 PNG로 변환하는 스크립트
Playwright를 사용하여 고품질 PNG 이미지 생성 (한글 폰트 지원)
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright


async def convert_svg_to_png(svg_path: Path, png_path: Path, width: int = 1600):
    """SVG 파일을 PNG로 변환"""

    # SVG 파일 읽기
    svg_content = svg_path.read_text(encoding='utf-8')

    # HTML 템플릿 (한글 폰트 지원)
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;700&display=swap');

            body {{
                margin: 0;
                padding: 20px;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                background: white;
                font-family: 'Noto Sans KR', 'Arial', sans-serif;
            }}
            svg {{
                max-width: 100%;
                height: auto;
                font-family: 'Noto Sans KR', 'Arial', sans-serif !important;
            }}
            svg text {{
                font-family: 'Noto Sans KR', 'Arial', sans-serif !important;
            }}
        </style>
    </head>
    <body>
        {svg_content}
    </body>
    </html>
    """

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        # HTML 설정
        await page.set_content(html_content)

        # 폰트 로딩 대기
        await page.wait_for_timeout(1000)

        # SVG 요소 찾기
        svg_element = await page.query_selector('svg')
        if not svg_element:
            raise ValueError(f"SVG element not found in {svg_path}")

        # SVG의 원본 크기 가져오기
        box = await svg_element.bounding_box()
        if not box:
            raise ValueError(f"Could not get bounding box for SVG in {svg_path}")

        # 비율 유지하면서 크기 계산
        aspect_ratio = box['height'] / box['width']
        height = int(width * aspect_ratio)

        # 뷰포트 설정
        await page.set_viewport_size({"width": width, "height": height})

        # 스크린샷 저장
        await svg_element.screenshot(path=str(png_path))

        await browser.close()

    print(f"✓ Converted: {svg_path.name} -> {png_path.name} ({width}x{height}px)")


async def main():
    """메인 함수"""

    # 이미지 디렉토리
    images_dir = Path(__file__).parent / "images"

    # 변환할 SVG 파일 목록 (파일명, PNG 파일명, 너비)
    svg_files = [
        ("deployment_thumbnail.svg", "deployment_thumbnail.png", 1200),
        ("deployment_before_after.svg", "deployment_before_after.png", 1200),
        ("deployment_architecture.svg", "deployment_architecture.png", 1400),
    ]

    print("🎨 배포 SVG to PNG 변환 시작...\n")

    for svg_name, png_name, width in svg_files:
        svg_path = images_dir / svg_name
        png_path = images_dir / png_name

        if not svg_path.exists():
            print(f"✗ SVG 파일을 찾을 수 없음: {svg_path}")
            continue

        try:
            await convert_svg_to_png(svg_path, png_path, width)
        except Exception as e:
            print(f"✗ 변환 실패 {svg_name}: {e}")

    print("\n✅ 변환 완료!")
    print("\n생성된 PNG:")
    for _, png_name, _ in svg_files:
        png_path = images_dir / png_name
        if png_path.exists():
            size_kb = png_path.stat().st_size / 1024
            print(f"  - {png_name} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    asyncio.run(main())
