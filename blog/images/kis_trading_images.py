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

from blog.tools.components.thumbnail import ThumbnailTemplate
from blog.tools.image_generator import BlogImageGenerator


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
        from blog.tools.components.base import SVGComponent
        from blog.tools.components.flow_diagram import FlowDiagram

        nodes = [
            (500, 170, 400, 80, "웹 대시보드", "#2196F3"),
            (450, 285, 500, 120, "FastAPI 라우터", "#4CAF50"),
            (200, 440, 1000, 160, "Celery 비동기 태스크", "#FF9800"),
            (200, 655, 400, 100, "KIS API + AI 분석", "#9C27B0"),
            (800, 655, 400, 100, "PostgreSQL + Redis", "#607D8B"),
            (1150, 440, 100, 160, "Telegram", "#03A9F4"),
        ]
        edges = [
            (0, 1, "웹 요청"),
            (1, 2, "task.delay"),
            (2, 3, "API 호출"),
            (2, 4, "결과 저장"),
            (2, 5, "알림"),
        ]
        task_boxes = [
            (230, "전체 종목 분석", "analyze_stocks", "진행상황 업데이트"),
            (430, "전체 종목 매수", "execute_buy_orders", "분할 매수 주문"),
            (630, "전체 종목 매도", "execute_sell_orders", "수익 목표 기반"),
            (830, "종목별 자동실행", "per_stock_automation", "분석→매수→매도"),
            (1030, "개별 종목", "단일 분석/매수/매도", "즉시 실행"),
        ]

        svg = SVGComponent.header(1400, 900)
        svg += SVGComponent.background(1400, 900)
        svg += SVGComponent.title(
            1400, "KIS 자동 매매 시스템 아키텍처", y=45, font_size=32
        )
        svg += """
    <ellipse cx="700" cy="100" rx="70" ry="35" fill="#E0E0E0" stroke="#666666" stroke-width="2"/>
    <text x="700" y="108" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#333333" text-anchor="middle">사용자</text>
    <line x1="700" y1="135" x2="700" y2="165" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>

    <rect x="200" y="440" width="1000" height="45" fill="#EF6C00" opacity="0.25" rx="8"/>
    <rect x="200" y="655" width="400" height="35" fill="#6A1B9A" opacity="0.3" rx="8"/>
    <rect x="800" y="655" width="400" height="35" fill="#37474F" opacity="0.3" rx="8"/>
"""
        svg += FlowDiagram.create(nodes=nodes, edges=edges)

        for x, title, task_name, note in task_boxes:
            width = 180 if x < 1030 else 150
            center = x + (width // 2)
            svg += f"""
    <rect x="{x}" y="490" width="{width}" height="90" fill="#FFB74D" stroke="#EF6C00" stroke-width="1" rx="5"/>
    <text x="{center}" y="515" font-family="Arial, sans-serif" font-size="13" font-weight="bold" fill="#333333" text-anchor="middle">{title}</text>
    <text x="{center}" y="535" font-family="Arial, sans-serif" font-size="11" fill="#666666" text-anchor="middle">{task_name}</text>
    <text x="{center}" y="555" font-family="Arial, sans-serif" font-size="10" fill="#888888" text-anchor="middle">{note}</text>
"""

        svg += """
    <text x="700" y="225" font-family="Arial, sans-serif" font-size="14" fill="#E3F2FD" text-anchor="middle">/kis-domestic-trading/ • /kis-overseas-trading/</text>
    <text x="540" y="345" font-family="Arial, sans-serif" font-size="12" fill="#E8F5E9" text-anchor="middle">보유주식 조회</text>
    <text x="640" y="345" font-family="Arial, sans-serif" font-size="12" fill="#E8F5E9" text-anchor="middle">AI 분석</text>
    <text x="740" y="345" font-family="Arial, sans-serif" font-size="12" fill="#E8F5E9" text-anchor="middle">매수/매도</text>
    <text x="840" y="345" font-family="Arial, sans-serif" font-size="12" fill="#E8F5E9" text-anchor="middle">종목설정</text>
    <text x="700" y="385" font-family="Arial, sans-serif" font-size="11" fill="#C8E6C9" text-anchor="middle">kis_domestic_trading.py • kis_overseas_trading.py • symbol_settings.py</text>
    <text x="300" y="720" font-family="Arial, sans-serif" font-size="12" fill="#E1BEE7" text-anchor="middle">KISClient</text>
    <text x="400" y="720" font-family="Arial, sans-serif" font-size="12" fill="#E1BEE7" text-anchor="middle">KISAnalyzer</text>
    <text x="500" y="720" font-family="Arial, sans-serif" font-size="12" fill="#E1BEE7" text-anchor="middle">YahooAnalyzer</text>
    <text x="400" y="745" font-family="Arial, sans-serif" font-size="11" fill="#CE93D8" text-anchor="middle">한국투자증권 API • Google Gemini</text>
    <text x="900" y="720" font-family="Arial, sans-serif" font-size="12" fill="#CFD8DC" text-anchor="middle">StockAnalysisResult</text>
    <text x="1100" y="720" font-family="Arial, sans-serif" font-size="12" fill="#CFD8DC" text-anchor="middle">SymbolTradeSettings</text>
    <text x="1000" y="745" font-family="Arial, sans-serif" font-size="11" fill="#B0BEC5" text-anchor="middle">분석 결과 • 종목별 설정 • 태스크 큐</text>
    <text x="1200" y="500" font-family="Arial, sans-serif" font-size="30" fill="#ffffff" text-anchor="middle">📱</text>
    <text x="1200" y="530" font-family="Arial, sans-serif" font-size="10" fill="#E1F5FE" text-anchor="middle">분석 완료</text>
    <text x="1200" y="545" font-family="Arial, sans-serif" font-size="10" fill="#E1F5FE" text-anchor="middle">주문 접수</text>
    <text x="1200" y="560" font-family="Arial, sans-serif" font-size="10" fill="#E1F5FE" text-anchor="middle">에러 알림</text>
    <text x="700" y="830" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">웹 요청 → FastAPI → Celery 태스크 → KIS API/AI 분석 → DB 저장 → Telegram 알림</text>
    <text x="700" y="855" font-family="Arial, sans-serif" font-size="12" fill="#999999" text-anchor="middle">비동기 처리로 오래 걸리는 작업도 즉시 응답, 진행 상황 실시간 폴링</text>
"""
        svg += SVGComponent.footer()
        return svg

    def create_buy_flow(self) -> str:
        """매수 로직 플로우 다이어그램 (1200x700)"""
        from blog.tools.components.base import SVGComponent
        from blog.tools.components.flow_diagram import FlowDiagram

        nodes = [
            (80, 80, 180, 70, "1. 1% 조건 확인", "#1976D2"),
            (280, 80, 180, 70, "2. AI 분석 조회", "#388E3C"),
            (480, 80, 180, 70, "3. 종목 설정 확인", "#F57C00"),
            (680, 80, 180, 70, "4. 가격대 추출", "#C2185B"),
            (500, 450, 300, 70, "5. 조건 필터링", "#7B1FA2"),
            (400, 580, 500, 80, "6. 분할 매수 주문 실행", "#2E7D32"),
        ]
        edges = [
            (0, 1, "YES"),
            (1, 2, ""),
            (2, 3, ""),
            (3, 4, "price levels"),
            (4, 5, "order"),
        ]
        price_levels = [
            (
                230,
                "적정매수(하한)",
                "appropriate_buy_min",
                "우선순위: 1",
                "68,000원",
                "#4CAF50",
                "#2E7D32",
                "#ffffff",
            ),
            (
                450,
                "적정매수(상한)",
                "appropriate_buy_max",
                "우선순위: 2",
                "70,000원",
                "#8BC34A",
                "#558B2F",
                "#ffffff",
            ),
            (
                670,
                "희망매수(하한)",
                "buy_hope_min",
                "우선순위: 3",
                "65,000원",
                "#CDDC39",
                "#9E9D24",
                "#333333",
            ),
            (
                890,
                "희망매수(상한)",
                "buy_hope_max",
                "우선순위: 4",
                "67,000원",
                "#FFC107",
                "#FF8F00",
                "#333333",
            ),
        ]

        svg = SVGComponent.header(1200, 700)
        svg += SVGComponent.background(1200, 700, fill="#ffffff")
        svg += SVGComponent.title(
            1200, "AI 분석 기반 분할 매수 플로우", y=40, font_size=28
        )
        svg += FlowDiagram.create(nodes=nodes, edges=edges)
        svg += """
    <rect x="200" y="220" width="900" height="180" fill="#F5F5F5" stroke="#9E9E9E" stroke-width="1" rx="5"/>
    <text x="650" y="250" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#333333" text-anchor="middle">AI 분석 결과 가격대 (낮은 순서)</text>
    <text x="210" y="175" font-family="Arial, sans-serif" font-size="10" fill="#F44336">NO → 매수 건너뜀</text>
    <text x="790" y="175" font-family="Arial, sans-serif" font-size="10" fill="#F44336">설정 없음 → 건너뜀</text>
    <text x="650" y="510" font-family="Arial, sans-serif" font-size="11" fill="#666666" text-anchor="middle">가격 &lt; 평균매수가 × 0.99 AND 가격 &lt; 현재가</text>
    <text x="650" y="645" font-family="Arial, sans-serif" font-size="13" fill="#E8F5E9" text-anchor="middle">조건 충족 가격대마다 buy_quantity_per_order 수량으로 지정가 주문</text>
"""

        for (
            x,
            title,
            field_name,
            priority,
            amount,
            fill,
            stroke,
            text_fill,
        ) in price_levels:
            svg += f"""
    <rect x="{x}" y="270" width="200" height="110" fill="{fill}" stroke="{stroke}" stroke-width="2" rx="5"/>
    <text x="{x + 100}" y="300" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="{text_fill}" text-anchor="middle">{title}</text>
    <text x="{x + 100}" y="325" font-family="Arial, sans-serif" font-size="12" fill="#666666" text-anchor="middle">{field_name}</text>
    <text x="{x + 100}" y="350" font-family="Arial, sans-serif" font-size="11" fill="#666666" text-anchor="middle">{priority}</text>
    <text x="{x + 100}" y="370" font-family="Arial, sans-serif" font-size="18" fill="{text_fill}" text-anchor="middle">{amount}</text>
"""

        svg += SVGComponent.footer()
        return svg

    def create_erd(self) -> str:
        """종목 설정 ERD 다이어그램 (1200x600)"""
        from blog.tools.components.base import SVGComponent
        from blog.tools.components.table import ComparisonTable

        svg = SVGComponent.header(1200, 600)
        svg += SVGComponent.background(1200, 600, fill="#ffffff")
        svg += SVGComponent.title(1200, "종목별 거래 설정 ERD", y=40, font_size=24)

        svg += ComparisonTable.create(
            x=70,
            y=90,
            width=300,
            height=220,
            headers=["users", "details"],
            rows=[
                ["id", "PK"],
                ["username", "text"],
                ["email", "text"],
                ["role", "text"],
                ["is_active", "bool"],
                ["...", ""],
            ],
            header_color="#1976D2",
        )
        svg += ComparisonTable.create(
            x=430,
            y=70,
            width=380,
            height=300,
            headers=["symbol_trade_settings", "details"],
            rows=[
                ["id", "PK"],
                ["user_id", "FK → users.id"],
                ["symbol", "text"],
                ["instrument_type", "text"],
                ["buy_quantity_per_order", "핵심"],
                ["buy_price_levels (1~4)", "핵심"],
                ["exchange_code", "text"],
                ["is_active", "bool"],
                ["note", "text"],
                ["created_at, updated_at", "timestamp"],
            ],
            header_color="#388E3C",
        )
        svg += ComparisonTable.create(
            x=70,
            y=340,
            width=300,
            height=220,
            headers=["user_trade_defaults", "details"],
            rows=[
                ["id", "PK"],
                ["user_id", "FK, UNIQUE"],
                ["crypto_default_buy_amount", "numeric"],
                ["equity_kr_default_buy_qty", "int"],
                ["equity_us_default_buy_qty", "int"],
                ["is_active", "bool"],
            ],
            header_color="#F57C00",
        )
        svg += ComparisonTable.create(
            x=850,
            y=140,
            width=320,
            height=240,
            headers=["stock_analysis_results", "details"],
            rows=[
                ["id", "PK"],
                ["stock_info_id", "FK"],
                ["decision", "buy/hold/sell"],
                ["appropriate_buy_min/max", "매수"],
                ["buy_hope_min/max", "매수"],
                ["appropriate_sell_min/max", "매도"],
                ["sell_target_min/max", "매도"],
            ],
            header_color="#7B1FA2",
        )

        svg += """
    <line x1="370" y1="175" x2="430" y2="145" stroke="#1976D2" stroke-width="2" marker-end="url(#arrowhead)"/>
    <text x="400" y="160" font-family="Arial, sans-serif" font-size="11" fill="#1976D2">1:N</text>
    <line x1="220" y1="310" x2="220" y2="340" stroke="#1976D2" stroke-width="2" marker-end="url(#arrowhead)"/>
    <text x="235" y="328" font-family="Arial, sans-serif" font-size="11" fill="#1976D2">1:1</text>
    <line x1="810" y1="220" x2="850" y2="220" stroke="#7B1FA2" stroke-width="2" marker-end="url(#arrowhead)"/>

    <rect x="430" y="420" width="420" height="130" fill="#FFFDE7" stroke="#FBC02D" stroke-width="1" rx="5"/>
    <text x="640" y="450" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#F57F17" text-anchor="middle">핵심 정책</text>
    <text x="450" y="480" font-family="Arial, sans-serif" font-size="12" fill="#333333">• 종목 설정(SymbolTradeSettings)이 없으면 자동 매수하지 않음</text>
    <text x="450" y="505" font-family="Arial, sans-serif" font-size="12" fill="#333333">• AI 분석 결과 가격대를 참조하여 분할 매수/매도 주문 실행</text>
    <text x="640" y="375" font-family="monospace" font-size="11" fill="#FF5722" text-anchor="middle">UNIQUE(user_id, symbol)</text>
"""
        svg += SVGComponent.footer()
        return svg

    def create_dashboard(self) -> str:
        """대시보드 스크린샷 대체 이미지 (1400x800)"""
        from blog.tools.components.base import SVGComponent

        summary_items = [
            (160, "예수금", "5,234,500원", "#1a1a2e"),
            (500, "보유 종목 수", "8개", "#1a1a2e"),
            (840, "총 평가 금액", "12,456,000원", "#1a1a2e"),
            (1180, "총 수익률", "+5.23%", "#d60000"),
        ]
        control_buttons = [
            (60, 280, "#2196F3", "🔍 전체 종목 AI 분석"),
            (360, 280, "#4CAF50", "🛒 자동 매수 주문"),
            (660, 280, "#F44336", "💰 자동 매도 주문"),
            (960, 380, "#FF9800", "⚡ 종목별 분석→매수→매도"),
        ]
        holdings = [
            (
                "삼성전자",
                "50",
                "71,500",
                "68,200",
                "+4.84%",
                "BUY 75%",
                "5주 / 2개",
                "#d60000",
                "#E8F5E9",
                "#4CAF50",
                "#4CAF50",
            ),
            (
                "SK하이닉스",
                "20",
                "178,000",
                "185,500",
                "-4.04%",
                "HOLD 60%",
                "2주 / 4개",
                "#0051c7",
                "#FFF3E0",
                "#4CAF50",
                "#4CAF50",
            ),
            (
                "NAVER",
                "10",
                "215,000",
                "198,000",
                "+8.59%",
                "SELL 80%",
                "미설정",
                "#d60000",
                "#FFEBEE",
                "#9E9E9E",
                "#BDBDBD",
            ),
        ]

        svg = SVGComponent.header(1400, 800)
        svg += SVGComponent.background(1400, 800, fill="#f5f5f5")
        svg += """
    <rect x="0" y="0" width="1400" height="60" fill="#2c2c2c"/>
    <circle cx="25" cy="30" r="8" fill="#ff5f56"/>
    <circle cx="50" cy="30" r="8" fill="#ffbd2e"/>
    <circle cx="75" cy="30" r="8" fill="#27ca40"/>
    <rect x="120" y="15" width="700" height="30" rx="15" fill="#444444"/>
    <text x="140" y="37" font-family="Arial, sans-serif" font-size="14" fill="#ffffff">https://your-domain.com/kis-domestic-trading/</text>

    <rect x="20" y="80" width="1360" height="700" fill="#f8f9fa"/>
    <rect x="20" y="80" width="1360" height="50" fill="#1a1a2e"/>
    <text x="50" y="112" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#ffffff">Auto Trader</text>
    <text x="300" y="112" font-family="Arial, sans-serif" font-size="14" fill="#a0a0a0">암호화폐</text>
    <text x="420" y="112" font-family="Arial, sans-serif" font-size="14" fill="#ffffff" font-weight="bold">국내주식</text>
    <text x="540" y="112" font-family="Arial, sans-serif" font-size="14" fill="#a0a0a0">해외주식</text>

    <text x="50" y="180" font-family="Arial, sans-serif" font-size="28" font-weight="bold" fill="#1a1a2e">📈 KIS 국내주식 자동 매매</text>
    <rect x="40" y="200" width="1320" height="100" fill="#ffffff" stroke="#e0e0e0" stroke-width="1" rx="8"/>
"""
        for x, label, value, color in summary_items:
            svg += f"""
    <text x="{x}" y="235" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">{label}</text>
    <text x="{x}" y="270" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="{color}" text-anchor="middle">{value}</text>
"""

        svg += """
    <rect x="40" y="320" width="1320" height="130" fill="#ffffff" stroke="#e0e0e0" stroke-width="1" rx="8"/>
    <text x="60" y="355" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#333333">🤖 자동 매매 제어</text>
"""
        for x, width, color, label in control_buttons:
            center = x + width // 2
            svg += f"""
    <rect x="{x}" y="375" width="{width}" height="55" fill="{color}" rx="5"/>
    <text x="{center}" y="410" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#ffffff" text-anchor="middle">{label}</text>
"""

        svg += """
    <rect x="40" y="470" width="1320" height="290" fill="#ffffff" stroke="#e0e0e0" stroke-width="1" rx="8"/>
    <text x="60" y="505" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#333333">📋 보유 종목</text>
    <rect x="60" y="520" width="1280" height="35" fill="#f0f0f0"/>
    <text x="130" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">종목명</text>
    <text x="270" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">수량</text>
    <text x="380" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">현재가</text>
    <text x="500" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">평균매수가</text>
    <text x="620" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">수익률</text>
    <text x="750" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">AI 분석</text>
    <text x="890" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">설정</text>
    <text x="1100" y="545" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333" text-anchor="middle">개별 액션</text>
"""

        row_y = 555
        for (
            name,
            qty,
            price,
            avg,
            pnl,
            ai,
            setting,
            pnl_color,
            ai_bg,
            setting_bg,
            buy_btn,
        ) in holdings:
            y = row_y + 30
            svg += f"""
    <line x1="60" y1="{row_y}" x2="1340" y2="{row_y}" stroke="#e0e0e0"/>
    <text x="130" y="{y}" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">{name}</text>
    <text x="270" y="{y}" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">{qty}</text>
    <text x="380" y="{y}" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">{price}</text>
    <text x="500" y="{y}" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">{avg}</text>
    <text x="620" y="{y}" font-family="Arial, sans-serif" font-size="13" fill="{pnl_color}" text-anchor="middle">{pnl}</text>
    <rect x="710" y="{row_y + 15}" width="80" height="25" fill="{ai_bg}" rx="3"/>
    <text x="750" y="{row_y + 33}" font-family="Arial, sans-serif" font-size="11" fill="#333333" text-anchor="middle">{ai}</text>
    <rect x="850" y="{row_y + 15}" width="80" height="25" fill="{setting_bg}" rx="3"/>
    <text x="890" y="{row_y + 33}" font-family="Arial, sans-serif" font-size="11" fill="#ffffff" text-anchor="middle">{setting}</text>
    <rect x="980" y="{row_y + 12}" width="60" height="28" fill="#2196F3" rx="3"/>
    <rect x="1050" y="{row_y + 12}" width="60" height="28" fill="{buy_btn}" rx="3"/>
    <rect x="1120" y="{row_y + 12}" width="60" height="28" fill="#F44336" rx="3"/>
    <text x="1010" y="{row_y + 31}" font-family="Arial, sans-serif" font-size="10" fill="#ffffff" text-anchor="middle">분석</text>
    <text x="1080" y="{row_y + 31}" font-family="Arial, sans-serif" font-size="10" fill="#ffffff" text-anchor="middle">매수</text>
    <text x="1150" y="{row_y + 31}" font-family="Arial, sans-serif" font-size="10" fill="#ffffff" text-anchor="middle">매도</text>
"""
            row_y += 45

        svg += """
    <text x="700" y="730" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">... 5개 종목 더 있음</text>
"""
        svg += SVGComponent.footer()
        return svg

    def create_progress(self) -> str:
        """진행 상황 표시 UI (1200x400)"""
        from blog.tools.components.base import SVGComponent

        cards = [
            (
                50,
                "#2196F3",
                "#E3F2FD",
                "🔍 전체 종목 AI 분석",
                217,
                "70%",
                "삼성전자 분석 중... (7/10)",
                "예상 남은 시간: 약 2분",
                "#1976D2",
                "#999999",
            ),
            (
                425,
                "#4CAF50",
                "#E8F5E9",
                "🛒 자동 매수 주문",
                124,
                "40%",
                "SK하이닉스 매수 주문 처리 중...",
                "✓ 삼성전자: 2건 주문 완료",
                "#2E7D32",
                "#4CAF50",
            ),
            (
                800,
                "#FF9800",
                "#FFF3E0",
                "⚡ 종목별 분석→매수→매도",
                155,
                "50%",
                "NAVER 매수 주문 중...",
                "현재 단계: 분석 → 매수 → 매도",
                "#EF6C00",
                "#FF9800",
            ),
        ]
        logs = [
            (
                "[10:32:15] ✓ 삼성전자 분석 완료 (decision: BUY, confidence: 75%)",
                "#666666",
            ),
            (
                "[10:32:16] ✓ 삼성전자 매수 주문 2건 접수 (68,000원 x 5주, 70,000원 x 5주)",
                "#666666",
            ),
            ("[10:32:18] ⏳ SK하이닉스 분석 시작...", "#666666"),
            ("[10:32:45] 🔄 SK하이닉스 분석 중 (70% 완료)", "#2196F3"),
        ]

        svg = SVGComponent.header(1200, 400)
        svg += SVGComponent.background(1200, 400, fill="#f8f9fa")
        svg += SVGComponent.title(1200, "실시간 진행 상황 표시 UI", y=40, font_size=24)

        for (
            x,
            color,
            bar_bg,
            title,
            bar_width,
            percent,
            status,
            detail,
            title_color,
            detail_color,
        ) in cards:
            svg += f"""
    <rect x="{x}" y="70" width="350" height="150" fill="#ffffff" stroke="{color}" stroke-width="2" rx="8"/>
    <text x="{x + 20}" y="100" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="{title_color}">{title}</text>
    <rect x="{x + 20}" y="115" width="310" height="25" fill="{bar_bg}" rx="5"/>
    <rect x="{x + 20}" y="115" width="{bar_width}" height="25" fill="{color}" rx="5"/>
    <text x="{x + 20 + bar_width // 2}" y="133" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#ffffff" text-anchor="middle">{percent}</text>
    <text x="{x + 20}" y="165" font-family="Arial, sans-serif" font-size="13" fill="#666666">{status}</text>
    <text x="{x + 20}" y="190" font-family="Arial, sans-serif" font-size="11" fill="{detail_color}">{detail}</text>
"""

        svg += """
    <rect x="50" y="240" width="1100" height="140" fill="#ffffff" stroke="#e0e0e0" stroke-width="1" rx="8"/>
    <text x="70" y="270" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#333333">📋 실행 로그</text>
    <rect x="70" y="285" width="1060" height="80" fill="#f5f5f5" rx="5"/>
"""
        for idx, (line, color) in enumerate(logs):
            svg += f'    <text x="85" y="{305 + (idx * 17)}" font-family="monospace" font-size="11" fill="{color}">{line}</text>\n'

        svg += SVGComponent.footer()
        return svg

    def create_flower(self) -> str:
        """Flower 모니터링 대시보드 (1200x600)"""
        from blog.tools.components.base import SVGComponent

        stats = [
            (30, "#E3F2FD", "#1976D2", "Active Tasks", "3", "#1565C0"),
            (230, "#E8F5E9", "#388E3C", "Succeeded", "47", "#1B5E20"),
            (430, "#FFEBEE", "#C62828", "Failed", "2", "#B71C1C"),
            (630, "#FFF3E0", "#F57C00", "Workers", "1", "#E65100"),
        ]
        task_rows = [
            (
                "kis.run_analysis_for_my_domestic_stocks",
                "PROGRESS",
                "#FFF3E0",
                "#EF6C00",
                "10:32:00",
                "45.2s",
            ),
            (
                "kis.execute_domestic_buy_order_task",
                "SUCCESS",
                "#E8F5E9",
                "#2E7D32",
                "10:31:45",
                "2.3s",
            ),
            (
                "kis.analyze_domestic_stock_task",
                "SUCCESS",
                "#E8F5E9",
                "#2E7D32",
                "10:31:20",
                "23.5s",
            ),
            (
                "kis.execute_overseas_buy_order_task",
                "FAILURE",
                "#FFEBEE",
                "#C62828",
                "10:30:55",
                "1.2s",
            ),
        ]

        svg = SVGComponent.header(1200, 600)
        svg += SVGComponent.background(1200, 600, fill="#f5f5f5")
        svg += """
    <rect x="0" y="0" width="1200" height="50" fill="#2c2c2c"/>
    <circle cx="20" cy="25" r="7" fill="#ff5f56"/>
    <circle cx="42" cy="25" r="7" fill="#ffbd2e"/>
    <circle cx="64" cy="25" r="7" fill="#27ca40"/>
    <rect x="100" y="12" width="500" height="26" rx="13" fill="#444444"/>
    <text x="120" y="31" font-family="Arial, sans-serif" font-size="12" fill="#ffffff">http://localhost:5555/</text>
    <rect x="0" y="50" width="1200" height="60" fill="#1a1a2e"/>
    <text x="30" y="88" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#ffffff">🌸 Flower</text>
    <text x="140" y="88" font-family="Arial, sans-serif" font-size="14" fill="#a0a0a0">Celery monitoring</text>
    <rect x="400" y="70" width="100" height="30" fill="#333355"/>
    <text x="450" y="92" font-family="Arial, sans-serif" font-size="13" fill="#ffffff" text-anchor="middle">Dashboard</text>
    <text x="550" y="92" font-family="Arial, sans-serif" font-size="13" fill="#a0a0a0" text-anchor="middle">Tasks</text>
    <text x="640" y="92" font-family="Arial, sans-serif" font-size="13" fill="#a0a0a0" text-anchor="middle">Workers</text>
    <rect x="0" y="110" width="1200" height="490" fill="#ffffff"/>
"""

        for x, fill, stroke, label, value, value_color in stats:
            svg += f"""
    <rect x="{x}" y="130" width="180" height="100" fill="{fill}" stroke="{stroke}" stroke-width="1" rx="8"/>
    <text x="{x + 90}" y="165" font-family="Arial, sans-serif" font-size="14" fill="{stroke}" text-anchor="middle">{label}</text>
    <text x="{x + 90}" y="205" font-family="Arial, sans-serif" font-size="36" font-weight="bold" fill="{value_color}" text-anchor="middle">{value}</text>
"""

        svg += """
    <rect x="30" y="250" width="780" height="320" fill="#ffffff" stroke="#e0e0e0" stroke-width="1" rx="8"/>
    <text x="50" y="280" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#333333">Recent Tasks</text>
    <rect x="45" y="295" width="750" height="30" fill="#f5f5f5"/>
    <text x="65" y="315" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#666666">Name</text>
    <text x="350" y="315" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#666666">State</text>
    <text x="480" y="315" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#666666">Received</text>
    <text x="650" y="315" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#666666">Runtime</text>
"""

        row_line = 325
        for name, state, bg, fg, received, runtime in task_rows:
            y = row_line + 25
            svg += f"""
    <line x1="45" y1="{row_line}" x2="795" y2="{row_line}" stroke="#e0e0e0"/>
    <text x="65" y="{y}" font-family="monospace" font-size="11" fill="#333333">{name}</text>
    <rect x="340" y="{row_line + 12}" width="70" height="20" fill="{bg}" rx="3"/>
    <text x="375" y="{row_line + 27}" font-family="Arial, sans-serif" font-size="10" fill="{fg}" text-anchor="middle">{state}</text>
    <text x="480" y="{y}" font-family="Arial, sans-serif" font-size="11" fill="#666666">{received}</text>
    <text x="650" y="{y}" font-family="Arial, sans-serif" font-size="11" fill="#666666">{runtime}</text>
"""
            row_line += 40

        svg += """
    <rect x="830" y="250" width="340" height="150" fill="#ffffff" stroke="#e0e0e0" stroke-width="1" rx="8"/>
    <text x="850" y="280" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#333333">Worker Status</text>
    <circle cx="865" cy="315" r="8" fill="#4CAF50"/>
    <text x="885" y="320" font-family="Arial, sans-serif" font-size="14" fill="#333333">celery@raspberrypi</text>
    <text x="865" y="350" font-family="Arial, sans-serif" font-size="12" fill="#666666">Concurrency: 4</text>
    <text x="865" y="370" font-family="Arial, sans-serif" font-size="12" fill="#666666">Active: 3 | Processed: 52</text>
    <text x="865" y="390" font-family="Arial, sans-serif" font-size="12" fill="#666666">Pool: prefork</text>
"""
        svg += SVGComponent.footer()
        return svg


if __name__ == "__main__":
    generator = KISTradingImages("kis_trading")
    generator.generate()
