#!/usr/bin/env python3
"""
SVG to PNG 변환 도구

Playwright를 사용하여 SVG를 고품질 PNG로 변환합니다.
한글 폰트를 완벽하게 지원합니다.

사용법:
    # CLI로 사용
    python -m blog.tools.svg_converter images/*.svg

    # 코드에서 사용
    from blog.tools.svg_converter import SVGConverter

    converter = SVGConverter()
    await converter.convert("image.svg", "image.png", width=1200)

    # 여러 파일 변환
    await converter.convert_all([
        ("thumb.svg", "thumb.png", 1200),
        ("arch.svg", "arch.png", 1400),
    ])
"""

import asyncio
from pathlib import Path


class SVGConverter:
    """SVG to PNG 변환기"""

    def __init__(self, images_dir: Path | None = None):
        """
        Args:
            images_dir: 이미지 디렉토리 (기본값: blog/images)
        """
        if images_dir is None:
            self.images_dir = Path(__file__).parent.parent / "images"
        else:
            self.images_dir = Path(images_dir)

    async def convert(
        self,
        svg_path: str | Path,
        png_path: str | Path | None = None,
        width: int = 1200,
    ) -> Path:
        """
        SVG 파일을 PNG로 변환

        Args:
            svg_path: SVG 파일 경로 (절대 경로 또는 images_dir 기준 상대 경로)
            png_path: PNG 저장 경로 (None이면 SVG와 같은 이름으로 저장)
            width: 출력 이미지 너비 (픽셀)

        Returns:
            생성된 PNG 파일 경로
        """
        from playwright.async_api import async_playwright

        # 경로 처리
        svg_path = Path(svg_path)
        if not svg_path.is_absolute():
            svg_path = self.images_dir / svg_path

        if png_path is None:
            png_path = svg_path.with_suffix(".png")
        else:
            png_path = Path(png_path)
            if not png_path.is_absolute():
                png_path = self.images_dir / png_path

        if not svg_path.exists():
            raise FileNotFoundError(f"SVG 파일을 찾을 수 없습니다: {svg_path}")

        # SVG 파일 읽기
        svg_content = svg_path.read_text(encoding="utf-8")

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
            svg_element = await page.query_selector("svg")
            if not svg_element:
                raise ValueError(f"SVG 요소를 찾을 수 없습니다: {svg_path}")

            # SVG의 원본 크기 가져오기
            box = await svg_element.bounding_box()
            if not box:
                raise ValueError(f"SVG bounding box를 가져올 수 없습니다: {svg_path}")

            # 비율 유지하면서 크기 계산
            aspect_ratio = box["height"] / box["width"]
            height = int(width * aspect_ratio)

            # 뷰포트 설정
            await page.set_viewport_size({"width": width, "height": height})

            # 스크린샷 저장
            await svg_element.screenshot(path=str(png_path))

            await browser.close()

        print(f"✓ {svg_path.name} → {png_path.name} ({width}x{height}px)")
        return png_path

    async def convert_all(
        self,
        files: list[tuple[str, str, int]],
        stop_on_error: bool = False,
    ) -> list[Path]:
        """
        여러 SVG 파일을 PNG로 변환

        Args:
            files: (svg_path, png_path, width) 튜플 리스트
            stop_on_error: 에러 시 중단 여부

        Returns:
            생성된 PNG 파일 경로 리스트
        """
        results = []

        for svg_name, png_name, width in files:
            try:
                png_path = await self.convert(svg_name, png_name, width)
                results.append(png_path)
            except Exception as e:
                print(f"✗ {svg_name} 변환 실패: {e}")
                if stop_on_error:
                    raise

        return results

    async def convert_directory(
        self,
        pattern: str = "*.svg",
        width: int = 1200,
    ) -> list[Path]:
        """
        디렉토리 내 모든 SVG 파일을 PNG로 변환

        Args:
            pattern: glob 패턴 (기본값: *.svg)
            width: 출력 이미지 너비

        Returns:
            생성된 PNG 파일 경로 리스트
        """
        svg_files = list(self.images_dir.glob(pattern))
        results = []

        for svg_path in svg_files:
            try:
                png_path = await self.convert(svg_path, width=width)
                results.append(png_path)
            except Exception as e:
                print(f"✗ {svg_path.name} 변환 실패: {e}")

        return results


async def main():
    """CLI 메인 함수"""
    import argparse

    parser = argparse.ArgumentParser(
        description="SVG를 PNG로 변환합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
    # 단일 파일 변환
    python -m blog.tools.svg_converter thumbnail.svg

    # 여러 파일 변환
    python -m blog.tools.svg_converter *.svg

    # 너비 지정
    python -m blog.tools.svg_converter thumbnail.svg -w 1400

    # 출력 파일명 지정
    python -m blog.tools.svg_converter input.svg -o output.png
        """,
    )
    parser.add_argument("files", nargs="+", help="변환할 SVG 파일들")
    parser.add_argument("-w", "--width", type=int, default=1200, help="출력 너비 (기본값: 1200)")
    parser.add_argument("-o", "--output", help="출력 파일명 (단일 파일 변환 시)")
    parser.add_argument("-d", "--dir", help="이미지 디렉토리")

    args = parser.parse_args()

    images_dir = Path(args.dir) if args.dir else None
    converter = SVGConverter(images_dir)

    print("🎨 SVG → PNG 변환 시작...\n")

    for svg_file in args.files:
        svg_path = Path(svg_file)

        # 출력 파일명 결정
        if args.output and len(args.files) == 1:
            png_path = args.output
        else:
            png_path = None

        try:
            await converter.convert(svg_path, png_path, args.width)
        except Exception as e:
            print(f"✗ {svg_file} 변환 실패: {e}")

    print("\n✅ 변환 완료!")


if __name__ == "__main__":
    asyncio.run(main())
