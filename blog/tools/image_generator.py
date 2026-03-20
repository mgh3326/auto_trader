#!/usr/bin/env python3
"""
블로그 이미지 생성기 베이스 클래스

각 블로그 글의 이미지 생성 스크립트가 상속받아 사용합니다.

사용법:
    from blog.tools.image_generator import BlogImageGenerator

    class MyBlogImages(BlogImageGenerator):
        def get_images(self):
            return [
                ("thumbnail", 1200, 630, self.create_thumbnail),
                ("architecture", 1400, 900, self.create_architecture),
            ]

        def create_thumbnail(self) -> str:
            return '''<svg>...</svg>'''

        def create_architecture(self) -> str:
            return '''<svg>...</svg>'''

    if __name__ == "__main__":
        MyBlogImages("my_blog").generate()
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path


class BlogImageGenerator(ABC):
    """블로그 이미지 생성기 베이스 클래스"""

    def __init__(self, prefix: str, images_dir: Path | None = None):
        """
        Args:
            prefix: 이미지 파일 접두사 (예: "kis_trading")
            images_dir: 이미지 저장 디렉토리 (기본값: blog/images)
        """
        self.prefix = prefix
        if images_dir is None:
            self.images_dir = Path(__file__).parent.parent / "images"
        else:
            self.images_dir = Path(images_dir)
        self.images_dir.mkdir(exist_ok=True)

    @abstractmethod
    def get_images(self) -> list[tuple[str, int, int, Callable[[], str]]]:
        """
        생성할 이미지 목록 반환

        Returns:
            리스트 of (이름, 너비, 높이, SVG 생성 함수)
            예: [("thumbnail", 1200, 630, self.create_thumbnail)]
        """
        pass

    def save_svg(self, name: str, content: str) -> Path:
        """SVG 파일 저장"""
        filename = f"{self.prefix}_{name}.svg"
        output_path = self.images_dir / filename
        output_path.write_text(content, encoding="utf-8")
        return output_path

    def generate_svgs(self) -> list[Path]:
        """모든 SVG 파일 생성"""
        print(f"🎨 {self.prefix} 블로그 이미지 생성 시작...\n")

        svg_paths = []
        for name, width, height, create_func in self.get_images():
            svg_content = create_func()
            svg_path = self.save_svg(name, svg_content)
            print(f"✅ {svg_path.name} ({width}x{height})")
            svg_paths.append(svg_path)

        print(f"\n✨ {len(svg_paths)}개 SVG 파일 생성 완료!")
        return svg_paths

    async def convert_to_png(self) -> list[Path]:
        """SVG를 PNG로 변환"""
        from blog.tools.svg_converter import SVGConverter

        converter = SVGConverter(self.images_dir)

        files = []
        for name, width, height, _ in self.get_images():
            svg_name = f"{self.prefix}_{name}.svg"
            png_name = f"{self.prefix}_{name}.png"
            files.append((svg_name, png_name, width))

        print("\n🔄 PNG 변환 시작...\n")
        png_paths = await converter.convert_all(files)

        print(f"\n✨ {len(png_paths)}개 PNG 파일 생성 완료!")
        return png_paths

    def generate(self, convert_png: bool = True):
        """
        이미지 생성 실행 (SVG + PNG)

        Args:
            convert_png: PNG 변환 여부 (기본값: True)
        """
        # SVG 생성
        svg_paths = self.generate_svgs()

        # PNG 변환
        if convert_png:
            asyncio.run(self.convert_to_png())

        # 결과 요약
        print("\n" + "=" * 50)
        print("📁 생성된 파일:")
        for name, width, height, _ in self.get_images():
            svg_name = f"{self.prefix}_{name}.svg"
            png_name = f"{self.prefix}_{name}.png"
            png_path = self.images_dir / png_name
            if png_path.exists():
                size_kb = png_path.stat().st_size / 1024
                print(f"  • {png_name} ({width}x{height}, {size_kb:.1f}KB)")
            else:
                print(f"  • {svg_name} ({width}x{height})")

    # ========== SVG 헬퍼 메서드 ==========

    @staticmethod
    def svg_header(width: int, height: int, defs: str = "") -> str:
        """SVG 헤더 생성"""
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#666666" />
        </marker>
        {defs}
    </defs>
'''

    @staticmethod
    def svg_footer() -> str:
        """SVG 푸터"""
        return "</svg>"

    @staticmethod
    def gradient_defs(gradient_id: str, colors: list[tuple[int, str]]) -> str:
        """그라데이션 정의 생성"""
        stops = "\n".join(
            f'<stop offset="{offset}%" style="stop-color:{color};stop-opacity:1" />'
            for offset, color in colors
        )
        return f'''
        <linearGradient id="{gradient_id}" x1="0%" y1="0%" x2="100%" y2="100%">
            {stops}
        </linearGradient>'''

    @staticmethod
    def rect(
        x: int,
        y: int,
        width: int,
        height: int,
        fill: str = "#ffffff",
        stroke: str = "#333333",
        stroke_width: int = 2,
        rx: int = 8,
    ) -> str:
        """사각형 생성"""
        return f'<rect x="{x}" y="{y}" width="{width}" height="{height}" fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}" rx="{rx}"/>'

    @staticmethod
    def text(
        x: int,
        y: int,
        content: str,
        font_size: int = 14,
        fill: str = "#333333",
        anchor: str = "start",
        weight: str = "normal",
        font_family: str = "Arial, sans-serif",
    ) -> str:
        """텍스트 생성"""
        return f'<text x="{x}" y="{y}" font-family="{font_family}" font-size="{font_size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{content}</text>'

    @staticmethod
    def line(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        stroke: str = "#666666",
        stroke_width: int = 2,
        arrow: bool = False,
    ) -> str:
        """선 생성"""
        marker = ' marker-end="url(#arrowhead)"' if arrow else ""
        return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{stroke_width}"{marker}/>'

    @staticmethod
    def circle(
        cx: int,
        cy: int,
        r: int,
        fill: str = "#ffffff",
        stroke: str = "#333333",
        stroke_width: int = 2,
    ) -> str:
        """원 생성"""
        return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"/>'


# ========== 썸네일 템플릿 ==========


class ThumbnailTemplate:
    """블로그 썸네일 템플릿"""

    @staticmethod
    def create(
        title_line1: str,
        title_line2: str = "",
        subtitle: str = "",
        icons: list[tuple[str, str, str]] = None,  # [(emoji, label, color), ...]
        tech_stack: str = "",
        bg_gradient: tuple[str, str, str] = ("#0d1b2a", "#1b263b", "#415a77"),
        accent_color: str = "#4CAF50",
    ) -> str:
        """
        썸네일 이미지 생성 (1200x630)

        Args:
            title_line1: 첫 번째 제목 줄
            title_line2: 두 번째 제목 줄 (선택)
            subtitle: 부제목
            icons: 아이콘 리스트 [(emoji, label, color), ...]
            tech_stack: 하단 기술 스택 텍스트
            bg_gradient: 배경 그라데이션 (상단, 중간, 하단)
            accent_color: 강조 색상
        """
        width, height = 1200, 630

        # 배경 그라데이션
        svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <linearGradient id="bgGradient" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" style="stop-color:{bg_gradient[0]};stop-opacity:1" />
            <stop offset="50%" style="stop-color:{bg_gradient[1]};stop-opacity:1" />
            <stop offset="100%" style="stop-color:{bg_gradient[2]};stop-opacity:1" />
        </linearGradient>
    </defs>

    <!-- 배경 -->
    <rect width="{width}" height="{height}" fill="url(#bgGradient)"/>

    <!-- 제목 -->
    <text x="{width // 2}" y="140" font-family="Arial, sans-serif" font-size="52" font-weight="bold" fill="#ffffff" text-anchor="middle">
        {title_line1}
    </text>
'''

        if title_line2:
            svg += f'''    <text x="{width // 2}" y="210" font-family="Arial, sans-serif" font-size="52" font-weight="bold" fill="{accent_color}" text-anchor="middle">
        {title_line2}
    </text>
'''
            subtitle_y = 290
        else:
            subtitle_y = 220

        if subtitle:
            svg += f'''    <!-- 부제목 -->
    <text x="{width // 2}" y="{subtitle_y}" font-family="Arial, sans-serif" font-size="30" fill="#778da9" text-anchor="middle">
        {subtitle}
    </text>
'''

        # 아이콘들
        if icons:
            icon_start_x = width // 2 - (len(icons) * 130) // 2 + 50
            svg += f'\n    <!-- 아이콘들 -->\n    <g transform="translate({icon_start_x}, 380)">\n'

            for i, (emoji, label, color) in enumerate(icons):
                x = i * 130
                svg += f'''        <rect x="{x}" y="0" width="100" height="100" rx="10" fill="{color}" opacity="0.9"/>
        <text x="{x + 50}" y="55" font-family="Arial, sans-serif" font-size="40" fill="#ffffff" text-anchor="middle">{emoji}</text>
        <text x="{x + 50}" y="85" font-family="Arial, sans-serif" font-size="14" fill="#ffffff" text-anchor="middle">{label}</text>
'''
            svg += "    </g>\n"

        if tech_stack:
            svg += f'''    <!-- 하단 기술 스택 -->
    <text x="{width // 2}" y="590" font-family="Arial, sans-serif" font-size="20" fill="#778da9" text-anchor="middle">
        {tech_stack}
    </text>
'''

        svg += "</svg>"
        return svg


if __name__ == "__main__":
    # 예시: 기본 썸네일 생성
    svg = ThumbnailTemplate.create(
        title_line1="블로그 이미지 생성기",
        title_line2="재사용 가능한 도구",
        subtitle="SVG 생성부터 PNG 변환까지",
        icons=[
            ("🎨", "SVG", "#2196F3"),
            ("🖼️", "PNG", "#4CAF50"),
            ("📝", "Blog", "#FF9800"),
        ],
        tech_stack="Python • Playwright • SVG",
    )
    print(svg)
