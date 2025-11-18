#!/usr/bin/env python3
"""
모니터링 시스템 블로그 SVG 이미지 생성 스크립트

SVG 형식으로 이미지를 생성한 후 convert_monitoring_svg_to_png.py로 PNG 변환

사용법:
    python blog/generate_monitoring_svg.py
    python blog/convert_monitoring_svg_to_png.py

생성되는 SVG:
    - blog/images/monitoring_thumbnail.svg (1200x630)
    - blog/images/before_after_monitoring.svg (1200x800)
    - blog/images/monitoring_architecture.svg (1400x1000)
"""

from pathlib import Path


def create_thumbnail_svg() -> str:
    """썸네일 이미지 SVG 생성 (1200x630)"""
    return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="630" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <linearGradient id="bgGradient" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" style="stop-color:#0f3460;stop-opacity:1" />
            <stop offset="50%" style="stop-color:#16213e;stop-opacity:1" />
            <stop offset="100%" style="stop-color:#1a1a2e;stop-opacity:1" />
        </linearGradient>
    </defs>

    <!-- 배경 -->
    <rect width="1200" height="630" fill="url(#bgGradient)"/>

    <!-- 제목 -->
    <text x="600" y="150" font-family="Arial, sans-serif" font-size="60" font-weight="bold" fill="#ffffff" text-anchor="middle">
        실전 운영을 위한
    </text>
    <text x="600" y="240" font-family="Arial, sans-serif" font-size="60" font-weight="bold" fill="#ffffff" text-anchor="middle">
        모니터링 시스템 구축
    </text>

    <!-- 부제목 -->
    <text x="600" y="340" font-family="Arial, sans-serif" font-size="35" fill="#00d4ff" text-anchor="middle">
        OpenTelemetry + Grafana 관찰성 스택
    </text>

    <!-- 하단 텍스트 -->
    <text x="600" y="480" font-family="Arial, sans-serif" font-size="25" fill="#a8dadc" text-anchor="middle">
        Grafana • Tempo • Loki • Prometheus
    </text>

    <!-- 아이콘 원들 -->
    <circle cx="250" cy="540" r="25" fill="#F46800"/>
    <circle cx="450" cy="540" r="25" fill="#E91E63"/>
    <circle cx="650" cy="540" r="25" fill="#00ACC1"/>
    <circle cx="850" cy="540" r="25" fill="#E6522C"/>
</svg>"""


def create_before_after_svg() -> str:
    """모니터링 전후 비교 SVG 생성 (1200x800)"""
    return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="800" xmlns="http://www.w3.org/2000/svg">
    <!-- 배경 -->
    <rect width="1200" height="800" fill="#f8f9fa"/>

    <!-- 제목 -->
    <text x="600" y="60" font-family="Arial, sans-serif" font-size="50" font-weight="bold" fill="#1a1a2e" text-anchor="middle">
        모니터링 시스템 구축 전 vs 후
    </text>

    <!-- Before 섹션 -->
    <text x="300" y="160" font-family="Arial, sans-serif" font-size="40" font-weight="bold" fill="#e63946" text-anchor="middle">
        ❌ Before
    </text>
    <rect x="50" y="200" width="500" height="550" fill="none" stroke="#e63946" stroke-width="3"/>

    <text x="300" y="260" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 에러 발견: 6시간 후
    </text>
    <text x="300" y="330" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 서버 접속해서 로그 확인
    </text>
    <text x="300" y="400" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 성능 저하 인지 불가
    </text>
    <text x="300" y="470" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 문제 원인 파악 어려움
    </text>
    <text x="300" y="540" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 불안한 운영
    </text>
    <text x="300" y="610" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 수동 모니터링 필요
    </text>
    <text x="300" y="680" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 커피 마시며 불안 😰
    </text>

    <!-- After 섹션 -->
    <text x="900" y="160" font-family="Arial, sans-serif" font-size="40" font-weight="bold" fill="#06d6a0" text-anchor="middle">
        ✅ After
    </text>
    <rect x="650" y="200" width="500" height="550" fill="none" stroke="#06d6a0" stroke-width="3"/>

    <text x="900" y="260" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 에러 발견: 1초 이내
    </text>
    <text x="900" y="330" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • Telegram 즉시 알림
    </text>
    <text x="900" y="400" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • Grafana로 실시간 확인
    </text>
    <text x="900" y="470" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • Trace로 원인 즉시 파악
    </text>
    <text x="900" y="540" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 안심하고 운영
    </text>
    <text x="900" y="610" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 자동 모니터링
    </text>
    <text x="900" y="680" font-family="Arial, sans-serif" font-size="22" fill="#333333" text-anchor="middle">
        • 커피 마시며 여유 ☕
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
        <marker id="arrowheadRed" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#E91E63" />
        </marker>
        <marker id="arrowheadBlue" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#00ACC1" />
        </marker>
        <marker id="arrowheadOrange" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#E6522C" />
        </marker>
        <marker id="arrowheadTeal" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#26A69A" />
        </marker>
    </defs>

    <!-- 배경 -->
    <rect width="1400" height="1000" fill="#ffffff"/>

    <!-- 제목 -->
    <text x="700" y="50" font-family="Arial, sans-serif" font-size="40" font-weight="bold" fill="#1a1a2e" text-anchor="middle">
        Grafana 관찰성 스택 아키텍처
    </text>

    <!-- FastAPI Layer -->
    <!-- 그림자 -->
    <rect x="55" y="125" width="280" height="150" fill="#cccccc" rx="5"/>
    <rect x="50" y="120" width="280" height="150" fill="#4CAF50" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="190" y="180" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#ffffff" text-anchor="middle">FastAPI App</text>
    <text x="190" y="210" font-family="Arial, sans-serif" font-size="20" fill="#ffffff" text-anchor="middle">+ Middleware</text>

    <rect x="55" y="305" width="280" height="80" fill="#cccccc" rx="5"/>
    <rect x="50" y="300" width="280" height="80" fill="#66BB6A" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="190" y="350" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#ffffff" text-anchor="middle">TelemetryManager</text>

    <rect x="55" y="405" width="280" height="80" fill="#cccccc" rx="5"/>
    <rect x="50" y="400" width="280" height="80" fill="#81C784" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="190" y="450" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#ffffff" text-anchor="middle">ErrorReporter</text>

    <!-- OTLP Layer -->
    <rect x="425" y="255" width="200" height="100" fill="#cccccc" rx="5"/>
    <rect x="420" y="250" width="200" height="100" fill="#9C27B0" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="520" y="290" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">OTLP Exporter</text>
    <text x="520" y="320" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">gRPC: 4317</text>

    <!-- Grafana Stack Layer -->
    <rect x="755" y="125" width="200" height="120" fill="#cccccc" rx="5"/>
    <rect x="750" y="120" width="200" height="120" fill="#F46800" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="850" y="160" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">Grafana</text>
    <text x="850" y="185" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">Dashboard</text>
    <text x="850" y="210" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">:3000</text>

    <rect x="1005" y="125" width="200" height="120" fill="#cccccc" rx="5"/>
    <rect x="1000" y="120" width="200" height="120" fill="#E91E63" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="1100" y="160" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">Tempo</text>
    <text x="1100" y="185" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">Traces</text>
    <text x="1100" y="210" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">:3200</text>

    <rect x="755" y="285" width="200" height="120" fill="#cccccc" rx="5"/>
    <rect x="750" y="280" width="200" height="120" fill="#00ACC1" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="850" y="320" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">Loki</text>
    <text x="850" y="345" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">Logs</text>
    <text x="850" y="370" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">:3100</text>

    <rect x="1005" y="285" width="200" height="120" fill="#cccccc" rx="5"/>
    <rect x="1000" y="280" width="200" height="120" fill="#E6522C" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="1100" y="320" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">Prometheus</text>
    <text x="1100" y="345" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">Metrics</text>
    <text x="1100" y="370" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">:9090</text>

    <rect x="880" y="445" width="200" height="100" fill="#cccccc" rx="5"/>
    <rect x="875" y="440" width="200" height="100" fill="#26A69A" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="975" y="480" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">Promtail</text>
    <text x="975" y="510" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">Log Collector</text>

    <!-- Docker Layer -->
    <rect x="880" y="585" width="200" height="80" fill="#cccccc" rx="5"/>
    <rect x="875" y="580" width="200" height="80" fill="#607D8B" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="975" y="610" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">Docker</text>
    <text x="975" y="640" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">Containers</text>

    <!-- Telegram Layer -->
    <rect x="55" y="525" width="280" height="80" fill="#cccccc" rx="5"/>
    <rect x="50" y="520" width="280" height="80" fill="#0088CC" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="190" y="570" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#ffffff" text-anchor="middle">Telegram Bot</text>

    <!-- 화살표들 -->
    <!-- FastAPI -> OTLP -->
    <line x1="330" y1="195" x2="420" y2="280" stroke="#333333" stroke-width="3" marker-end="url(#arrowhead)"/>
    <line x1="330" y1="340" x2="420" y2="310" stroke="#333333" stroke-width="3" marker-end="url(#arrowhead)"/>

    <!-- OTLP -> Grafana Stack -->
    <line x1="620" y1="280" x2="750" y2="180" stroke="#E91E63" stroke-width="3" marker-end="url(#arrowheadRed)"/>
    <line x1="620" y1="300" x2="1000" y2="180" stroke="#E91E63" stroke-width="3" marker-end="url(#arrowheadRed)"/>
    <line x1="620" y1="310" x2="1000" y2="340" stroke="#E6522C" stroke-width="3" marker-end="url(#arrowheadOrange)"/>

    <!-- ErrorReporter -> Telegram -->
    <line x1="190" y1="480" x2="190" y2="520" stroke="#0088CC" stroke-width="3" marker-end="url(#arrowhead)"/>

    <!-- Docker -> Promtail -> Loki -->
    <line x1="975" y1="580" x2="975" y2="540" stroke="#26A69A" stroke-width="3" marker-end="url(#arrowheadTeal)"/>
    <line x1="975" y1="440" x2="950" y2="400" stroke="#00ACC1" stroke-width="3" marker-end="url(#arrowheadBlue)"/>

    <!-- Grafana connections (점선) -->
    <line x1="950" y1="180" x2="1000" y2="180" stroke="#666666" stroke-width="2" stroke-dasharray="5,5"/>
    <line x1="850" y1="240" x2="850" y2="280" stroke="#666666" stroke-width="2" stroke-dasharray="5,5"/>
    <line x1="950" y1="240" x2="1100" y2="280" stroke="#666666" stroke-width="2" stroke-dasharray="5,5"/>

    <!-- 범례 -->
    <text x="700" y="730" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#333333" text-anchor="middle">
        핵심 기능:
    </text>

    <text x="700" y="770" font-family="Arial, sans-serif" font-size="18" fill="#555555" text-anchor="middle">
        • Trace-to-Log 연동으로 트레이스와 로그 통합
    </text>
    <text x="700" y="800" font-family="Arial, sans-serif" font-size="18" fill="#555555" text-anchor="middle">
        • Promtail이 Docker 로그 자동 수집
    </text>
    <text x="700" y="830" font-family="Arial, sans-serif" font-size="18" fill="#555555" text-anchor="middle">
        • Telegram으로 실시간 에러 알림
    </text>
    <text x="700" y="860" font-family="Arial, sans-serif" font-size="18" fill="#555555" text-anchor="middle">
        • Raspberry Pi 5 최적화 (CPU/메모리 제한)
    </text>

    <!-- 하단 주석 -->
    <text x="700" y="920" font-family="Arial, sans-serif" font-size="16" fill="#999999" text-anchor="middle">
        OpenTelemetry 표준 사용으로 벤더 종속 없음
    </text>
    <text x="700" y="950" font-family="Arial, sans-serif" font-size="16" fill="#999999" text-anchor="middle">
        SigNoz에서 Grafana Stack으로 마이그레이션 (환경 변수만 변경)
    </text>
</svg>"""


def main():
    """메인 함수"""
    # images 디렉토리 확인
    images_dir = Path(__file__).parent / 'images'
    images_dir.mkdir(exist_ok=True)

    print("🎨 모니터링 시스템 SVG 이미지 생성 중...\n")

    # 1. 썸네일 생성
    print("  1/3 썸네일 SVG 생성 중...")
    thumbnail_path = images_dir / 'monitoring_thumbnail.svg'
    thumbnail_path.write_text(create_thumbnail_svg(), encoding='utf-8')
    print(f"  ✅ 저장: {thumbnail_path}")

    # 2. Before/After 비교 생성
    print("  2/3 Before/After 비교 SVG 생성 중...")
    before_after_path = images_dir / 'before_after_monitoring.svg'
    before_after_path.write_text(create_before_after_svg(), encoding='utf-8')
    print(f"  ✅ 저장: {before_after_path}")

    # 3. 아키텍처 다이어그램 생성
    print("  3/3 아키텍처 다이어그램 SVG 생성 중...")
    architecture_path = images_dir / 'monitoring_architecture.svg'
    architecture_path.write_text(create_architecture_svg(), encoding='utf-8')
    print(f"  ✅ 저장: {architecture_path}")

    print("\n✨ 모든 SVG 이미지 생성 완료!")
    print("\n생성된 SVG:")
    print(f"  - {thumbnail_path}")
    print(f"  - {before_after_path}")
    print(f"  - {architecture_path}")
    print("\n다음 단계:")
    print("  python blog/convert_monitoring_svg_to_png.py")


if __name__ == '__main__':
    main()
