#!/usr/bin/env python3
"""
Python 3.14 업그레이드 블로그 이미지 생성기

사용법:
    uv run python blog/images/python314_images.py
"""

import sys
from pathlib import Path
from typing import override

# 모듈 경로 추가
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from blog.tools.components.base import FONT_FAMILY
from blog.tools.components.thumbnail import ThumbnailTemplate
from blog.tools.image_generator import BlogImageGenerator


class Python314Images(BlogImageGenerator):
    """Python 3.14 업그레이드 블로그 이미지 생성기"""

    @override
    def get_images(self):
        return [
            ("thumbnail", 1200, 630, self.create_thumbnail),
            ("version_timeline", 1200, 450, self.create_version_timeline),
            ("performance_comparison", 1200, 500, self.create_performance_comparison),
        ]

    def create_thumbnail(self) -> str:
        """썸네일 이미지 (1200x630)"""
        return ThumbnailTemplate.create(
            title_line1="Python 3.14",
            title_line2="Pi Release",
            subtitle="t-strings, Free-threading, 그리고 성능 개선",
            icons=[
                ("zap", "Speed", "#FF6B35"),
                ("code", "Python", "#306998"),
                ("refresh-cw", "Upgrade", "#4CAF50"),
            ],
            theme="light",
            bg_pattern="wave",
            accent_color="#FFD43B",
        )

    def create_version_timeline(self) -> str:
        """Python 버전 타임라인 (1200x450)"""
        from blog.tools.components.base import SVGComponent

        year_markers = [
            (150, "2022"),
            (300, "2023"),
            (450, "2024"),
            (600, "2025"),
            (750, "2026"),
            (900, "2027"),
            (1050, "2028+"),
        ]
        versions = [
            (
                150,
                90,
                600,
                35,
                "#FFCDD2",
                "#E57373",
                2,
                170,
                115,
                16,
                "#C62828",
                "Python 3.11",
                450,
                "2022.10 Release → 2027.10 EOL",
                (750, 107, 8, "#E57373", 765, 112, "#C62828", "EOL"),
                None,
                None,
            ),
            (
                300,
                140,
                750,
                35,
                "#FFE0B2",
                "#FFB74D",
                2,
                320,
                165,
                16,
                "#EF6C00",
                "Python 3.12",
                600,
                "2023.10 Release → 2028.10 EOL",
                (1050, 157, 8, "#FFB74D", 1065, 162, "#EF6C00", "EOL"),
                None,
                None,
            ),
            (
                450,
                190,
                650,
                35,
                "#C8E6C9",
                "#81C784",
                2,
                470,
                215,
                16,
                "#2E7D32",
                "Python 3.13",
                700,
                "2024.10 Release → 2029.10 EOL",
                None,
                None,
                None,
            ),
            (
                600,
                240,
                500,
                45,
                "#FFD43B",
                "#3776AB",
                3,
                620,
                270,
                18,
                "#3776AB",
                "Python 3.14",
                None,
                None,
                None,
                (750, 270, 14, "#333333", '"Pi Release"'),
                (900, 270, 12, "#666666", "2025.10 → 2030.10"),
            ),
        ]

        svg = SVGComponent.header(1200, 450)
        svg += SVGComponent.background(1200, 450, fill="#ffffff")
        svg += SVGComponent.title(1200, "Python 버전별 릴리즈 및 EOL 타임라인")
        svg += '    <line x1="100" y1="350" x2="1100" y2="350" stroke="#666666" stroke-width="3"/>\n'

        for x, label in year_markers:
            svg += (
                f'    <text x="{x}" y="380" {FONT_FAMILY} '
                f'font-size="14" fill="#666666" text-anchor="middle">{label}</text>\n'
            )
            svg += (
                f'    <line x1="{x}" y1="340" x2="{x}" y2="360" '
                'stroke="#999999" stroke-width="2"/>\n'
            )

        for version in versions:
            (
                bar_x,
                bar_y,
                bar_width,
                bar_height,
                bar_fill,
                bar_stroke,
                bar_stroke_width,
                name_x,
                name_y,
                name_size,
                name_fill,
                name_text,
                range_x,
                range_text,
                eol,
                highlight,
                period,
            ) = version
            svg += (
                f'    <rect x="{bar_x}" y="{bar_y}" width="{bar_width}" height="{bar_height}" '
                f'fill="{bar_fill}" stroke="{bar_stroke}" stroke-width="{bar_stroke_width}" rx="5"/>\n'
            )
            svg += (
                f'    <text x="{name_x}" y="{name_y}" {FONT_FAMILY} '
                f'font-size="{name_size}" font-weight="bold" fill="{name_fill}">{name_text}</text>\n'
            )

            if range_x is not None and range_text is not None:
                svg += (
                    f'    <text x="{range_x}" y="{name_y}" {FONT_FAMILY} '
                    f'font-size="12" fill="#666666" text-anchor="middle">{range_text}</text>\n'
                )

            if eol is not None:
                (
                    eol_x,
                    eol_y,
                    eol_r,
                    eol_fill,
                    eol_text_x,
                    eol_text_y,
                    eol_text_fill,
                    eol_text,
                ) = eol
                svg += f'    <circle cx="{eol_x}" cy="{eol_y}" r="{eol_r}" fill="{eol_fill}"/>\n'
                svg += (
                    f'    <text x="{eol_text_x}" y="{eol_text_y}" {FONT_FAMILY} '
                    f'font-size="11" fill="{eol_text_fill}">{eol_text}</text>\n'
                )

            if highlight is not None:
                (
                    highlight_x,
                    highlight_y,
                    highlight_size,
                    highlight_fill,
                    highlight_text,
                ) = highlight
                svg += (
                    f'    <text x="{highlight_x}" y="{highlight_y}" {FONT_FAMILY} '
                    f'font-size="{highlight_size}" fill="{highlight_fill}">{highlight_text}</text>\n'
                )

            if period is not None:
                period_x, period_y, period_size, period_fill, period_text = period
                svg += (
                    f'    <text x="{period_x}" y="{period_y}" {FONT_FAMILY} '
                    f'font-size="{period_size}" fill="{period_fill}">{period_text}</text>\n'
                )

        svg += '    <line x1="650" y1="80" x2="650" y2="350" stroke="#F44336" stroke-width="2" stroke-dasharray="5,5"/>\n'
        svg += (
            '    <text x="650" y="410" {FONT_FAMILY} font-size="14" '
            'font-weight="bold" fill="#F44336" text-anchor="middle">현재 (2025.12)</text>\n'
        )
        svg += '    <rect x="100" y="400" width="20" height="15" fill="#FFD43B" stroke="#3776AB" stroke-width="2"/>\n'
        svg += '    <text x="130" y="412" {FONT_FAMILY} font-size="12" fill="#333333">현재 사용 중</text>\n'
        svg += '    <circle cx="250" cy="408" r="6" fill="#E57373"/>\n'
        svg += '    <text x="265" y="412" {FONT_FAMILY} font-size="12" fill="#666666">EOL (End of Life)</text>\n'
        svg += SVGComponent.footer()
        return svg

    def create_performance_comparison(self) -> str:
        """성능 비교 그래프 (1200x500)"""
        from blog.tools.components.base import SVGComponent

        y_axis_labels = [
            (90, "100%"),
            (170, "80%"),
            (250, "60%"),
            (330, "40%"),
            (400, "20%"),
        ]
        grid_lines = [90, 170, 250, 330]
        categories = [
            (
                "앱 시작 시간",
                (200, 90, 80, 310, "#81C784", "#388E3C", "2.298s", 240, 85, "#2E7D32"),
                (
                    290,
                    108,
                    80,
                    292,
                    "#FFD43B",
                    "#3776AB",
                    "2.156s",
                    330,
                    103,
                    "#3776AB",
                    "-6%",
                    125,
                ),
                (285, 430),
            ),
            (
                "테스트 실행",
                (430, 90, 80, 310, "#81C784", "#388E3C", "17.91s", 470, 85, "#2E7D32"),
                (
                    520,
                    102,
                    80,
                    298,
                    "#FFD43B",
                    "#3776AB",
                    "17.12s",
                    560,
                    97,
                    "#3776AB",
                    "-4%",
                    119,
                ),
                (515, 430),
            ),
            (
                "API 응답 시간",
                (660, 90, 80, 310, "#81C784", "#388E3C", "78.67ms", 700, 85, "#2E7D32"),
                (
                    750,
                    103,
                    80,
                    297,
                    "#FFD43B",
                    "#3776AB",
                    "75.23ms",
                    790,
                    98,
                    "#3776AB",
                    "-4%",
                    120,
                ),
                (745, 430),
            ),
            (
                "메모리 사용량",
                (890, 90, 80, 310, "#81C784", "#388E3C", "138.7MB", 930, 85, "#2E7D32"),
                (
                    980,
                    98,
                    80,
                    302,
                    "#FFD43B",
                    "#3776AB",
                    "135.2MB",
                    1020,
                    93,
                    "#3776AB",
                    "-2.5%",
                    115,
                ),
                (975, 430),
            ),
        ]

        svg = SVGComponent.header(1200, 500)
        svg += SVGComponent.background(1200, 500, fill="#ffffff")
        svg += SVGComponent.title(
            1200, "Python 3.13 vs 3.14 성능 비교", y=40, font_size=24
        )
        svg += '    <line x1="150" y1="80" x2="150" y2="400" stroke="#666666" stroke-width="2"/>\n'

        for y, label in y_axis_labels:
            svg += (
                f'    <text x="140" y="{y}" {FONT_FAMILY} '
                f'font-size="12" fill="#666666" text-anchor="end">{label}</text>\n'
            )

        for y in grid_lines:
            svg += (
                f'    <line x1="150" y1="{y}" x2="1100" y2="{y}" '
                'stroke="#e0e0e0" stroke-width="1"/>\n'
            )

        svg += '    <line x1="150" y1="400" x2="1100" y2="400" stroke="#666666" stroke-width="2"/>\n'

        for category_label, py313, py314, label_pos in categories:
            x, y, width, height, fill, stroke, value, value_x, value_y, value_fill = (
                py313
            )
            svg += (
                f'    <rect x="{x}" y="{y}" width="{width}" height="{height}" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>\n'
            )
            svg += (
                f'    <text x="{value_x}" y="{value_y}" {FONT_FAMILY} '
                f'font-size="11" fill="{value_fill}" text-anchor="middle">{value}</text>\n'
            )

            (
                x,
                y,
                width,
                height,
                fill,
                stroke,
                value,
                value_x,
                value_y,
                value_fill,
                improvement,
                improvement_y,
            ) = py314
            svg += (
                f'    <rect x="{x}" y="{y}" width="{width}" height="{height}" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>\n'
            )
            svg += (
                f'    <text x="{value_x}" y="{value_y}" {FONT_FAMILY} '
                f'font-size="11" fill="{value_fill}" text-anchor="middle">{value}</text>\n'
            )
            svg += (
                f'    <text x="{value_x}" y="{improvement_y}" {FONT_FAMILY} '
                'font-size="11" font-weight="bold" fill="#4CAF50" text-anchor="middle">'
                f"{improvement}</text>\n"
            )

            label_x, label_y = label_pos
            svg += (
                f'    <text x="{label_x}" y="{label_y}" {FONT_FAMILY} '
                f'font-size="13" fill="#333333" text-anchor="middle">{category_label}</text>\n'
            )

        svg += '    <rect x="400" y="455" width="25" height="18" fill="#81C784" stroke="#388E3C" stroke-width="1"/>\n'
        svg += '    <text x="435" y="470" {FONT_FAMILY} font-size="14" fill="#333333">Python 3.13</text>\n'
        svg += '    <rect x="560" y="455" width="25" height="18" fill="#FFD43B" stroke="#3776AB" stroke-width="1"/>\n'
        svg += '    <text x="595" y="470" {FONT_FAMILY} font-size="14" fill="#333333">Python 3.14</text>\n'
        svg += '    <text x="850" y="470" {FONT_FAMILY} font-size="14" font-weight="bold" fill="#4CAF50">평균 4% 성능 향상</text>\n'
        svg += SVGComponent.footer()
        return svg


if __name__ == "__main__":
    generator = Python314Images("python314_upgrade")
    generator.generate()
