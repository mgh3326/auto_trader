#!/usr/bin/env python3
"""
Python 3.14 업그레이드 블로그 이미지 생성기

사용법:
    uv run python blog/images/python314_images.py
"""

import sys
from pathlib import Path

# 모듈 경로 추가
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from blog.tools.image_generator import BlogImageGenerator, ThumbnailTemplate


class Python314Images(BlogImageGenerator):
    """Python 3.14 업그레이드 블로그 이미지 생성기"""

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
                ("t\"\"", "t-strings", "#3776AB"),
                ("//", "Free-thread", "#FFD43B"),
                ("5%", "성능 향상", "#4CAF50"),
                ("zstd", "압축", "#FF9800"),
            ],
            tech_stack="Python 3.14 • UV • FastAPI • PostgreSQL • Redis",
            bg_gradient=("#0d1b2a", "#1b263b", "#415a77"),
            accent_color="#FFD43B",
        )

    def create_version_timeline(self) -> str:
        """Python 버전 타임라인 (1200x450)"""
        return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="450" xmlns="http://www.w3.org/2000/svg">
    <!-- 배경 -->
    <rect width="1200" height="450" fill="#ffffff"/>

    <!-- 제목 -->
    <text x="600" y="45" font-family="Arial, sans-serif" font-size="28" font-weight="bold" fill="#1a1a2e" text-anchor="middle">
        Python 버전별 릴리즈 및 EOL 타임라인
    </text>

    <!-- 타임라인 축 -->
    <line x1="100" y1="350" x2="1100" y2="350" stroke="#666666" stroke-width="3"/>

    <!-- 년도 표시 -->
    <text x="150" y="380" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">2022</text>
    <text x="300" y="380" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">2023</text>
    <text x="450" y="380" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">2024</text>
    <text x="600" y="380" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">2025</text>
    <text x="750" y="380" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">2026</text>
    <text x="900" y="380" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">2027</text>
    <text x="1050" y="380" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">2028+</text>

    <!-- 년도 구분선 -->
    <line x1="150" y1="340" x2="150" y2="360" stroke="#999999" stroke-width="2"/>
    <line x1="300" y1="340" x2="300" y2="360" stroke="#999999" stroke-width="2"/>
    <line x1="450" y1="340" x2="450" y2="360" stroke="#999999" stroke-width="2"/>
    <line x1="600" y1="340" x2="600" y2="360" stroke="#999999" stroke-width="2"/>
    <line x1="750" y1="340" x2="750" y2="360" stroke="#999999" stroke-width="2"/>
    <line x1="900" y1="340" x2="900" y2="360" stroke="#999999" stroke-width="2"/>
    <line x1="1050" y1="340" x2="1050" y2="360" stroke="#999999" stroke-width="2"/>

    <!-- Python 3.11 (2022.10 ~ 2027.10) -->
    <rect x="150" y="90" width="600" height="35" fill="#FFCDD2" stroke="#E57373" stroke-width="2" rx="5"/>
    <text x="170" y="115" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#C62828">Python 3.11</text>
    <text x="450" y="115" font-family="Arial, sans-serif" font-size="12" fill="#666666" text-anchor="middle">2022.10 Release → 2027.10 EOL</text>
    <circle cx="750" cy="107" r="8" fill="#E57373"/>
    <text x="765" y="112" font-family="Arial, sans-serif" font-size="11" fill="#C62828">EOL</text>

    <!-- Python 3.12 (2023.10 ~ 2028.10) -->
    <rect x="300" y="140" width="750" height="35" fill="#FFE0B2" stroke="#FFB74D" stroke-width="2" rx="5"/>
    <text x="320" y="165" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#EF6C00">Python 3.12</text>
    <text x="600" y="165" font-family="Arial, sans-serif" font-size="12" fill="#666666" text-anchor="middle">2023.10 Release → 2028.10 EOL</text>
    <circle cx="1050" cy="157" r="8" fill="#FFB74D"/>
    <text x="1065" y="162" font-family="Arial, sans-serif" font-size="11" fill="#EF6C00">EOL</text>

    <!-- Python 3.13 (2024.10 ~ 2029.10) -->
    <rect x="450" y="190" width="650" height="35" fill="#C8E6C9" stroke="#81C784" stroke-width="2" rx="5"/>
    <text x="470" y="215" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#2E7D32">Python 3.13</text>
    <text x="700" y="215" font-family="Arial, sans-serif" font-size="12" fill="#666666" text-anchor="middle">2024.10 Release → 2029.10 EOL</text>

    <!-- Python 3.14 (2025.10 ~ 2030.10) - 강조 -->
    <rect x="600" y="240" width="500" height="45" fill="#FFD43B" stroke="#3776AB" stroke-width="3" rx="5"/>
    <text x="620" y="270" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#3776AB">Python 3.14</text>
    <text x="750" y="270" font-family="Arial, sans-serif" font-size="14" fill="#333333">"Pi Release"</text>
    <text x="900" y="270" font-family="Arial, sans-serif" font-size="12" fill="#666666">2025.10 → 2030.10</text>

    <!-- 현재 시점 표시 -->
    <line x1="650" y1="80" x2="650" y2="350" stroke="#F44336" stroke-width="2" stroke-dasharray="5,5"/>
    <text x="650" y="410" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#F44336" text-anchor="middle">현재 (2025.12)</text>

    <!-- 범례 -->
    <rect x="100" y="400" width="20" height="15" fill="#FFD43B" stroke="#3776AB" stroke-width="2"/>
    <text x="130" y="412" font-family="Arial, sans-serif" font-size="12" fill="#333333">현재 사용 중</text>

    <circle cx="250" cy="408" r="6" fill="#E57373"/>
    <text x="265" y="412" font-family="Arial, sans-serif" font-size="12" fill="#666666">EOL (End of Life)</text>
</svg>"""

    def create_performance_comparison(self) -> str:
        """성능 비교 그래프 (1200x500)"""
        return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="500" xmlns="http://www.w3.org/2000/svg">
    <!-- 배경 -->
    <rect width="1200" height="500" fill="#ffffff"/>

    <!-- 제목 -->
    <text x="600" y="40" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#1a1a2e" text-anchor="middle">
        Python 3.13 vs 3.14 성능 비교
    </text>

    <!-- Y축 -->
    <line x1="150" y1="80" x2="150" y2="400" stroke="#666666" stroke-width="2"/>
    <text x="140" y="90" font-family="Arial, sans-serif" font-size="12" fill="#666666" text-anchor="end">100%</text>
    <text x="140" y="170" font-family="Arial, sans-serif" font-size="12" fill="#666666" text-anchor="end">80%</text>
    <text x="140" y="250" font-family="Arial, sans-serif" font-size="12" fill="#666666" text-anchor="end">60%</text>
    <text x="140" y="330" font-family="Arial, sans-serif" font-size="12" fill="#666666" text-anchor="end">40%</text>
    <text x="140" y="400" font-family="Arial, sans-serif" font-size="12" fill="#666666" text-anchor="end">20%</text>

    <!-- 그리드 라인 -->
    <line x1="150" y1="90" x2="1100" y2="90" stroke="#e0e0e0" stroke-width="1"/>
    <line x1="150" y1="170" x2="1100" y2="170" stroke="#e0e0e0" stroke-width="1"/>
    <line x1="150" y1="250" x2="1100" y2="250" stroke="#e0e0e0" stroke-width="1"/>
    <line x1="150" y1="330" x2="1100" y2="330" stroke="#e0e0e0" stroke-width="1"/>

    <!-- X축 -->
    <line x1="150" y1="400" x2="1100" y2="400" stroke="#666666" stroke-width="2"/>

    <!-- 카테고리 1: 앱 시작 시간 -->
    <rect x="200" y="90" width="80" height="310" fill="#81C784" stroke="#388E3C" stroke-width="2"/>
    <text x="240" y="85" font-family="Arial, sans-serif" font-size="11" fill="#2E7D32" text-anchor="middle">2.298s</text>

    <rect x="290" y="108" width="80" height="292" fill="#FFD43B" stroke="#3776AB" stroke-width="2"/>
    <text x="330" y="103" font-family="Arial, sans-serif" font-size="11" fill="#3776AB" text-anchor="middle">2.156s</text>
    <text x="330" y="125" font-family="Arial, sans-serif" font-size="11" font-weight="bold" fill="#4CAF50" text-anchor="middle">-6%</text>

    <text x="285" y="430" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">앱 시작 시간</text>

    <!-- 카테고리 2: 테스트 실행 -->
    <rect x="430" y="90" width="80" height="310" fill="#81C784" stroke="#388E3C" stroke-width="2"/>
    <text x="470" y="85" font-family="Arial, sans-serif" font-size="11" fill="#2E7D32" text-anchor="middle">17.91s</text>

    <rect x="520" y="102" width="80" height="298" fill="#FFD43B" stroke="#3776AB" stroke-width="2"/>
    <text x="560" y="97" font-family="Arial, sans-serif" font-size="11" fill="#3776AB" text-anchor="middle">17.12s</text>
    <text x="560" y="119" font-family="Arial, sans-serif" font-size="11" font-weight="bold" fill="#4CAF50" text-anchor="middle">-4%</text>

    <text x="515" y="430" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">테스트 실행</text>

    <!-- 카테고리 3: API 응답 시간 -->
    <rect x="660" y="90" width="80" height="310" fill="#81C784" stroke="#388E3C" stroke-width="2"/>
    <text x="700" y="85" font-family="Arial, sans-serif" font-size="11" fill="#2E7D32" text-anchor="middle">78.67ms</text>

    <rect x="750" y="103" width="80" height="297" fill="#FFD43B" stroke="#3776AB" stroke-width="2"/>
    <text x="790" y="98" font-family="Arial, sans-serif" font-size="11" fill="#3776AB" text-anchor="middle">75.23ms</text>
    <text x="790" y="120" font-family="Arial, sans-serif" font-size="11" font-weight="bold" fill="#4CAF50" text-anchor="middle">-4%</text>

    <text x="745" y="430" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">API 응답 시간</text>

    <!-- 카테고리 4: 메모리 사용량 -->
    <rect x="890" y="90" width="80" height="310" fill="#81C784" stroke="#388E3C" stroke-width="2"/>
    <text x="930" y="85" font-family="Arial, sans-serif" font-size="11" fill="#2E7D32" text-anchor="middle">138.7MB</text>

    <rect x="980" y="98" width="80" height="302" fill="#FFD43B" stroke="#3776AB" stroke-width="2"/>
    <text x="1020" y="93" font-family="Arial, sans-serif" font-size="11" fill="#3776AB" text-anchor="middle">135.2MB</text>
    <text x="1020" y="115" font-family="Arial, sans-serif" font-size="11" font-weight="bold" fill="#4CAF50" text-anchor="middle">-2.5%</text>

    <text x="975" y="430" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">메모리 사용량</text>

    <!-- 범례 -->
    <rect x="400" y="455" width="25" height="18" fill="#81C784" stroke="#388E3C" stroke-width="1"/>
    <text x="435" y="470" font-family="Arial, sans-serif" font-size="14" fill="#333333">Python 3.13</text>

    <rect x="560" y="455" width="25" height="18" fill="#FFD43B" stroke="#3776AB" stroke-width="1"/>
    <text x="595" y="470" font-family="Arial, sans-serif" font-size="14" fill="#333333">Python 3.14</text>

    <!-- 결론 -->
    <text x="850" y="470" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#4CAF50">평균 4% 성능 향상</text>
</svg>"""


if __name__ == "__main__":
    generator = Python314Images("python314_upgrade")
    generator.generate()
