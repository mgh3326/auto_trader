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
        for name, width, _height, _ in self.get_images():
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
        self.generate_svgs()

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


# Re-export for backward compatibility — existing scripts import from here
from blog.tools.components.thumbnail import ThumbnailTemplate  # noqa: F401, E402
