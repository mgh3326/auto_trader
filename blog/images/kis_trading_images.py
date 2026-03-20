#!/usr/bin/env python3
"""
KIS 자동 매매 블로그 이미지 생성기

사용법:
    python blog/images/kis_trading_images.py
"""

import sys
from pathlib import Path

# 모듈 경로 추가
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from blog.tools.image_generator import BlogImageGenerator, ThumbnailTemplate


class KISTradingImages(BlogImageGenerator):
    """KIS 자동 매매 블로그 이미지 생성기"""

    def get_images(self):
        return [
            ("thumbnail", 1200, 630, self.create_thumbnail),
            ("architecture", 1400, 900, self.create_architecture),
            ("buy_flow", 1200, 700, self.create_buy_flow),
            ("erd", 1200, 600, self.create_erd),
            ("dashboard_domestic", 1400, 800, self.create_dashboard),
            ("progress", 1200, 400, self.create_progress),
            ("flower", 1200, 600, self.create_flower),
        ]

    def create_thumbnail(self) -> str:
        """썸네일 이미지 (1200x630)"""
        return ThumbnailTemplate.create(
            title_line1="KIS 국내/해외 주식",
            title_line2="자동 매매 시스템",
            subtitle="Celery + AI 분석 기반 스마트 트레이딩",
            icons=[
                ("🤖", "AI 분석", "#2196F3"),
                ("⚡", "Celery", "#8BC34A"),
                ("📈", "자동 매매", "#FF9800"),
                ("📱", "Telegram", "#9C27B0"),
            ],
            tech_stack="FastAPI • Celery • Redis • PostgreSQL • KIS API • Google Gemini",
            bg_gradient=("#0d1b2a", "#1b263b", "#415a77"),
            accent_color="#f4d03f",
        )

    def create_architecture(self) -> str:
        """아키텍처 다이어그램 (1400x900)"""
        return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1400" height="900" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#666666" />
        </marker>
    </defs>

    <!-- 배경 -->
    <rect width="1400" height="900" fill="#f8f9fa"/>

    <!-- 제목 -->
    <text x="700" y="45" font-family="Arial, sans-serif" font-size="32" font-weight="bold" fill="#1a1a2e" text-anchor="middle">
        KIS 자동 매매 시스템 아키텍처
    </text>

    <!-- 사용자 -->
    <ellipse cx="700" cy="100" rx="70" ry="35" fill="#E0E0E0" stroke="#666666" stroke-width="2"/>
    <text x="700" y="108" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#333333" text-anchor="middle">사용자</text>

    <!-- 화살표: 사용자 -> 대시보드 -->
    <line x1="700" y1="135" x2="700" y2="165" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- 웹 대시보드 -->
    <rect x="500" y="170" width="400" height="80" fill="#2196F3" stroke="#1565C0" stroke-width="2" rx="8"/>
    <text x="700" y="200" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#ffffff" text-anchor="middle">웹 대시보드 (FastAPI + Jinja2)</text>
    <text x="700" y="225" font-family="Arial, sans-serif" font-size="14" fill="#E3F2FD" text-anchor="middle">/kis-domestic-trading/ • /kis-overseas-trading/</text>

    <!-- 화살표: 대시보드 -> API -->
    <line x1="700" y1="250" x2="700" y2="280" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- FastAPI 라우터 -->
    <rect x="450" y="285" width="500" height="120" fill="#4CAF50" stroke="#2E7D32" stroke-width="2" rx="8"/>
    <text x="700" y="315" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#ffffff" text-anchor="middle">FastAPI 라우터</text>
    <text x="540" y="345" font-family="Arial, sans-serif" font-size="12" fill="#E8F5E9" text-anchor="middle">보유주식 조회</text>
    <text x="640" y="345" font-family="Arial, sans-serif" font-size="12" fill="#E8F5E9" text-anchor="middle">AI 분석</text>
    <text x="740" y="345" font-family="Arial, sans-serif" font-size="12" fill="#E8F5E9" text-anchor="middle">매수/매도</text>
    <text x="840" y="345" font-family="Arial, sans-serif" font-size="12" fill="#E8F5E9" text-anchor="middle">종목설정</text>
    <text x="700" y="385" font-family="Arial, sans-serif" font-size="11" fill="#C8E6C9" text-anchor="middle">kis_domestic_trading.py • kis_overseas_trading.py • symbol_settings.py</text>

    <!-- 화살표: API -> Celery -->
    <line x1="700" y1="405" x2="700" y2="435" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- Celery 태스크 영역 -->
    <rect x="200" y="440" width="1000" height="160" fill="#FF9800" stroke="#EF6C00" stroke-width="2" rx="8"/>
    <text x="700" y="470" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#ffffff" text-anchor="middle">Celery 비동기 태스크</text>

    <!-- Celery 태스크 박스들 -->
    <rect x="230" y="490" width="180" height="90" fill="#FFB74D" stroke="#EF6C00" stroke-width="1" rx="5"/>
    <text x="320" y="515" font-family="Arial, sans-serif" font-size="13" font-weight="bold" fill="#333333" text-anchor="middle">전체 종목 분석</text>
    <text x="320" y="535" font-family="Arial, sans-serif" font-size="11" fill="#666666" text-anchor="middle">analyze_stocks</text>
    <text x="320" y="555" font-family="Arial, sans-serif" font-size="10" fill="#888888" text-anchor="middle">진행상황 업데이트</text>

    <rect x="430" y="490" width="180" height="90" fill="#FFB74D" stroke="#EF6C00" stroke-width="1" rx="5"/>
    <text x="520" y="515" font-family="Arial, sans-serif" font-size="13" font-weight="bold" fill="#333333" text-anchor="middle">전체 종목 매수</text>
    <text x="520" y="535" font-family="Arial, sans-serif" font-size="11" fill="#666666" text-anchor="middle">execute_buy_orders</text>
    <text x="520" y="555" font-family="Arial, sans-serif" font-size="10" fill="#888888" text-anchor="middle">분할 매수 주문</text>

    <rect x="630" y="490" width="180" height="90" fill="#FFB74D" stroke="#EF6C00" stroke-width="1" rx="5"/>
    <text x="720" y="515" font-family="Arial, sans-serif" font-size="13" font-weight="bold" fill="#333333" text-anchor="middle">전체 종목 매도</text>
    <text x="720" y="535" font-family="Arial, sans-serif" font-size="11" fill="#666666" text-anchor="middle">execute_sell_orders</text>
    <text x="720" y="555" font-family="Arial, sans-serif" font-size="10" fill="#888888" text-anchor="middle">수익 목표 기반</text>

    <rect x="830" y="490" width="180" height="90" fill="#FFB74D" stroke="#EF6C00" stroke-width="1" rx="5"/>
    <text x="920" y="515" font-family="Arial, sans-serif" font-size="13" font-weight="bold" fill="#333333" text-anchor="middle">종목별 자동실행</text>
    <text x="920" y="535" font-family="Arial, sans-serif" font-size="11" fill="#666666" text-anchor="middle">per_stock_automation</text>
    <text x="920" y="555" font-family="Arial, sans-serif" font-size="10" fill="#888888" text-anchor="middle">분석→매수→매도</text>

    <rect x="1030" y="490" width="150" height="90" fill="#FFB74D" stroke="#EF6C00" stroke-width="1" rx="5"/>
    <text x="1105" y="515" font-family="Arial, sans-serif" font-size="13" font-weight="bold" fill="#333333" text-anchor="middle">개별 종목</text>
    <text x="1105" y="535" font-family="Arial, sans-serif" font-size="11" fill="#666666" text-anchor="middle">단일 분석/매수/매도</text>
    <text x="1105" y="555" font-family="Arial, sans-serif" font-size="10" fill="#888888" text-anchor="middle">즉시 실행</text>

    <!-- 화살표: Celery -> 서비스 -->
    <line x1="500" y1="600" x2="500" y2="650" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>
    <line x1="900" y1="600" x2="900" y2="650" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- 서비스 레이어 -->
    <rect x="200" y="655" width="400" height="100" fill="#9C27B0" stroke="#6A1B9A" stroke-width="2" rx="8"/>
    <text x="400" y="690" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#ffffff" text-anchor="middle">KIS API + AI 분석</text>
    <text x="300" y="720" font-family="Arial, sans-serif" font-size="12" fill="#E1BEE7" text-anchor="middle">KISClient</text>
    <text x="400" y="720" font-family="Arial, sans-serif" font-size="12" fill="#E1BEE7" text-anchor="middle">KISAnalyzer</text>
    <text x="500" y="720" font-family="Arial, sans-serif" font-size="12" fill="#E1BEE7" text-anchor="middle">YahooAnalyzer</text>
    <text x="400" y="745" font-family="Arial, sans-serif" font-size="11" fill="#CE93D8" text-anchor="middle">한국투자증권 API • Google Gemini</text>

    <rect x="800" y="655" width="400" height="100" fill="#607D8B" stroke="#37474F" stroke-width="2" rx="8"/>
    <text x="1000" y="690" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#ffffff" text-anchor="middle">PostgreSQL + Redis</text>
    <text x="900" y="720" font-family="Arial, sans-serif" font-size="12" fill="#CFD8DC" text-anchor="middle">StockAnalysisResult</text>
    <text x="1100" y="720" font-family="Arial, sans-serif" font-size="12" fill="#CFD8DC" text-anchor="middle">SymbolTradeSettings</text>
    <text x="1000" y="745" font-family="Arial, sans-serif" font-size="11" fill="#B0BEC5" text-anchor="middle">분석 결과 • 종목별 설정 • 태스크 큐</text>

    <!-- Telegram 알림 -->
    <rect x="1150" y="440" width="100" height="160" fill="#03A9F4" stroke="#0277BD" stroke-width="2" rx="8"/>
    <text x="1200" y="475" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#ffffff" text-anchor="middle">Telegram</text>
    <text x="1200" y="500" font-family="Arial, sans-serif" font-size="30" fill="#ffffff" text-anchor="middle">📱</text>
    <text x="1200" y="530" font-family="Arial, sans-serif" font-size="10" fill="#E1F5FE" text-anchor="middle">분석 완료</text>
    <text x="1200" y="545" font-family="Arial, sans-serif" font-size="10" fill="#E1F5FE" text-anchor="middle">주문 접수</text>
    <text x="1200" y="560" font-family="Arial, sans-serif" font-size="10" fill="#E1F5FE" text-anchor="middle">에러 알림</text>

    <!-- 화살표: Celery -> Telegram -->
    <line x1="1100" y1="520" x2="1145" y2="520" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- 하단 설명 -->
    <text x="700" y="830" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">
        웹 요청 → FastAPI → Celery 태스크 → KIS API/AI 분석 → DB 저장 → Telegram 알림
    </text>
    <text x="700" y="855" font-family="Arial, sans-serif" font-size="12" fill="#999999" text-anchor="middle">
        비동기 처리로 오래 걸리는 작업도 즉시 응답, 진행 상황 실시간 폴링
    </text>
</svg>"""

    def create_buy_flow(self) -> str:
        """매수 로직 플로우 다이어그램 (1200x700)"""
        return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="700" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#333333" />
        </marker>
    </defs>

    <!-- 배경 -->
    <rect width="1200" height="700" fill="#ffffff"/>

    <!-- 제목 -->
    <text x="600" y="40" font-family="Arial, sans-serif" font-size="28" font-weight="bold" fill="#1a1a2e" text-anchor="middle">
        AI 분석 기반 분할 매수 플로우
    </text>

    <!-- Step 1: 1% 조건 확인 -->
    <rect x="100" y="80" width="220" height="80" fill="#E3F2FD" stroke="#1976D2" stroke-width="2" rx="8"/>
    <text x="210" y="110" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#1565C0" text-anchor="middle">1. 1% 조건 확인</text>
    <text x="210" y="135" font-family="Arial, sans-serif" font-size="11" fill="#666666" text-anchor="middle">현재가 &lt; 평균매수가 × 0.99</text>

    <!-- 화살표 -->
    <line x1="320" y1="120" x2="380" y2="120" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>
    <text x="350" y="110" font-family="Arial, sans-serif" font-size="10" fill="#4CAF50">YES</text>

    <!-- Step 2: AI 분석 결과 조회 -->
    <rect x="390" y="80" width="220" height="80" fill="#E8F5E9" stroke="#388E3C" stroke-width="2" rx="8"/>
    <text x="500" y="110" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#2E7D32" text-anchor="middle">2. AI 분석 결과 조회</text>
    <text x="500" y="135" font-family="Arial, sans-serif" font-size="11" fill="#666666" text-anchor="middle">DB에서 최신 분석 조회</text>

    <!-- 화살표 -->
    <line x1="610" y1="120" x2="670" y2="120" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- Step 3: 종목 설정 확인 -->
    <rect x="680" y="80" width="220" height="80" fill="#FFF3E0" stroke="#F57C00" stroke-width="2" rx="8"/>
    <text x="790" y="110" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#EF6C00" text-anchor="middle">3. 종목 설정 확인</text>
    <text x="790" y="135" font-family="Arial, sans-serif" font-size="11" fill="#666666" text-anchor="middle">설정 없으면 매수 건너뜀</text>

    <!-- 화살표 -->
    <line x1="900" y1="120" x2="960" y2="120" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- Step 4: 가격대 추출 -->
    <rect x="970" y="80" width="200" height="80" fill="#FCE4EC" stroke="#C2185B" stroke-width="2" rx="8"/>
    <text x="1070" y="110" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#AD1457" text-anchor="middle">4. 가격대 추출</text>
    <text x="1070" y="135" font-family="Arial, sans-serif" font-size="11" fill="#666666" text-anchor="middle">buy_price_levels 적용</text>

    <!-- 화살표 (세로) -->
    <line x1="1070" y1="160" x2="1070" y2="200" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- AI 분석 결과 가격대 박스 -->
    <rect x="200" y="220" width="900" height="180" fill="#F5F5F5" stroke="#9E9E9E" stroke-width="1" rx="5"/>
    <text x="650" y="250" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#333333" text-anchor="middle">AI 분석 결과 가격대 (낮은 순서)</text>

    <!-- 4개 가격대 박스 -->
    <rect x="230" y="270" width="200" height="110" fill="#4CAF50" stroke="#2E7D32" stroke-width="2" rx="5"/>
    <text x="330" y="300" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#ffffff" text-anchor="middle">적정매수(하한)</text>
    <text x="330" y="325" font-family="Arial, sans-serif" font-size="12" fill="#E8F5E9" text-anchor="middle">appropriate_buy_min</text>
    <text x="330" y="350" font-family="Arial, sans-serif" font-size="11" fill="#C8E6C9" text-anchor="middle">우선순위: 1</text>
    <text x="330" y="370" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">68,000원</text>

    <rect x="450" y="270" width="200" height="110" fill="#8BC34A" stroke="#558B2F" stroke-width="2" rx="5"/>
    <text x="550" y="300" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#ffffff" text-anchor="middle">적정매수(상한)</text>
    <text x="550" y="325" font-family="Arial, sans-serif" font-size="12" fill="#DCEDC8" text-anchor="middle">appropriate_buy_max</text>
    <text x="550" y="350" font-family="Arial, sans-serif" font-size="11" fill="#C5E1A5" text-anchor="middle">우선순위: 2</text>
    <text x="550" y="370" font-family="Arial, sans-serif" font-size="18" fill="#ffffff" text-anchor="middle">70,000원</text>

    <rect x="670" y="270" width="200" height="110" fill="#CDDC39" stroke="#9E9D24" stroke-width="2" rx="5"/>
    <text x="770" y="300" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#333333" text-anchor="middle">희망매수(하한)</text>
    <text x="770" y="325" font-family="Arial, sans-serif" font-size="12" fill="#666666" text-anchor="middle">buy_hope_min</text>
    <text x="770" y="350" font-family="Arial, sans-serif" font-size="11" fill="#827717" text-anchor="middle">우선순위: 3</text>
    <text x="770" y="370" font-family="Arial, sans-serif" font-size="18" fill="#333333" text-anchor="middle">65,000원</text>

    <rect x="890" y="270" width="200" height="110" fill="#FFC107" stroke="#FF8F00" stroke-width="2" rx="5"/>
    <text x="990" y="300" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#333333" text-anchor="middle">희망매수(상한)</text>
    <text x="990" y="325" font-family="Arial, sans-serif" font-size="12" fill="#666666" text-anchor="middle">buy_hope_max</text>
    <text x="990" y="350" font-family="Arial, sans-serif" font-size="11" fill="#FF6F00" text-anchor="middle">우선순위: 4</text>
    <text x="990" y="370" font-family="Arial, sans-serif" font-size="18" fill="#333333" text-anchor="middle">67,000원</text>

    <!-- 화살표 (세로) -->
    <line x1="650" y1="400" x2="650" y2="440" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- Step 5: 조건 필터링 -->
    <rect x="400" y="450" width="500" height="80" fill="#E1BEE7" stroke="#7B1FA2" stroke-width="2" rx="8"/>
    <text x="650" y="480" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#6A1B9A" text-anchor="middle">5. 조건 필터링</text>
    <text x="650" y="510" font-family="Arial, sans-serif" font-size="11" fill="#666666" text-anchor="middle">가격 &lt; 평균매수가 × 0.99 AND 가격 &lt; 현재가</text>

    <!-- 화살표 (세로) -->
    <line x1="650" y1="530" x2="650" y2="570" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- Step 6: 분할 매수 주문 -->
    <rect x="350" y="580" width="600" height="90" fill="#4CAF50" stroke="#2E7D32" stroke-width="3" rx="8"/>
    <text x="650" y="615" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#ffffff" text-anchor="middle">6. 분할 매수 주문 실행</text>
    <text x="650" y="645" font-family="Arial, sans-serif" font-size="13" fill="#E8F5E9" text-anchor="middle">조건 충족 가격대마다 buy_quantity_per_order 수량으로 지정가 주문</text>

    <!-- 실패 경로 표시 -->
    <text x="210" y="175" font-family="Arial, sans-serif" font-size="10" fill="#F44336">NO → 매수 건너뜀</text>
    <text x="790" y="175" font-family="Arial, sans-serif" font-size="10" fill="#F44336">설정 없음 → 건너뜀</text>
</svg>"""

    def create_erd(self) -> str:
        """종목 설정 ERD 다이어그램 (1200x600)"""
        return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="600" xmlns="http://www.w3.org/2000/svg">
    <!-- 배경 -->
    <rect width="1200" height="600" fill="#ffffff"/>

    <!-- 제목 -->
    <text x="600" y="40" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#1a1a2e" text-anchor="middle">
        종목별 거래 설정 ERD
    </text>

    <!-- Users 테이블 -->
    <rect x="100" y="100" width="300" height="200" fill="#E3F2FD" stroke="#1976D2" stroke-width="2" rx="5"/>
    <rect x="100" y="100" width="300" height="40" fill="#1976D2" rx="5"/>
    <text x="250" y="128" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#ffffff" text-anchor="middle">users</text>

    <text x="120" y="165" font-family="monospace" font-size="12" fill="#333333">id (PK)</text>
    <text x="120" y="185" font-family="monospace" font-size="12" fill="#333333">username</text>
    <text x="120" y="205" font-family="monospace" font-size="12" fill="#333333">email</text>
    <text x="120" y="225" font-family="monospace" font-size="12" fill="#333333">role</text>
    <text x="120" y="245" font-family="monospace" font-size="12" fill="#333333">is_active</text>
    <text x="120" y="265" font-family="monospace" font-size="12" fill="#666666">...</text>

    <!-- SymbolTradeSettings 테이블 -->
    <rect x="500" y="80" width="350" height="280" fill="#E8F5E9" stroke="#388E3C" stroke-width="2" rx="5"/>
    <rect x="500" y="80" width="350" height="40" fill="#388E3C" rx="5"/>
    <text x="675" y="108" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#ffffff" text-anchor="middle">symbol_trade_settings</text>

    <text x="520" y="145" font-family="monospace" font-size="12" fill="#333333">id (PK)</text>
    <text x="520" y="165" font-family="monospace" font-size="12" fill="#1976D2">user_id (FK) → users.id</text>
    <text x="520" y="185" font-family="monospace" font-size="12" fill="#333333">symbol</text>
    <text x="520" y="205" font-family="monospace" font-size="12" fill="#333333">instrument_type</text>
    <text x="520" y="225" font-family="monospace" font-size="12" fill="#4CAF50" font-weight="bold">buy_quantity_per_order</text>
    <text x="520" y="245" font-family="monospace" font-size="12" fill="#4CAF50" font-weight="bold">buy_price_levels (1~4)</text>
    <text x="520" y="265" font-family="monospace" font-size="12" fill="#333333">exchange_code</text>
    <text x="520" y="285" font-family="monospace" font-size="12" fill="#333333">is_active</text>
    <text x="520" y="305" font-family="monospace" font-size="12" fill="#333333">note</text>
    <text x="520" y="325" font-family="monospace" font-size="12" fill="#666666">created_at, updated_at</text>

    <!-- UserTradeDefaults 테이블 -->
    <rect x="100" y="350" width="300" height="200" fill="#FFF3E0" stroke="#F57C00" stroke-width="2" rx="5"/>
    <rect x="100" y="350" width="300" height="40" fill="#F57C00" rx="5"/>
    <text x="250" y="378" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#ffffff" text-anchor="middle">user_trade_defaults</text>

    <text x="120" y="415" font-family="monospace" font-size="12" fill="#333333">id (PK)</text>
    <text x="120" y="435" font-family="monospace" font-size="12" fill="#1976D2">user_id (FK, UNIQUE)</text>
    <text x="120" y="455" font-family="monospace" font-size="12" fill="#333333">crypto_default_buy_amount</text>
    <text x="120" y="475" font-family="monospace" font-size="12" fill="#333333">equity_kr_default_buy_qty</text>
    <text x="120" y="495" font-family="monospace" font-size="12" fill="#333333">equity_us_default_buy_qty</text>
    <text x="120" y="515" font-family="monospace" font-size="12" fill="#333333">is_active</text>

    <!-- StockAnalysisResult 테이블 (참조용) -->
    <rect x="900" y="150" width="280" height="220" fill="#F3E5F5" stroke="#7B1FA2" stroke-width="2" rx="5"/>
    <rect x="900" y="150" width="280" height="40" fill="#7B1FA2" rx="5"/>
    <text x="1040" y="178" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#ffffff" text-anchor="middle">stock_analysis_results</text>

    <text x="920" y="215" font-family="monospace" font-size="12" fill="#333333">id (PK)</text>
    <text x="920" y="235" font-family="monospace" font-size="12" fill="#333333">stock_info_id (FK)</text>
    <text x="920" y="255" font-family="monospace" font-size="12" fill="#333333">decision (buy/hold/sell)</text>
    <text x="920" y="275" font-family="monospace" font-size="12" fill="#4CAF50" font-weight="bold">appropriate_buy_min/max</text>
    <text x="920" y="295" font-family="monospace" font-size="12" fill="#4CAF50" font-weight="bold">buy_hope_min/max</text>
    <text x="920" y="315" font-family="monospace" font-size="12" fill="#F44336" font-weight="bold">appropriate_sell_min/max</text>
    <text x="920" y="335" font-family="monospace" font-size="12" fill="#F44336" font-weight="bold">sell_target_min/max</text>

    <!-- 관계선 -->
    <line x1="400" y1="200" x2="500" y2="165" stroke="#1976D2" stroke-width="2"/>
    <text x="440" y="175" font-family="Arial, sans-serif" font-size="11" fill="#1976D2">1:N</text>

    <line x1="400" y1="250" x2="400" y2="400" stroke="#1976D2" stroke-width="2"/>
    <line x1="250" y1="300" x2="250" y2="350" stroke="#1976D2" stroke-width="2"/>
    <text x="265" y="328" font-family="Arial, sans-serif" font-size="11" fill="#1976D2">1:1</text>

    <!-- 설명 박스 -->
    <rect x="500" y="420" width="350" height="130" fill="#FFFDE7" stroke="#FBC02D" stroke-width="1" rx="5"/>
    <text x="675" y="450" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#F57F17" text-anchor="middle">핵심 정책</text>
    <text x="520" y="480" font-family="Arial, sans-serif" font-size="12" fill="#333333">• 종목 설정(SymbolTradeSettings)이 없으면</text>
    <text x="530" y="500" font-family="Arial, sans-serif" font-size="12" fill="#333333">해당 종목은 자동 매수하지 않음</text>
    <text x="520" y="525" font-family="Arial, sans-serif" font-size="12" fill="#333333">• AI 분석 결과의 가격대를 참조하여</text>
    <text x="530" y="545" font-family="Arial, sans-serif" font-size="12" fill="#333333">분할 매수/매도 주문 실행</text>

    <!-- UNIQUE 제약 표시 -->
    <text x="675" y="375" font-family="monospace" font-size="11" fill="#FF5722" text-anchor="middle">UNIQUE(user_id, symbol)</text>
</svg>"""

    def create_dashboard(self) -> str:
        """대시보드 스크린샷 대체 이미지 (1400x800)"""
        return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1400" height="800" xmlns="http://www.w3.org/2000/svg">
    <!-- 브라우저 프레임 -->
    <rect width="1400" height="800" fill="#f5f5f5"/>
    <rect x="0" y="0" width="1400" height="60" fill="#2c2c2c"/>

    <!-- 브라우저 버튼 -->
    <circle cx="25" cy="30" r="8" fill="#ff5f56"/>
    <circle cx="50" cy="30" r="8" fill="#ffbd2e"/>
    <circle cx="75" cy="30" r="8" fill="#27ca40"/>

    <!-- URL 바 -->
    <rect x="120" y="15" width="700" height="30" rx="15" fill="#444444"/>
    <text x="140" y="37" font-family="Arial, sans-serif" font-size="14" fill="#ffffff">https://your-domain.com/kis-domestic-trading/</text>

    <!-- 콘텐츠 영역 -->
    <rect x="20" y="80" width="1360" height="700" fill="#f8f9fa"/>

    <!-- 네비게이션 -->
    <rect x="20" y="80" width="1360" height="50" fill="#1a1a2e"/>
    <text x="50" y="112" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#ffffff">Auto Trader</text>
    <text x="300" y="112" font-family="Arial, sans-serif" font-size="14" fill="#a0a0a0">암호화폐</text>
    <text x="420" y="112" font-family="Arial, sans-serif" font-size="14" fill="#ffffff" font-weight="bold">국내주식</text>
    <text x="540" y="112" font-family="Arial, sans-serif" font-size="14" fill="#a0a0a0">해외주식</text>

    <!-- 제목 -->
    <text x="50" y="180" font-family="Arial, sans-serif" font-size="28" font-weight="bold" fill="#1a1a2e">📈 KIS 국내주식 자동 매매</text>

    <!-- 요약 카드 -->
    <rect x="40" y="200" width="1320" height="100" fill="#ffffff" stroke="#e0e0e0" stroke-width="1" rx="8"/>

    <text x="160" y="235" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">예수금</text>
    <text x="160" y="270" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#1a1a2e" text-anchor="middle">5,234,500원</text>

    <text x="500" y="235" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">보유 종목 수</text>
    <text x="500" y="270" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#1a1a2e" text-anchor="middle">8개</text>

    <text x="840" y="235" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">총 평가 금액</text>
    <text x="840" y="270" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#1a1a2e" text-anchor="middle">12,456,000원</text>

    <text x="1180" y="235" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">총 수익률</text>
    <text x="1180" y="270" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#d60000" text-anchor="middle">+5.23%</text>

    <!-- 자동 매매 제어 카드 -->
    <rect x="40" y="320" width="1320" height="130" fill="#ffffff" stroke="#e0e0e0" stroke-width="1" rx="8"/>
    <text x="60" y="355" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#333333">🤖 자동 매매 제어</text>

    <!-- 버튼들 -->
    <rect x="60" y="375" width="280" height="55" fill="#2196F3" rx="5"/>
    <text x="200" y="410" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#ffffff" text-anchor="middle">🔍 전체 종목 AI 분석</text>

    <rect x="360" y="375" width="280" height="55" fill="#4CAF50" rx="5"/>
    <text x="500" y="410" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#ffffff" text-anchor="middle">🛒 자동 매수 주문</text>

    <rect x="660" y="375" width="280" height="55" fill="#F44336" rx="5"/>
    <text x="800" y="410" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#ffffff" text-anchor="middle">💰 자동 매도 주문</text>

    <rect x="960" y="375" width="380" height="55" fill="#FF9800" rx="5"/>
    <text x="1150" y="410" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#ffffff" text-anchor="middle">⚡ 종목별 분석→매수→매도</text>

    <!-- 보유 종목 테이블 헤더 -->
    <rect x="40" y="470" width="1320" height="290" fill="#ffffff" stroke="#e0e0e0" stroke-width="1" rx="8"/>
    <text x="60" y="505" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#333333">📋 보유 종목</text>

    <!-- 테이블 헤더 -->
    <rect x="60" y="520" width="1280" height="35" fill="#f0f0f0"/>
    <text x="130" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">종목명</text>
    <text x="270" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">수량</text>
    <text x="380" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">현재가</text>
    <text x="500" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">평균매수가</text>
    <text x="620" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">수익률</text>
    <text x="750" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">AI 분석</text>
    <text x="890" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">설정</text>
    <text x="1100" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">개별 액션</text>

    <!-- 종목 행 1 -->
    <line x1="60" y1="555" x2="1340" y2="555" stroke="#e0e0e0"/>
    <text x="130" y="585" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">삼성전자</text>
    <text x="270" y="585" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">50</text>
    <text x="380" y="585" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">71,500</text>
    <text x="500" y="585" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">68,200</text>
    <text x="620" y="585" font-family="Arial, sans-serif" font-size="13" fill="#d60000" text-anchor="middle">+4.84%</text>
    <rect x="710" y="570" width="80" height="25" fill="#E8F5E9" rx="3"/>
    <text x="750" y="588" font-family="Arial, sans-serif" font-size="11" fill="#2E7D32" text-anchor="middle">BUY 75%</text>
    <rect x="850" y="570" width="80" height="25" fill="#4CAF50" rx="3"/>
    <text x="890" y="588" font-family="Arial, sans-serif" font-size="11" fill="#ffffff" text-anchor="middle">5주 / 2개</text>
    <rect x="980" y="567" width="60" height="28" fill="#2196F3" rx="3"/>
    <rect x="1050" y="567" width="60" height="28" fill="#4CAF50" rx="3"/>
    <rect x="1120" y="567" width="60" height="28" fill="#F44336" rx="3"/>
    <text x="1010" y="586" font-family="Arial, sans-serif" font-size="10" fill="#ffffff" text-anchor="middle">분석</text>
    <text x="1080" y="586" font-family="Arial, sans-serif" font-size="10" fill="#ffffff" text-anchor="middle">매수</text>
    <text x="1150" y="586" font-family="Arial, sans-serif" font-size="10" fill="#ffffff" text-anchor="middle">매도</text>

    <!-- 종목 행 2 -->
    <line x1="60" y1="600" x2="1340" y2="600" stroke="#e0e0e0"/>
    <text x="130" y="630" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">SK하이닉스</text>
    <text x="270" y="630" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">20</text>
    <text x="380" y="630" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">178,000</text>
    <text x="500" y="630" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">185,500</text>
    <text x="620" y="630" font-family="Arial, sans-serif" font-size="13" fill="#0051c7" text-anchor="middle">-4.04%</text>
    <rect x="710" y="615" width="80" height="25" fill="#FFF3E0" rx="3"/>
    <text x="750" y="633" font-family="Arial, sans-serif" font-size="11" fill="#EF6C00" text-anchor="middle">HOLD 60%</text>
    <rect x="850" y="615" width="80" height="25" fill="#4CAF50" rx="3"/>
    <text x="890" y="633" font-family="Arial, sans-serif" font-size="11" fill="#ffffff" text-anchor="middle">2주 / 4개</text>
    <rect x="980" y="612" width="60" height="28" fill="#2196F3" rx="3"/>
    <rect x="1050" y="612" width="60" height="28" fill="#4CAF50" rx="3"/>
    <rect x="1120" y="612" width="60" height="28" fill="#F44336" rx="3"/>
    <text x="1010" y="631" font-family="Arial, sans-serif" font-size="10" fill="#ffffff" text-anchor="middle">분석</text>
    <text x="1080" y="631" font-family="Arial, sans-serif" font-size="10" fill="#ffffff" text-anchor="middle">매수</text>
    <text x="1150" y="631" font-family="Arial, sans-serif" font-size="10" fill="#ffffff" text-anchor="middle">매도</text>

    <!-- 종목 행 3 -->
    <line x1="60" y1="645" x2="1340" y2="645" stroke="#e0e0e0"/>
    <text x="130" y="675" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">NAVER</text>
    <text x="270" y="675" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">10</text>
    <text x="380" y="675" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">215,000</text>
    <text x="500" y="675" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">198,000</text>
    <text x="620" y="675" font-family="Arial, sans-serif" font-size="13" fill="#d60000" text-anchor="middle">+8.59%</text>
    <rect x="710" y="660" width="80" height="25" fill="#FFEBEE" rx="3"/>
    <text x="750" y="678" font-family="Arial, sans-serif" font-size="11" fill="#C62828" text-anchor="middle">SELL 80%</text>
    <rect x="850" y="660" width="80" height="25" fill="#9E9E9E" rx="3"/>
    <text x="890" y="678" font-family="Arial, sans-serif" font-size="11" fill="#ffffff" text-anchor="middle">미설정</text>
    <rect x="980" y="657" width="60" height="28" fill="#2196F3" rx="3"/>
    <rect x="1050" y="657" width="60" height="28" fill="#BDBDBD" rx="3"/>
    <rect x="1120" y="657" width="60" height="28" fill="#F44336" rx="3"/>
    <text x="1010" y="676" font-family="Arial, sans-serif" font-size="10" fill="#ffffff" text-anchor="middle">분석</text>
    <text x="1080" y="676" font-family="Arial, sans-serif" font-size="10" fill="#ffffff" text-anchor="middle">매수</text>
    <text x="1150" y="676" font-family="Arial, sans-serif" font-size="10" fill="#ffffff" text-anchor="middle">매도</text>

    <!-- 더보기 표시 -->
    <text x="700" y="730" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">... 5개 종목 더 있음</text>
</svg>"""

    def create_progress(self) -> str:
        """진행 상황 표시 UI (1200x400)"""
        return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="400" xmlns="http://www.w3.org/2000/svg">
    <!-- 배경 -->
    <rect width="1200" height="400" fill="#f8f9fa"/>

    <!-- 제목 -->
    <text x="600" y="40" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#1a1a2e" text-anchor="middle">
        실시간 진행 상황 표시 UI
    </text>

    <!-- 분석 진행 카드 -->
    <rect x="50" y="70" width="350" height="150" fill="#ffffff" stroke="#2196F3" stroke-width="2" rx="8"/>
    <text x="70" y="100" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#1976D2">🔍 전체 종목 AI 분석</text>

    <rect x="70" y="115" width="310" height="25" fill="#E3F2FD" rx="5"/>
    <rect x="70" y="115" width="217" height="25" fill="#2196F3" rx="5"/>
    <text x="225" y="133" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#ffffff" text-anchor="middle">70%</text>

    <text x="70" y="165" font-family="Arial, sans-serif" font-size="13" fill="#666666">삼성전자 분석 중... (7/10)</text>
    <text x="70" y="190" font-family="Arial, sans-serif" font-size="11" fill="#999999">예상 남은 시간: 약 2분</text>

    <!-- 매수 진행 카드 -->
    <rect x="425" y="70" width="350" height="150" fill="#ffffff" stroke="#4CAF50" stroke-width="2" rx="8"/>
    <text x="445" y="100" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#2E7D32">🛒 자동 매수 주문</text>

    <rect x="445" y="115" width="310" height="25" fill="#E8F5E9" rx="5"/>
    <rect x="445" y="115" width="124" height="25" fill="#4CAF50" rx="5"/>
    <text x="507" y="133" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#ffffff" text-anchor="middle">40%</text>

    <text x="445" y="165" font-family="Arial, sans-serif" font-size="13" fill="#666666">SK하이닉스 매수 주문 처리 중...</text>
    <text x="445" y="190" font-family="Arial, sans-serif" font-size="11" fill="#4CAF50">✓ 삼성전자: 2건 주문 완료</text>

    <!-- 종목별 자동실행 카드 -->
    <rect x="800" y="70" width="350" height="150" fill="#ffffff" stroke="#FF9800" stroke-width="2" rx="8"/>
    <text x="820" y="100" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#EF6C00">⚡ 종목별 분석→매수→매도</text>

    <rect x="820" y="115" width="310" height="25" fill="#FFF3E0" rx="5"/>
    <rect x="820" y="115" width="155" height="25" fill="#FF9800" rx="5"/>
    <text x="897" y="133" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#ffffff" text-anchor="middle">50%</text>

    <text x="820" y="165" font-family="Arial, sans-serif" font-size="13" fill="#666666">NAVER 매수 주문 중...</text>
    <text x="820" y="190" font-family="Arial, sans-serif" font-size="11" fill="#FF9800">현재 단계: 분석 → 매수 → 매도</text>

    <!-- 상세 로그 영역 -->
    <rect x="50" y="240" width="1100" height="140" fill="#ffffff" stroke="#e0e0e0" stroke-width="1" rx="8"/>
    <text x="70" y="270" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#333333">📋 실행 로그</text>

    <rect x="70" y="285" width="1060" height="80" fill="#f5f5f5" rx="5"/>
    <text x="85" y="305" font-family="monospace" font-size="11" fill="#666666">[10:32:15] ✓ 삼성전자 분석 완료 (decision: BUY, confidence: 75%)</text>
    <text x="85" y="322" font-family="monospace" font-size="11" fill="#666666">[10:32:16] ✓ 삼성전자 매수 주문 2건 접수 (68,000원 x 5주, 70,000원 x 5주)</text>
    <text x="85" y="339" font-family="monospace" font-size="11" fill="#666666">[10:32:18] ⏳ SK하이닉스 분석 시작...</text>
    <text x="85" y="356" font-family="monospace" font-size="11" fill="#2196F3">[10:32:45] 🔄 SK하이닉스 분석 중 (70% 완료)</text>
</svg>"""

    def create_flower(self) -> str:
        """Flower 모니터링 대시보드 (1200x600)"""
        return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="600" xmlns="http://www.w3.org/2000/svg">
    <!-- 브라우저 프레임 -->
    <rect width="1200" height="600" fill="#f5f5f5"/>
    <rect x="0" y="0" width="1200" height="50" fill="#2c2c2c"/>

    <!-- 브라우저 버튼 -->
    <circle cx="20" cy="25" r="7" fill="#ff5f56"/>
    <circle cx="42" cy="25" r="7" fill="#ffbd2e"/>
    <circle cx="64" cy="25" r="7" fill="#27ca40"/>

    <!-- URL 바 -->
    <rect x="100" y="12" width="500" height="26" rx="13" fill="#444444"/>
    <text x="120" y="31" font-family="Arial, sans-serif" font-size="12" fill="#ffffff">http://localhost:5555/</text>

    <!-- Flower 헤더 -->
    <rect x="0" y="50" width="1200" height="60" fill="#1a1a2e"/>
    <text x="30" y="88" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#ffffff">🌸 Flower</text>
    <text x="140" y="88" font-family="Arial, sans-serif" font-size="14" fill="#a0a0a0">Celery monitoring</text>

    <!-- 탭 메뉴 -->
    <rect x="400" y="70" width="100" height="30" fill="#333355"/>
    <text x="450" y="92" font-family="Arial, sans-serif" font-size="13" fill="#ffffff" text-anchor="middle">Dashboard</text>
    <text x="550" y="92" font-family="Arial, sans-serif" font-size="13" fill="#a0a0a0" text-anchor="middle">Tasks</text>
    <text x="640" y="92" font-family="Arial, sans-serif" font-size="13" fill="#a0a0a0" text-anchor="middle">Workers</text>

    <!-- 콘텐츠 영역 -->
    <rect x="0" y="110" width="1200" height="490" fill="#ffffff"/>

    <!-- 통계 카드들 -->
    <rect x="30" y="130" width="180" height="100" fill="#E3F2FD" stroke="#1976D2" stroke-width="1" rx="8"/>
    <text x="120" y="165" font-family="Arial, sans-serif" font-size="14" fill="#1976D2" text-anchor="middle">Active Tasks</text>
    <text x="120" y="205" font-family="Arial, sans-serif" font-size="36" font-weight="bold" fill="#1565C0" text-anchor="middle">3</text>

    <rect x="230" y="130" width="180" height="100" fill="#E8F5E9" stroke="#388E3C" stroke-width="1" rx="8"/>
    <text x="320" y="165" font-family="Arial, sans-serif" font-size="14" fill="#2E7D32" text-anchor="middle">Succeeded</text>
    <text x="320" y="205" font-family="Arial, sans-serif" font-size="36" font-weight="bold" fill="#1B5E20" text-anchor="middle">47</text>

    <rect x="430" y="130" width="180" height="100" fill="#FFEBEE" stroke="#C62828" stroke-width="1" rx="8"/>
    <text x="520" y="165" font-family="Arial, sans-serif" font-size="14" fill="#C62828" text-anchor="middle">Failed</text>
    <text x="520" y="205" font-family="Arial, sans-serif" font-size="36" font-weight="bold" fill="#B71C1C" text-anchor="middle">2</text>

    <rect x="630" y="130" width="180" height="100" fill="#FFF3E0" stroke="#F57C00" stroke-width="1" rx="8"/>
    <text x="720" y="165" font-family="Arial, sans-serif" font-size="14" fill="#EF6C00" text-anchor="middle">Workers</text>
    <text x="720" y="205" font-family="Arial, sans-serif" font-size="36" font-weight="bold" fill="#E65100" text-anchor="middle">1</text>

    <!-- 최근 태스크 목록 -->
    <rect x="30" y="250" width="780" height="320" fill="#ffffff" stroke="#e0e0e0" stroke-width="1" rx="8"/>
    <text x="50" y="280" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#333333">Recent Tasks</text>

    <!-- 테이블 헤더 -->
    <rect x="45" y="295" width="750" height="30" fill="#f5f5f5"/>
    <text x="65" y="315" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#666666">Name</text>
    <text x="350" y="315" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#666666">State</text>
    <text x="480" y="315" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#666666">Received</text>
    <text x="650" y="315" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#666666">Runtime</text>

    <!-- 태스크 행들 -->
    <line x1="45" y1="325" x2="795" y2="325" stroke="#e0e0e0"/>
    <text x="65" y="350" font-family="monospace" font-size="11" fill="#333333">kis.run_analysis_for_my_domestic_stocks</text>
    <rect x="340" y="337" width="70" height="20" fill="#FFF3E0" rx="3"/>
    <text x="375" y="352" font-family="Arial, sans-serif" font-size="10" fill="#EF6C00" text-anchor="middle">PROGRESS</text>
    <text x="480" y="350" font-family="Arial, sans-serif" font-size="11" fill="#666666">10:32:00</text>
    <text x="650" y="350" font-family="Arial, sans-serif" font-size="11" fill="#666666">45.2s</text>

    <line x1="45" y1="365" x2="795" y2="365" stroke="#e0e0e0"/>
    <text x="65" y="390" font-family="monospace" font-size="11" fill="#333333">kis.execute_domestic_buy_order_task</text>
    <rect x="340" y="377" width="70" height="20" fill="#E8F5E9" rx="3"/>
    <text x="375" y="392" font-family="Arial, sans-serif" font-size="10" fill="#2E7D32" text-anchor="middle">SUCCESS</text>
    <text x="480" y="390" font-family="Arial, sans-serif" font-size="11" fill="#666666">10:31:45</text>
    <text x="650" y="390" font-family="Arial, sans-serif" font-size="11" fill="#666666">2.3s</text>

    <line x1="45" y1="405" x2="795" y2="405" stroke="#e0e0e0"/>
    <text x="65" y="430" font-family="monospace" font-size="11" fill="#333333">kis.analyze_domestic_stock_task</text>
    <rect x="340" y="417" width="70" height="20" fill="#E8F5E9" rx="3"/>
    <text x="375" y="432" font-family="Arial, sans-serif" font-size="10" fill="#2E7D32" text-anchor="middle">SUCCESS</text>
    <text x="480" y="430" font-family="Arial, sans-serif" font-size="11" fill="#666666">10:31:20</text>
    <text x="650" y="430" font-family="Arial, sans-serif" font-size="11" fill="#666666">23.5s</text>

    <line x1="45" y1="445" x2="795" y2="445" stroke="#e0e0e0"/>
    <text x="65" y="470" font-family="monospace" font-size="11" fill="#333333">kis.execute_overseas_buy_order_task</text>
    <rect x="340" y="457" width="70" height="20" fill="#FFEBEE" rx="3"/>
    <text x="375" y="472" font-family="Arial, sans-serif" font-size="10" fill="#C62828" text-anchor="middle">FAILURE</text>
    <text x="480" y="470" font-family="Arial, sans-serif" font-size="11" fill="#666666">10:30:55</text>
    <text x="650" y="470" font-family="Arial, sans-serif" font-size="11" fill="#666666">1.2s</text>

    <!-- Worker 상태 -->
    <rect x="830" y="250" width="340" height="150" fill="#ffffff" stroke="#e0e0e0" stroke-width="1" rx="8"/>
    <text x="850" y="280" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#333333">Worker Status</text>

    <circle cx="865" cy="315" r="8" fill="#4CAF50"/>
    <text x="885" y="320" font-family="Arial, sans-serif" font-size="14" fill="#333333">celery@raspberrypi</text>

    <text x="865" y="350" font-family="Arial, sans-serif" font-size="12" fill="#666666">Concurrency: 4</text>
    <text x="865" y="370" font-family="Arial, sans-serif" font-size="12" fill="#666666">Active: 3 | Processed: 52</text>
    <text x="865" y="390" font-family="Arial, sans-serif" font-size="12" fill="#666666">Pool: prefork</text>
</svg>"""


if __name__ == "__main__":
    generator = KISTradingImages("kis_trading")
    generator.generate()
