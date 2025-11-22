#!/usr/bin/env python3
"""
배포(Deployment) 시스템 블로그 SVG 이미지 생성 스크립트

SVG 형식으로 이미지를 생성한 후 convert_svg_to_png_playwright.py로 PNG 변환

사용법:
    python blog/generate_deployment_svg.py
    python blog/convert_svg_to_png_playwright.py blog/images/deployment_*.svg

생성되는 SVG:
    - blog/images/deployment_thumbnail.svg (1200x630)
    - blog/images/deployment_before_after.svg (1200x800)
    - blog/images/deployment_architecture.svg (1400x1000)
"""

from pathlib import Path


def create_thumbnail_svg() -> str:
    """썸네일 이미지 SVG 생성 (1200x630)"""
    return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="630" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <linearGradient id="bgGradient" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" style="stop-color:#16213e;stop-opacity:1" />
            <stop offset="50%" style="stop-color:#0f3460;stop-opacity:1" />
            <stop offset="100%" style="stop-color:#533483;stop-opacity:1" />
        </linearGradient>
    </defs>

    <!-- 배경 -->
    <rect width="1200" height="630" fill="url(#bgGradient)"/>

    <!-- 제목 -->
    <text x="600" y="140" font-family="Arial, sans-serif" font-size="55" font-weight="bold" fill="#ffffff" text-anchor="middle">
        라즈베리파이 홈서버에
    </text>
    <text x="600" y="220" font-family="Arial, sans-serif" font-size="55" font-weight="bold" fill="#ffffff" text-anchor="middle">
        자동 HTTPS로 안전하게 배포
    </text>

    <!-- 부제목 -->
    <text x="600" y="320" font-family="Arial, sans-serif" font-size="35" fill="#00d4ff" text-anchor="middle">
        Caddy + Docker Compose 프로덕션 배포
    </text>

    <!-- 아이콘 원들 -->
    <circle cx="200" cy="500" r="30" fill="#C51A4A"/>  <!-- Raspberry Pi -->
    <text x="200" y="515" font-family="Arial, sans-serif" font-size="35" font-weight="bold" fill="#ffffff" text-anchor="middle">π</text>

    <circle cx="400" cy="500" r="30" fill="#1F88C0"/>  <!-- Docker -->
    <text x="400" y="515" font-family="Arial, sans-serif" font-size="35" font-weight="bold" fill="#ffffff" text-anchor="middle">🐋</text>

    <circle cx="600" cy="500" r="30" fill="#1F88C0"/>  <!-- Caddy -->
    <text x="600" y="515" font-family="Arial, sans-serif" font-size="35" font-weight="bold" fill="#ffffff" text-anchor="middle">🔒</text>

    <circle cx="800" cy="500" r="30" fill="#4CAF50"/>  <!-- DuckDNS -->
    <text x="800" y="515" font-family="Arial, sans-serif" font-size="35" font-weight="bold" fill="#ffffff" text-anchor="middle">🦆</text>

    <circle cx="1000" cy="500" r="30" fill="#F46800"/>  <!-- 24/7 -->
    <text x="1000" y="515" font-family="Arial, sans-serif" font-size="35" font-weight="bold" fill="#ffffff" text-anchor="middle">⚡</text>

    <!-- 하단 텍스트 -->
    <text x="600" y="580" font-family="Arial, sans-serif" font-size="22" fill="#a8dadc" text-anchor="middle">
        Raspberry Pi • Caddy • Let's Encrypt • 24시간 운영
    </text>
</svg>"""


def create_before_after_svg() -> str:
    """배포 전후 비교 SVG 생성 (1200x800)"""
    return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="800" xmlns="http://www.w3.org/2000/svg">
    <!-- 배경 -->
    <rect width="1200" height="800" fill="#f8f9fa"/>

    <!-- 제목 -->
    <text x="600" y="60" font-family="Arial, sans-serif" font-size="50" font-weight="bold" fill="#1a1a2e" text-anchor="middle">
        localhost vs HTTPS 도메인 배포
    </text>

    <!-- Before 섹션 (localhost) -->
    <text x="300" y="160" font-family="Arial, sans-serif" font-size="40" font-weight="bold" fill="#e63946" text-anchor="middle">
        ❌ Before (localhost)
    </text>
    <rect x="50" y="200" width="500" height="550" fill="none" stroke="#e63946" stroke-width="3"/>

    <text x="300" y="260" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 접속: localhost:8000만
    </text>
    <text x="300" y="320" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 보안: HTTP (암호화 없음)
    </text>
    <text x="300" y="380" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 운영: 노트북 켜야 함
    </text>
    <text x="300" y="440" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 외부 접근: 불가능
    </text>
    <text x="300" y="500" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 인증서: 없음 (🔓)
    </text>
    <text x="300" y="560" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 모니터링: 수동 확인
    </text>
    <text x="300" y="620" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 안정성: 낮음
    </text>
    <text x="300" y="680" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 비용: $0 (개발만)
    </text>

    <!-- After 섹션 (HTTPS + 24/7) -->
    <text x="900" y="160" font-family="Arial, sans-serif" font-size="40" font-weight="bold" fill="#06d6a0" text-anchor="middle">
        ✅ After (프로덕션)
    </text>
    <rect x="650" y="200" width="500" height="550" fill="none" stroke="#06d6a0" stroke-width="3"/>

    <text x="900" y="260" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 접속: your-domain.com
    </text>
    <text x="900" y="320" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 보안: HTTPS (Let's Encrypt)
    </text>
    <text x="900" y="380" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 운영: 24시간 자동 실행
    </text>
    <text x="900" y="440" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 외부 접근: 언제 어디서나
    </text>
    <text x="900" y="500" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 인증서: 자동 발급/갱신 (🔒)
    </text>
    <text x="900" y="560" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 모니터링: Grafana 대시보드
    </text>
    <text x="900" y="620" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 안정성: 높음 (자동 재시작)
    </text>
    <text x="900" y="680" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 비용: $2.50/월 (전기세만)
    </text>
</svg>"""


def create_architecture_svg() -> str:
    """아키텍처 다이어그램 SVG 생성 (1400x1000)"""
    return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1400" height="1000" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <!-- 화살표 마커 -->
        <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#333333" />
        </marker>
    </defs>

    <!-- 배경 -->
    <rect width="1400" height="1000" fill="#ffffff"/>

    <!-- 제목 -->
    <text x="700" y="50" font-family="Arial, sans-serif" font-size="40" font-weight="bold" fill="#1a1a2e" text-anchor="middle">
        Caddy + Docker Compose 배포 아키텍처
    </text>

    <!-- 인터넷 -->
    <ellipse cx="700" cy="130" rx="80" ry="40" fill="#E0E0E0" stroke="#333333" stroke-width="2"/>
    <text x="700" y="140" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#333333" text-anchor="middle">인터넷</text>

    <!-- 공유기 -->
    <rect x="600" y="200" width="200" height="80" fill="#90CAF9" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="700" y="235" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#000000" text-anchor="middle">공유기</text>
    <text x="700" y="260" font-family="Arial, sans-serif" font-size="18" fill="#000000" text-anchor="middle">포트포워딩</text>

    <!-- Raspberry Pi 5 -->
    <rect x="50" y="320" width="1300" height="650" fill="#FFF3E0" stroke="#E65100" stroke-width="3" rx="10"/>
    <text x="700" y="360" font-family="Arial, sans-serif" font-size="28" font-weight="bold" fill="#E65100" text-anchor="middle">🍓 Raspberry Pi 5 (8GB)</text>

    <!-- Caddy -->
    <rect x="550" y="400" width="300" height="100" fill="#1F88C0" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="700" y="440" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#ffffff" text-anchor="middle">Caddy</text>
    <text x="700" y="465" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">Reverse Proxy</text>
    <text x="700" y="485" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">+ Auto HTTPS</text>

    <!-- Auto-trader -->
    <rect x="100" y="560" width="250" height="120" fill="#4CAF50" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="225" y="600" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">Auto-trader</text>
    <text x="225" y="625" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">FastAPI</text>
    <text x="225" y="650" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">:8000 (내부)</text>

    <!-- Grafana -->
    <rect x="400" y="560" width="250" height="120" fill="#F46800" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="525" y="600" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">Grafana</text>
    <text x="525" y="625" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">Dashboard</text>
    <text x="525" y="650" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">:3000 (내부)</text>

    <!-- Tempo -->
    <rect x="700" y="560" width="200" height="90" fill="#E91E63" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="800" y="595" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#ffffff" text-anchor="middle">Tempo</text>
    <text x="800" y="620" font-family="Arial, sans-serif" font-size="16" fill="#ffffff" text-anchor="middle">Traces</text>
    <text x="800" y="640" font-family="Arial, sans-serif" font-size="16" fill="#ffffff" text-anchor="middle">:4317</text>

    <!-- Loki -->
    <rect x="950" y="560" width="200" height="90" fill="#00ACC1" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="1050" y="595" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#ffffff" text-anchor="middle">Loki</text>
    <text x="1050" y="620" font-family="Arial, sans-serif" font-size="16" fill="#ffffff" text-anchor="middle">Logs</text>
    <text x="1050" y="640" font-family="Arial, sans-serif" font-size="16" fill="#ffffff" text-anchor="middle">:3100</text>

    <!-- Prometheus -->
    <rect x="700" y="700" width="200" height="90" fill="#E6522C" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="800" y="735" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#ffffff" text-anchor="middle">Prometheus</text>
    <text x="800" y="760" font-family="Arial, sans-serif" font-size="16" fill="#ffffff" text-anchor="middle">Metrics</text>
    <text x="800" y="780" font-family="Arial, sans-serif" font-size="16" fill="#ffffff" text-anchor="middle">:9090</text>

    <!-- PostgreSQL + Redis -->
    <rect x="100" y="720" width="250" height="100" fill="#336791" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="225" y="755" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#ffffff" text-anchor="middle">PostgreSQL</text>
    <text x="225" y="780" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">+ Redis</text>
    <text x="225" y="800" font-family="Arial, sans-serif" font-size="16" fill="#ffffff" text-anchor="middle">(네이티브)</text>

    <!-- DuckDNS -->
    <rect x="950" y="700" width="200" height="90" fill="#26A69A" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="1050" y="735" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#ffffff" text-anchor="middle">DuckDNS</text>
    <text x="1050" y="760" font-family="Arial, sans-serif" font-size="16" fill="#ffffff" text-anchor="middle">무료 DDNS</text>
    <text x="1050" y="780" font-family="Arial, sans-serif" font-size="16" fill="#ffffff" text-anchor="middle">동적 IP 관리</text>

    <!-- Let's Encrypt -->
    <rect x="950" y="840" width="200" height="90" fill="#7B1FA2" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="1050" y="875" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#ffffff" text-anchor="middle">Let's Encrypt</text>
    <text x="1050" y="900" font-family="Arial, sans-serif" font-size="16" fill="#ffffff" text-anchor="middle">무료 SSL 인증서</text>
    <text x="1050" y="920" font-family="Arial, sans-serif" font-size="16" fill="#ffffff" text-anchor="middle">자동 발급/갱신</text>

    <!-- 화살표 -->
    <!-- 인터넷 → 공유기 -->
    <line x1="700" y1="170" x2="700" y2="200" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- 공유기 → Caddy -->
    <line x1="700" y1="280" x2="700" y2="400" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>
    <text x="720" y="340" font-family="Arial, sans-serif" font-size="18" fill="#333333">80, 443</text>

    <!-- Caddy → Auto-trader -->
    <line x1="600" y1="500" x2="300" y2="560" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>
    <text x="400" y="530" font-family="Arial, sans-serif" font-size="16" fill="#4CAF50">https://domain.com</text>

    <!-- Caddy → Grafana -->
    <line x1="650" y1="500" x2="550" y2="560" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>
    <text x="580" y="530" font-family="Arial, sans-serif" font-size="16" fill="#F46800">/grafana</text>

    <!-- Auto-trader → PostgreSQL -->
    <line x1="225" y1="680" x2="225" y2="720" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- Grafana → Tempo -->
    <line x1="650" y1="610" x2="700" y2="600" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- Grafana → Loki -->
    <line x1="650" y1="630" x2="950" y2="610" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- Grafana → Prometheus -->
    <line x1="600" y1="680" x2="700" y2="700" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- Caddy → Let's Encrypt -->
    <line x1="850" y1="450" x2="950" y2="885" stroke="#7B1FA2" stroke-width="2" stroke-dasharray="5,5" marker-end="url(#arrowhead)"/>
    <text x="880" y="670" font-family="Arial, sans-serif" font-size="16" fill="#7B1FA2">자동 인증서</text>

    <!-- 외부 라벨 -->
    <text x="700" y="910" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#E65100" text-anchor="middle">
        24시간 자동 운영 • 월 $2.50 전기세
    </text>
</svg>"""


def main():
    # 출력 디렉토리
    output_dir = Path(__file__).parent / "images"
    output_dir.mkdir(exist_ok=True)

    print("🎨 배포(Deployment) 시스템 SVG 이미지 생성 중...\n")

    # 1. 썸네일
    print("  1/3 썸네일 SVG 생성 중...")
    thumbnail_path = output_dir / "deployment_thumbnail.svg"
    thumbnail_path.write_text(create_thumbnail_svg(), encoding="utf-8")
    print(f"  ✅ 저장: {thumbnail_path.absolute()}")

    # 2. Before/After 비교
    print("  2/3 Before/After 비교 SVG 생성 중...")
    before_after_path = output_dir / "deployment_before_after.svg"
    before_after_path.write_text(create_before_after_svg(), encoding="utf-8")
    print(f"  ✅ 저장: {before_after_path.absolute()}")

    # 3. 아키텍처 다이어그램
    print("  3/3 아키텍처 다이어그램 SVG 생성 중...")
    architecture_path = output_dir / "deployment_architecture.svg"
    architecture_path.write_text(create_architecture_svg(), encoding="utf-8")
    print(f"  ✅ 저장: {architecture_path.absolute()}")

    print("\n✨ 모든 SVG 이미지 생성 완료!\n")
    print("생성된 SVG:")
    print(f"  - {thumbnail_path.absolute()}")
    print(f"  - {before_after_path.absolute()}")
    print(f"  - {architecture_path.absolute()}")
    print("\n다음 단계:")
    print(f"  python blog/convert_svg_to_png_playwright.py blog/images/deployment_*.svg")


if __name__ == "__main__":
    main()
