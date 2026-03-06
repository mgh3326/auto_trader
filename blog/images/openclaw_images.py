#!/usr/bin/env python3
"""
OpenClaw 통합 블로그 이미지 생성

Infra-5편: OpenClaw + FastAPI 콜백으로 비동기 LLM 파이프라인 만들기
"""

from blog.tools.image_generator import BlogImageGenerator, ThumbnailTemplate


class OpenClawImages(BlogImageGenerator):
    """OpenClaw 통합 블로그 이미지 생성기"""

    def get_images(self):
        """생성할 이미지 목록 반환"""
        return [
            ("thumbnail", 1200, 630, self.create_thumbnail),
            ("architecture", 1400, 900, self.create_architecture),
            ("ssh_tunnel", 1200, 700, self.create_ssh_tunnel),
            ("auth_flow", 1200, 600, self.create_auth_flow),
        ]

    def create_thumbnail(self) -> str:
        """썸네일 이미지 (1200x630)"""
        return ThumbnailTemplate.create(
            title_line1="OpenClaw + FastAPI",
            title_line2="비동기 LLM 파이프라인",
            subtitle="Raspberry Pi에서 GPT 분석, 웹훅/콜백으로 결과 적재",
            icons=[
                ("🤖", "OpenClaw", "#9C27B0"),
                ("🔗", "Webhook", "#2196F3"),
                ("📊", "Callback", "#4CAF50"),
                ("🗄️", "DB", "#FF9800"),
            ],
            tech_stack="FastAPI • Raspberry Pi • SSH Tunnel • PostgreSQL",
            bg_gradient=("#1a1a2e", "#16213e", "#0f3460"),
            accent_color="#e94560",
        )

    def create_architecture(self) -> str:
        """아키텍처 다이어그램 (1400x900)"""
        width, height = 1400, 900

        # 그라데이션 정의
        gradient = self.gradient_defs(
            "bgGrad",
            [
                (0, "#f8fafc"),
                (100, "#e2e8f0"),
            ],
        )

        svg = self.svg_header(
            width,
            height,
            defs=gradient
            + """
        <marker id="arrow" markerWidth="12" markerHeight="8" refX="12" refY="4" orient="auto">
            <polygon points="0 0, 12 4, 0 8" fill="#64748b"/>
        </marker>
        <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="2" dy="2" stdDeviation="3" flood-opacity="0.1"/>
        </filter>
        """,
        )

        # 배경
        svg += self.rect(0, 0, width, height, fill="url(#bgGrad)", stroke="none", rx=0)

        # 제목
        svg += self.text(
            width // 2,
            50,
            "OpenClaw 비동기 분석 파이프라인",
            font_size=32,
            weight="bold",
            anchor="middle",
            fill="#1e293b",
        )
        svg += self.text(
            width // 2,
            85,
            "auto_trader → OpenClaw → callback → DB",
            font_size=18,
            anchor="middle",
            fill="#64748b",
        )

        # ========== 왼쪽: Auto Trader 영역 ==========
        # 박스
        svg += '<rect x="50" y="140" width="350" height="700" rx="16" fill="#ffffff" stroke="#3b82f6" stroke-width="2" filter="url(#shadow)"/>'
        svg += self.text(
            225,
            175,
            "Auto Trader (FastAPI)",
            font_size=20,
            weight="bold",
            anchor="middle",
            fill="#1e40af",
        )

        # OpenClawClient
        svg += self.rect(80, 210, 290, 90, fill="#dbeafe", stroke="#3b82f6", rx=12)
        svg += self.text(
            225,
            245,
            "OpenClawClient",
            font_size=16,
            weight="bold",
            anchor="middle",
            fill="#1e40af",
        )
        svg += self.text(
            225,
            270,
            "request_analysis()",
            font_size=14,
            anchor="middle",
            fill="#3b82f6",
        )
        svg += self.text(
            225,
            290,
            "uuid4() → request_id 생성",
            font_size=12,
            anchor="middle",
            fill="#64748b",
        )

        # Message Builder
        svg += self.rect(80, 330, 290, 80, fill="#e0e7ff", stroke="#6366f1", rx=12)
        svg += self.text(
            225,
            360,
            "_build_openclaw_message()",
            font_size=14,
            weight="bold",
            anchor="middle",
            fill="#4338ca",
        )
        svg += self.text(
            225,
            385,
            "프롬프트 + 콜백URL + 스키마",
            font_size=12,
            anchor="middle",
            fill="#64748b",
        )

        # Callback Router
        svg += self.rect(80, 520, 290, 90, fill="#dcfce7", stroke="#22c55e", rx=12)
        svg += self.text(
            225,
            555,
            "/api/v1/openclaw/callback",
            font_size=14,
            weight="bold",
            anchor="middle",
            fill="#166534",
        )
        svg += self.text(
            225,
            580,
            "POST: 분석 결과 수신",
            font_size=12,
            anchor="middle",
            fill="#64748b",
        )

        # DB 저장
        svg += self.rect(80, 640, 290, 80, fill="#fef3c7", stroke="#f59e0b", rx=12)
        svg += self.text(
            225,
            670,
            "StockAnalysisResult",
            font_size=14,
            weight="bold",
            anchor="middle",
            fill="#b45309",
        )
        svg += self.text(
            225,
            695,
            "PostgreSQL 영구 저장",
            font_size=12,
            anchor="middle",
            fill="#64748b",
        )

        # Auth Middleware
        svg += self.rect(80, 750, 290, 60, fill="#fce7f3", stroke="#ec4899", rx=12)
        svg += self.text(
            225,
            780,
            "AuthMiddleware",
            font_size=14,
            weight="bold",
            anchor="middle",
            fill="#be185d",
        )
        svg += self.text(
            225,
            800,
            "/openclaw/callback allowlist",
            font_size=11,
            anchor="middle",
            fill="#64748b",
        )

        # ========== 중앙: 화살표 및 설명 ==========
        # 요청 화살표 (Client → OpenClaw)
        svg += '<path d="M370 260 L490 260 L490 350 L580 350" fill="none" stroke="#3b82f6" stroke-width="2" marker-end="url(#arrow)"/>'
        svg += self.text(
            430,
            245,
            "① POST /hooks/agent",
            font_size=12,
            anchor="middle",
            fill="#3b82f6",
        )
        svg += self.text(
            430, 320, "sessionKey:", font_size=11, anchor="middle", fill="#64748b"
        )
        svg += self.text(
            430,
            335,
            "auto-trader:openclaw:{id}",
            font_size=10,
            anchor="middle",
            fill="#64748b",
        )

        # 콜백 화살표 (OpenClaw → Callback)
        svg += '<path d="M820 550 L490 550 L490 560 L370 560" fill="none" stroke="#22c55e" stroke-width="2" marker-end="url(#arrow)"/>'
        svg += self.text(
            595,
            535,
            "④ POST callback_url",
            font_size=12,
            anchor="middle",
            fill="#22c55e",
        )
        svg += self.text(
            595,
            575,
            "Authorization: Bearer {token}",
            font_size=10,
            anchor="middle",
            fill="#64748b",
        )

        # DB 저장 화살표
        svg += '<line x1="225" y1="610" x2="225" y2="630" stroke="#f59e0b" stroke-width="2" marker-end="url(#arrow)"/>'
        svg += self.text(
            245, 625, "⑤ INSERT", font_size=11, anchor="start", fill="#f59e0b"
        )

        # ========== 오른쪽: OpenClaw / Raspberry Pi ==========
        svg += '<rect x="580" y="140" width="380" height="480" rx="16" fill="#faf5ff" stroke="#9333ea" stroke-width="2" filter="url(#shadow)"/>'
        svg += self.text(
            770,
            175,
            "OpenClaw (Raspberry Pi 5)",
            font_size=20,
            weight="bold",
            anchor="middle",
            fill="#7c3aed",
        )

        # Gateway
        svg += self.rect(610, 210, 320, 80, fill="#f3e8ff", stroke="#a855f7", rx=12)
        svg += self.text(
            770,
            245,
            "Gateway /hooks/agent",
            font_size=16,
            weight="bold",
            anchor="middle",
            fill="#7c3aed",
        )
        svg += self.text(
            770, 270, "Bearer token 인증", font_size=12, anchor="middle", fill="#64748b"
        )

        # 화살표: Gateway → Agent
        svg += '<line x1="770" y1="290" x2="770" y2="320" stroke="#a855f7" stroke-width="2" marker-end="url(#arrow)"/>'
        svg += self.text(
            790, 310, "② 큐잉", font_size=11, anchor="start", fill="#a855f7"
        )

        # Agent 처리
        svg += self.rect(610, 330, 320, 130, fill="#ede9fe", stroke="#8b5cf6", rx=12)
        svg += self.text(
            770,
            365,
            "Agent (Robin)",
            font_size="16",
            weight="bold",
            anchor="middle",
            fill="#6d28d9",
        )
        svg += self.text(
            770,
            395,
            "③ GPT/Claude 분석 실행",
            font_size=14,
            anchor="middle",
            fill="#7c3aed",
        )
        svg += self.text(
            770,
            425,
            "message 파싱 → LLM 호출",
            font_size=12,
            anchor="middle",
            fill="#64748b",
        )
        svg += self.text(
            770, 445, "→ JSON 결과 생성", font_size=12, anchor="middle", fill="#64748b"
        )

        # 콜백 전송
        svg += self.rect(610, 490, 320, 100, fill="#fef3c7", stroke="#f59e0b", rx=12)
        svg += self.text(
            770,
            525,
            "HTTP Callback 전송",
            font_size="16",
            weight="bold",
            anchor="middle",
            fill="#b45309",
        )
        svg += self.text(
            770,
            555,
            "callback_url + token",
            font_size=12,
            anchor="middle",
            fill="#64748b",
        )
        svg += self.text(
            770,
            575,
            "JSON: decision, confidence, ...",
            font_size=11,
            anchor="middle",
            fill="#64748b",
        )

        # ========== 하단: 범례 ==========
        svg += self.rect(1000, 700, 350, 150, fill="#ffffff", stroke="#cbd5e1", rx=12)
        svg += self.text(
            1175,
            730,
            "상관관계 키",
            font_size=16,
            weight="bold",
            anchor="middle",
            fill="#334155",
        )

        # request_id
        svg += self.rect(1020, 750, 140, 35, fill="#dbeafe", stroke="#3b82f6", rx=6)
        svg += self.text(
            1090, 773, "request_id", font_size=12, anchor="middle", fill="#1e40af"
        )

        # sessionKey
        svg += self.rect(1180, 750, 150, 35, fill="#f3e8ff", stroke="#a855f7", rx=6)
        svg += self.text(
            1255, 773, "sessionKey", font_size=12, anchor="middle", fill="#7c3aed"
        )

        svg += self.text(
            1175,
            820,
            "같은 UUID로 요청/응답 매칭",
            font_size=12,
            anchor="middle",
            fill="#64748b",
        )

        svg += self.svg_footer()
        return svg

    def create_ssh_tunnel(self) -> str:
        """SSH 터널링 다이어그램 (1200x700)"""
        width, height = 1200, 700

        svg = self.svg_header(
            width,
            height,
            defs="""
        <marker id="arrow2" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#0ea5e9"/>
        </marker>
        <marker id="arrow3" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#22c55e"/>
        </marker>
        """,
        )

        # 배경
        svg += self.rect(0, 0, width, height, fill="#f1f5f9", stroke="none", rx=0)

        # 제목
        svg += self.text(
            width // 2,
            45,
            "개발 환경 SSH 터널 구성",
            font_size=28,
            weight="bold",
            anchor="middle",
            fill="#1e293b",
        )
        svg += self.text(
            width // 2,
            75,
            "로컬 ↔ Raspberry Pi 양방향 포트 포워딩",
            font_size=16,
            anchor="middle",
            fill="#64748b",
        )

        # ========== 왼쪽: 로컬 머신 ==========
        svg += '<rect x="50" y="120" width="380" height="450" rx="16" fill="#ffffff" stroke="#0284c7" stroke-width="2"/>'
        svg += self.text(
            240,
            155,
            "🖥️ 로컬 머신 (macOS)",
            font_size="18",
            weight="bold",
            anchor="middle",
            fill="#0369a1",
        )

        # FastAPI
        svg += self.rect(80, 190, 320, 80, fill="#e0f2fe", stroke="#0ea5e9", rx=10)
        svg += self.text(
            240,
            225,
            "FastAPI (auto_trader)",
            font_size=14,
            weight="bold",
            anchor="middle",
            fill="#0369a1",
        )
        svg += self.text(
            240, 250, "localhost:18000", font_size=14, anchor="middle", fill="#0ea5e9"
        )

        # 포트 바인딩
        svg += self.rect(80, 300, 320, 70, fill="#fef3c7", stroke="#f59e0b", rx=10)
        svg += self.text(
            240,
            330,
            "-R 127.0.0.1:18000",
            font_size=13,
            weight="bold",
            anchor="middle",
            fill="#b45309",
        )
        svg += self.text(
            240,
            350,
            "Pi에서 콜백 받을 주소 노출",
            font_size=12,
            anchor="middle",
            fill="#78716c",
        )

        svg += self.rect(80, 400, 320, 70, fill="#dcfce7", stroke="#22c55e", rx=10)
        svg += self.text(
            240,
            430,
            "-L 127.0.0.1:18789",
            font_size=13,
            weight="bold",
            anchor="middle",
            fill="#166534",
        )
        svg += self.text(
            240,
            450,
            "Pi의 OpenClaw 훅 접근",
            font_size=12,
            anchor="middle",
            fill="#78716c",
        )

        # 명령어 박스
        svg += self.rect(80, 490, 320, 60, fill="#f1f5f9", stroke="#94a3b8", rx=8)
        svg += self.text(
            240,
            510,
            "uv run uvicorn app.main:api",
            font_size=11,
            anchor="middle",
            fill="#475569",
        )
        svg += self.text(
            240,
            530,
            "--host 127.0.0.1 --port 18000",
            font_size=11,
            anchor="middle",
            fill="#475569",
        )

        # ========== 중앙: SSH 터널 ==========
        svg += self.rect(
            470, 220, 260, 260, fill="#faf5ff", stroke="#9333ea", stroke_width=3, rx=16
        )
        svg += self.text(
            600,
            260,
            "🔐 SSH 터널",
            font_size=18,
            weight="bold",
            anchor="middle",
            fill="#7c3aed",
        )

        svg += self.text(
            600, 310, "ssh -p 2022 -N -T", font_size=12, anchor="middle", fill="#64748b"
        )
        svg += self.text(
            600,
            335,
            "-o ExitOnForwardFailure=yes",
            font_size=11,
            anchor="middle",
            fill="#64748b",
        )

        # 터널 방향
        svg += self.rect(490, 370, 220, 40, fill="#e0f2fe", stroke="#0ea5e9", rx=8)
        svg += self.text(
            600,
            395,
            "-L :18789 (로컬→Pi)",
            font_size=12,
            weight="bold",
            anchor="middle",
            fill="#0369a1",
        )

        svg += self.rect(490, 420, 220, 40, fill="#dcfce7", stroke="#22c55e", rx=8)
        svg += self.text(
            600,
            445,
            "-R :18000 (Pi→로컬)",
            font_size=12,
            weight="bold",
            anchor="middle",
            fill="#166534",
        )

        # ========== 오른쪽: Raspberry Pi ==========
        svg += '<rect x="770" y="120" width="380" height="450" rx="16" fill="#ffffff" stroke="#dc2626" stroke-width="2"/>'
        svg += self.text(
            960,
            155,
            "🍓 Raspberry Pi 5",
            font_size=18,
            weight="bold",
            anchor="middle",
            fill="#b91c1c",
        )

        # OpenClaw
        svg += self.rect(800, 190, 320, 80, fill="#fef2f2", stroke="#ef4444", rx=10)
        svg += self.text(
            960,
            225,
            "OpenClaw Gateway",
            font_size=14,
            weight="bold",
            anchor="middle",
            fill="#b91c1c",
        )
        svg += self.text(
            960,
            250,
            "localhost:18789 → /hooks/agent",
            font_size=13,
            anchor="middle",
            fill="#dc2626",
        )

        svg += self.rect(800, 300, 320, 70, fill="#fce7f3", stroke="#ec4899", rx=10)
        svg += self.text(
            960,
            330,
            "Agent (Robin)",
            font_size=14,
            weight="bold",
            anchor="middle",
            fill="#be185d",
        )
        svg += self.text(
            960,
            350,
            "GPT/Claude 분석 실행",
            font_size=12,
            anchor="middle",
            fill="#78716c",
        )

        # 콜백 경로
        svg += self.rect(800, 400, 320, 70, fill="#dcfce7", stroke="#22c55e", rx=10)
        svg += self.text(
            960,
            430,
            "콜백 → 127.0.0.1:18000",
            font_size=13,
            weight="bold",
            anchor="middle",
            fill="#166534",
        )
        svg += self.text(
            960,
            450,
            "터널 통해 로컬 FastAPI 도달",
            font_size=12,
            anchor="middle",
            fill="#78716c",
        )

        # SSH Server
        svg += self.rect(800, 490, 320, 60, fill="#f3e8ff", stroke="#a855f7", rx=8)
        svg += self.text(
            960,
            520,
            "SSH Server: port 2022",
            font_size=13,
            anchor="middle",
            fill="#7c3aed",
        )

        # ========== 화살표 ==========
        # 로컬 → SSH (요청)
        svg += '<path d="M400 345 L470 345" fill="none" stroke="#0ea5e9" stroke-width="3" marker-end="url(#arrow2)"/>'

        # SSH → Pi (요청)
        svg += '<path d="M730 390 L770 390" fill="none" stroke="#0ea5e9" stroke-width="3" marker-end="url(#arrow2)"/>'

        # Pi → SSH (응답)
        svg += '<path d="M770 440 L730 440" fill="none" stroke="#22c55e" stroke-width="3" marker-end="url(#arrow3)"/>'

        # SSH → 로컬 (응답)
        svg += '<path d="M470 440 L400 440" fill="none" stroke="#22c55e" stroke-width="3" marker-end="url(#arrow3)"/>'

        # ========== 하단: 명령어 ==========
        svg += self.rect(50, 600, 1100, 80, fill="#1e293b", stroke="none", rx=12)
        svg += self.text(
            600,
            630,
            "ssh -p 2022 -N -T -o ExitOnForwardFailure=yes \\",
            font_size=14,
            anchor="middle",
            fill="#94a3b8",
        )
        svg += self.text(
            600,
            655,
            "-L 127.0.0.1:18789:127.0.0.1:18789 -R 127.0.0.1:18000:127.0.0.1:18000 user@pi-host",
            font_size=14,
            anchor="middle",
            fill="#38bdf8",
        )

        svg += self.svg_footer()
        return svg

    def create_auth_flow(self) -> str:
        """인증 플로우 다이어그램 (1200x600)"""
        width, height = 1200, 600

        svg = self.svg_header(
            width,
            height,
            defs="""
        <marker id="arrow4" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#3b82f6"/>
        </marker>
        """,
        )

        # 배경
        svg += self.rect(0, 0, width, height, fill="#fafafa", stroke="none", rx=0)

        # 제목
        svg += self.text(
            width // 2,
            40,
            "콜백 인증 플로우",
            font_size=26,
            weight="bold",
            anchor="middle",
            fill="#1e293b",
        )

        # ========== 엔티티 박스 ==========
        entities = [
            (100, "auto_trader", "#3b82f6", "#dbeafe"),
            (400, "OpenClaw", "#9333ea", "#f3e8ff"),
            (700, "AuthMiddleware", "#ec4899", "#fce7f3"),
            (1000, "Callback Router", "#22c55e", "#dcfce7"),
        ]

        for x, name, stroke, fill in entities:
            svg += self.rect(x - 70, 80, 140, 50, fill=fill, stroke=stroke, rx=8)
            svg += self.text(
                x, 110, name, font_size=13, weight="bold", anchor="middle", fill=stroke
            )

        # ========== 라이프라인 ==========
        for x, _, stroke, _ in entities:
            svg += f'<line x1="{x}" y1="130" x2="{x}" y2="550" stroke="{stroke}" stroke-width="2" stroke-dasharray="5,5"/>'

        # ========== 메시지 흐름 ==========
        y_base = 170

        # 1. 요청 생성
        svg += self.rect(40, y_base, 120, 45, fill="#dbeafe", stroke="#3b82f6", rx=6)
        svg += self.text(
            100,
            y_base + 18,
            "message 생성",
            font_size=11,
            weight="bold",
            anchor="middle",
            fill="#1e40af",
        )
        svg += self.text(
            100,
            y_base + 35,
            "callback_token 포함",
            font_size=10,
            anchor="middle",
            fill="#3b82f6",
        )

        # 2. POST /hooks/agent
        y = y_base + 70
        svg += f'<line x1="100" y1="{y}" x2="390" y2="{y}" stroke="#3b82f6" stroke-width="2" marker-end="url(#arrow4)"/>'
        svg += self.text(
            245,
            y - 10,
            "POST /hooks/agent",
            font_size=12,
            anchor="middle",
            fill="#3b82f6",
        )
        svg += self.text(
            245,
            y + 15,
            "Authorization: Bearer {OPENCLAW_TOKEN}",
            font_size=10,
            anchor="middle",
            fill="#64748b",
        )

        # 3. 비동기 처리
        svg += self.rect(340, y + 35, 120, 40, fill="#f3e8ff", stroke="#9333ea", rx=6)
        svg += self.text(
            400,
            y + 55,
            "비동기 LLM 분석",
            font_size=11,
            anchor="middle",
            fill="#7c3aed",
        )

        # 4. 202 Accepted
        y2 = y + 100
        svg += f'<line x1="390" y1="{y2}" x2="110" y2="{y2}" stroke="#9333ea" stroke-width="2" stroke-dasharray="3,3" marker-end="url(#arrow4)"/>'
        svg += self.text(
            250,
            y2 - 8,
            "202 Accepted (즉시 응답)",
            font_size=11,
            anchor="middle",
            fill="#9333ea",
        )

        # 5. 분석 완료 후 콜백
        y3 = y2 + 70
        svg += f'<line x1="400" y1="{y3}" x2="690" y2="{y3}" stroke="#9333ea" stroke-width="2" marker-end="url(#arrow4)"/>'
        svg += self.text(
            545,
            y3 - 10,
            "POST /api/v1/openclaw/callback",
            font_size=12,
            anchor="middle",
            fill="#9333ea",
        )
        svg += self.text(
            545,
            y3 + 15,
            "Authorization: Bearer {CALLBACK_TOKEN}",
            font_size=10,
            anchor="middle",
            fill="#64748b",
        )

        # 6. Allowlist 체크
        svg += self.rect(640, y3 + 30, 120, 50, fill="#fce7f3", stroke="#ec4899", rx=6)
        svg += self.text(
            700,
            y3 + 50,
            "PUBLIC_API_PATHS",
            font_size=10,
            weight="bold",
            anchor="middle",
            fill="#be185d",
        )
        svg += self.text(
            700,
            y3 + 68,
            "세션 인증 우회",
            font_size=10,
            anchor="middle",
            fill="#ec4899",
        )

        # 7. 토큰 검증
        y4 = y3 + 110
        svg += f'<line x1="700" y1="{y4}" x2="990" y2="{y4}" stroke="#ec4899" stroke-width="2" marker-end="url(#arrow4)"/>'
        svg += self.text(
            845, y4 - 8, "토큰 전달", font_size=11, anchor="middle", fill="#ec4899"
        )

        svg += self.rect(940, y4 + 15, 120, 50, fill="#dcfce7", stroke="#22c55e", rx=6)
        svg += self.text(
            1000,
            y4 + 35,
            "hmac.compare_digest",
            font_size=10,
            weight="bold",
            anchor="middle",
            fill="#166534",
        )
        svg += self.text(
            1000, y4 + 53, "토큰 비교", font_size=10, anchor="middle", fill="#22c55e"
        )

        # 8. DB 저장
        y5 = y4 + 90
        svg += self.rect(940, y5, 120, 40, fill="#fef3c7", stroke="#f59e0b", rx=6)
        svg += self.text(
            1000,
            y5 + 25,
            "DB 저장",
            font_size=12,
            weight="bold",
            anchor="middle",
            fill="#b45309",
        )

        # 9. 200 OK 응답
        y6 = y5 + 60
        svg += f'<line x1="990" y1="{y6}" x2="410" y2="{y6}" stroke="#22c55e" stroke-width="2" stroke-dasharray="3,3" marker-end="url(#arrow4)"/>'
        svg += self.text(
            700,
            y6 - 8,
            '200 OK {"status": "ok", "analysis_result_id": 123}',
            font_size=11,
            anchor="middle",
            fill="#22c55e",
        )

        # ========== 범례 ==========
        svg += self.rect(30, 540, 280, 50, fill="#ffffff", stroke="#cbd5e1", rx=8)
        svg += self.text(
            50, 565, "🔐 OPENCLAW_TOKEN:", font_size=11, anchor="start", fill="#1e293b"
        )
        svg += self.text(
            165, 565, "Gateway 인증", font_size=11, anchor="start", fill="#64748b"
        )
        svg += self.text(
            50, 582, "🔑 CALLBACK_TOKEN:", font_size=11, anchor="start", fill="#1e293b"
        )
        svg += self.text(
            170, 582, "콜백 인증", font_size=11, anchor="start", fill="#64748b"
        )

        svg += self.svg_footer()
        return svg


if __name__ == "__main__":
    OpenClawImages("openclaw").generate()
