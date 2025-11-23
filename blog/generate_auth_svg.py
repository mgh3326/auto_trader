#!/usr/bin/env python3
"""
인증(Authentication) 시스템 블로그 SVG 이미지 생성 스크립트

SVG 형식으로 이미지를 생성한 후 convert_svg_to_png_playwright.py로 PNG 변환

사용법:
    python blog/generate_auth_svg.py
    python blog/convert_auth_svg_to_png.py

생성되는 SVG:
    - blog/images/auth_thumbnail.svg (1200x630)
    - blog/images/auth_architecture.svg (1400x1000)
    - blog/images/auth_role_hierarchy.svg (1200x600)
"""

from pathlib import Path


def create_thumbnail_svg() -> str:
    """썸네일 이미지 SVG 생성 (1200x630)"""
    return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="630" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <linearGradient id="bgGradient" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" style="stop-color:#1a1a2e;stop-opacity:1" />
            <stop offset="50%" style="stop-color:#16213e;stop-opacity:1" />
            <stop offset="100%" style="stop-color:#0f3460;stop-opacity:1" />
        </linearGradient>
    </defs>

    <!-- 배경 -->
    <rect width="1200" height="630" fill="url(#bgGradient)"/>

    <!-- 제목 -->
    <text x="600" y="150" font-family="Arial, sans-serif" font-size="55" font-weight="bold" fill="#ffffff" text-anchor="middle">
        JWT 인증 시스템으로
    </text>
    <text x="600" y="230" font-family="Arial, sans-serif" font-size="55" font-weight="bold" fill="#ffffff" text-anchor="middle">
        안전한 웹 애플리케이션 구축
    </text>

    <!-- 부제목 -->
    <text x="600" y="330" font-family="Arial, sans-serif" font-size="35" fill="#00d4ff" text-anchor="middle">
        회원가입부터 역할 기반 접근 제어까지
    </text>

    <!-- 아이콘 영역 -->
    <circle cx="200" cy="500" r="30" fill="#4CAF50"/>  <!-- JWT -->
    <text x="200" y="515" font-family="Arial, sans-serif" font-size="35" font-weight="bold" fill="#ffffff" text-anchor="middle">🔐</text>

    <circle cx="400" cy="500" r="30" fill="#2196F3"/>  <!-- bcrypt -->
    <text x="400" y="515" font-family="Arial, sans-serif" font-size="35" font-weight="bold" fill="#ffffff" text-anchor="middle">🔒</text>

    <circle cx="600" cy="500" r="30" fill="#FF9800"/>  <!-- Redis -->
    <text x="600" y="515" font-family="Arial, sans-serif" font-size="35" font-weight="bold" fill="#ffffff" text-anchor="middle">⚡</text>

    <circle cx="800" cy="500" r="30" fill="#9C27B0"/>  <!-- RBAC -->
    <text x="800" y="515" font-family="Arial, sans-serif" font-size="35" font-weight="bold" fill="#ffffff" text-anchor="middle">👥</text>

    <circle cx="1000" cy="500" r="30" fill="#F44336"/>  <!-- Rate Limit -->
    <text x="1000" y="515" font-family="Arial, sans-serif" font-size="35" font-weight="bold" fill="#ffffff" text-anchor="middle">🛡️</text>

    <!-- 하단 텍스트 -->
    <text x="600" y="580" font-family="Arial, sans-serif" font-size="22" fill="#a8dadc" text-anchor="middle">
        JWT • bcrypt • Redis • RBAC • Rate Limiting
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
        JWT 인증 시스템 아키텍처
    </text>

    <!-- 사용자 -->
    <ellipse cx="700" cy="130" rx="80" ry="40" fill="#E0E0E0" stroke="#333333" stroke-width="2"/>
    <text x="700" y="140" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#333333" text-anchor="middle">사용자</text>

    <!-- FastAPI 애플리케이션 영역 -->
    <rect x="50" y="220" width="1300" height="750" fill="#FFF3E0" stroke="#FF9800" stroke-width="3" rx="10"/>
    <text x="700" y="260" font-family="Arial, sans-serif" font-size="28" font-weight="bold" fill="#FF9800" text-anchor="middle">FastAPI 애플리케이션</text>

    <!-- 인증 라우터 -->
    <rect x="100" y="300" width="300" height="200" fill="#4CAF50" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="250" y="340" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#ffffff" text-anchor="middle">인증 라우터</text>
    <text x="250" y="370" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">/auth/register</text>
    <text x="250" y="400" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">/auth/login</text>
    <text x="250" y="430" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">/auth/refresh</text>
    <text x="250" y="460" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">/auth/logout</text>

    <!-- JWT 인증 미들웨어 -->
    <rect x="450" y="300" width="300" height="120" fill="#2196F3" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="600" y="340" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">JWT 인증 미들웨어</text>
    <text x="600" y="370" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">토큰 검증</text>
    <text x="600" y="395" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">사용자 인증</text>

    <!-- RBAC (역할 기반 접근 제어) -->
    <rect x="800" y="300" width="280" height="120" fill="#9C27B0" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="940" y="335" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">RBAC</text>
    <text x="940" y="360" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">역할 기반</text>
    <text x="940" y="385" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">접근 제어</text>

    <!-- 보호된 API 엔드포인트 -->
    <rect x="450" y="460" width="630" height="150" fill="#FF9800" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="765" y="500" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#ffffff" text-anchor="middle">보호된 API 엔드포인트</text>
    <text x="765" y="535" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">/api/stocks (Viewer+)</text>
    <text x="765" y="565" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">/api/analyze (Analyst+)</text>
    <text x="765" y="595" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">/admin/* (Admin)</text>

    <!-- PostgreSQL -->
    <rect x="100" y="650" width="250" height="100" fill="#336791" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="225" y="690" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">PostgreSQL</text>
    <text x="225" y="720" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">사용자 정보</text>

    <!-- Redis -->
    <rect x="400" y="650" width="250" height="100" fill="#DC382D" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="525" y="690" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">Redis</text>
    <text x="525" y="720" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">Refresh Token</text>

    <!-- bcrypt -->
    <rect x="700" y="650" width="250" height="100" fill="#F44336" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="825" y="690" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">bcrypt</text>
    <text x="825" y="720" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">비밀번호 해싱</text>

    <!-- Rate Limiting -->
    <rect x="1000" y="650" width="280" height="100" fill="#795548" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="1140" y="690" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#ffffff" text-anchor="middle">Rate Limiting</text>
    <text x="1140" y="720" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">무차별 대입 방어</text>

    <!-- 화살표 -->
    <!-- 사용자 → 인증 라우터 -->
    <line x1="700" y1="170" x2="250" y2="300" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- 인증 라우터 → PostgreSQL -->
    <line x1="250" y1="500" x2="225" y2="650" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- 인증 라우터 → Redis -->
    <line x1="350" y1="500" x2="500" y2="650" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- 인증 라우터 → bcrypt -->
    <line x1="400" y1="450" x2="750" y2="650" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- JWT 미들웨어 → RBAC -->
    <line x1="750" y1="360" x2="800" y2="360" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- RBAC → 보호된 API -->
    <line x1="940" y1="420" x2="765" y2="460" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- Rate Limiting -->
    <line x1="400" y1="330" x2="1000" y2="700" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)" stroke-dasharray="5,5"/>

    <!-- 하단 설명 -->
    <text x="700" y="920" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#FF9800" text-anchor="middle">
        JWT + Redis + bcrypt + RBAC + Rate Limiting
    </text>
    <text x="700" y="955" font-family="Arial, sans-serif" font-size="18" fill="#666666" text-anchor="middle">
        상태 비저장(Stateless) + 강제 로그아웃 + 역할 기반 접근 제어
    </text>
</svg>"""


def create_role_hierarchy_svg() -> str:
    """역할 계층 구조 SVG 생성 (1200x600)"""
    return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="600" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#4CAF50" />
        </marker>
    </defs>

    <!-- 배경 -->
    <rect width="1200" height="600" fill="#f8f9fa"/>

    <!-- 제목 -->
    <text x="600" y="50" font-family="Arial, sans-serif" font-size="40" font-weight="bold" fill="#1a1a2e" text-anchor="middle">
        역할 기반 접근 제어 (RBAC) 계층 구조
    </text>

    <!-- Admin (최상위) -->
    <rect x="500" y="120" width="200" height="80" fill="#F44336" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="600" y="155" font-family="Arial, sans-serif" font-size="26" font-weight="bold" fill="#ffffff" text-anchor="middle">Admin</text>
    <text x="600" y="180" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">모든 권한</text>

    <!-- Trader -->
    <rect x="500" y="250" width="200" height="80" fill="#FF9800" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="600" y="285" font-family="Arial, sans-serif" font-size="26" font-weight="bold" fill="#ffffff" text-anchor="middle">Trader</text>
    <text x="600" y="310" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">거래 실행</text>

    <!-- Analyst -->
    <rect x="500" y="380" width="200" height="80" fill="#2196F3" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="600" y="415" font-family="Arial, sans-serif" font-size="26" font-weight="bold" fill="#ffffff" text-anchor="middle">Analyst</text>
    <text x="600" y="440" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">분석 조회</text>

    <!-- Viewer (최하위) -->
    <rect x="500" y="510" width="200" height="80" fill="#4CAF50" stroke="#333333" stroke-width="2" rx="5"/>
    <text x="600" y="545" font-family="Arial, sans-serif" font-size="26" font-weight="bold" fill="#ffffff" text-anchor="middle">Viewer</text>
    <text x="600" y="570" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">읽기 전용</text>

    <!-- 화살표 (권한 상속) -->
    <line x1="600" y1="200" x2="600" y2="250" stroke="#4CAF50" stroke-width="3" marker-end="url(#arrowhead)"/>
    <line x1="600" y1="330" x2="600" y2="380" stroke="#4CAF50" stroke-width="3" marker-end="url(#arrowhead)"/>
    <line x1="600" y1="460" x2="600" y2="510" stroke="#4CAF50" stroke-width="3" marker-end="url(#arrowhead)"/>

    <!-- 권한 설명 (오른쪽) -->
    <text x="800" y="160" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#F44336" text-anchor="start">
        • 사용자 관리
    </text>
    <text x="800" y="190" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#F44336" text-anchor="start">
        • 시스템 설정
    </text>

    <text x="800" y="290" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#FF9800" text-anchor="start">
        • 매수/매도 실행
    </text>
    <text x="800" y="320" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#FF9800" text-anchor="start">
        + Analyst 권한
    </text>

    <text x="800" y="420" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#2196F3" text-anchor="start">
        • AI 분석 요청
    </text>
    <text x="800" y="450" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#2196F3" text-anchor="start">
        + Viewer 권한
    </text>

    <text x="800" y="550" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#4CAF50" text-anchor="start">
        • 종목 조회
    </text>
    <text x="800" y="580" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#4CAF50" text-anchor="start">
        • 대시보드 확인
    </text>

    <!-- 권한 설명 (왼쪽) -->
    <text x="50" y="160" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#666666" text-anchor="start">
        계층: 3
    </text>

    <text x="50" y="290" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#666666" text-anchor="start">
        계층: 2
    </text>

    <text x="50" y="420" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#666666" text-anchor="start">
        계층: 1
    </text>

    <text x="50" y="550" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#666666" text-anchor="start">
        계층: 0
    </text>
</svg>"""


def main():
    # 출력 디렉토리
    output_dir = Path(__file__).parent / "images"
    output_dir.mkdir(exist_ok=True)

    print("🎨 인증(Authentication) 시스템 SVG 이미지 생성 중...\n")

    # 1. 썸네일
    print("  1/3 썸네일 SVG 생성 중...")
    thumbnail_path = output_dir / "auth_thumbnail.svg"
    thumbnail_path.write_text(create_thumbnail_svg(), encoding="utf-8")
    print(f"  ✅ 저장: {thumbnail_path.absolute()}")

    # 2. 아키텍처 다이어그램
    print("  2/3 아키텍처 다이어그램 SVG 생성 중...")
    architecture_path = output_dir / "auth_architecture.svg"
    architecture_path.write_text(create_architecture_svg(), encoding="utf-8")
    print(f"  ✅ 저장: {architecture_path.absolute()}")

    # 3. 역할 계층 구조
    print("  3/3 역할 계층 구조 SVG 생성 중...")
    role_hierarchy_path = output_dir / "auth_role_hierarchy.svg"
    role_hierarchy_path.write_text(create_role_hierarchy_svg(), encoding="utf-8")
    print(f"  ✅ 저장: {role_hierarchy_path.absolute()}")

    print("\n✨ 모든 SVG 이미지 생성 완료!\n")
    print("생성된 SVG:")
    print(f"  - {thumbnail_path.absolute()}")
    print(f"  - {architecture_path.absolute()}")
    print(f"  - {role_hierarchy_path.absolute()}")
    print("\n다음 단계:")
    print(f"  python blog/convert_auth_svg_to_png.py")


if __name__ == "__main__":
    main()
