#!/usr/bin/env python3
"""
OpenClaw 통합 블로그 이미지 생성

Infra-5편: OpenClaw + FastAPI 콜백으로 비동기 LLM 파이프라인 만들기
"""

from typing import override

from blog.tools.components.base import SVGComponent
from blog.tools.components.flow_diagram import FlowDiagram
from blog.tools.components.thumbnail import ThumbnailTemplate
from blog.tools.image_generator import BlogImageGenerator


class OpenClawImages(BlogImageGenerator):
    """OpenClaw 통합 블로그 이미지 생성기"""

    @override
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
        extra_defs = """
        <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" style="stop-color:#f8fafc;stop-opacity:1" />
            <stop offset="100%" style="stop-color:#e2e8f0;stop-opacity:1" />
        </linearGradient>
        <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="2" dy="2" stdDeviation="3" flood-opacity="0.12"/>
        </filter>
        """

        svg = SVGComponent.header(width, height, extra_defs=extra_defs)
        svg += '    <rect width="1400" height="900" fill="url(#bgGrad)"/>\n'
        svg += SVGComponent.title(
            width, "OpenClaw 비동기 분석 파이프라인", y=52, font_size=32, fill="#1e293b"
        )
        svg += (
            '    <text x="700" y="85" font-family="Arial, sans-serif" font-size="18" '
            'fill="#64748b" text-anchor="middle">auto_trader → OpenClaw → callback → DB</text>\n'
        )

        svg += (
            '    <rect x="50" y="135" width="360" height="705" rx="16" fill="#ffffff" '
            'stroke="#3b82f6" stroke-width="2" filter="url(#shadow)"/>\n'
        )
        svg += (
            '    <text x="230" y="170" font-family="Arial, sans-serif" font-size="20" '
            'font-weight="bold" fill="#1e40af" text-anchor="middle">Auto Trader (FastAPI)</text>\n'
        )
        svg += (
            '    <rect x="590" y="135" width="370" height="485" rx="16" fill="#faf5ff" '
            'stroke="#9333ea" stroke-width="2" filter="url(#shadow)"/>\n'
        )
        svg += (
            '    <text x="775" y="170" font-family="Arial, sans-serif" font-size="20" '
            'font-weight="bold" fill="#7c3aed" text-anchor="middle">OpenClaw (Raspberry Pi 5)</text>\n'
        )

        left_nodes = [
            (85, 210, 290, 90, "OpenClawClient", "#3b82f6"),
            (85, 330, 290, 80, "_build_openclaw_message", "#6366f1"),
            (85, 520, 290, 90, "/api/v1/openclaw/callback", "#22c55e"),
            (85, 640, 290, 80, "StockAnalysisResult", "#f59e0b"),
            (85, 750, 290, 60, "AuthMiddleware", "#ec4899"),
        ]
        left_edges = [
            (0, 1, "프롬프트 + callback_url"),
            (1, 2, "request_id 전달"),
            (2, 3, "⑤ INSERT"),
            (2, 4, "allowlist 검사"),
        ]
        svg += FlowDiagram.create(left_nodes, left_edges)

        right_nodes = [
            (615, 210, 320, 80, "Gateway /hooks/agent", "#a855f7"),
            (615, 330, 320, 130, "Agent (Robin)", "#8b5cf6"),
            (615, 490, 320, 100, "HTTP Callback 전송", "#f59e0b"),
        ]
        right_edges = [
            (0, 1, "② 큐잉"),
            (1, 2, "③ 분석 JSON 생성"),
        ]
        svg += FlowDiagram.create(right_nodes, right_edges)

        bridge_nodes = [
            (400, 245, 170, 60, "POST /hooks/agent", "#3b82f6"),
            (400, 525, 170, 60, "POST callback_url", "#22c55e"),
        ]
        bridge_edges = [
            (0, 1, "① 요청 / ④ 응답"),
        ]
        svg += FlowDiagram.create(bridge_nodes, bridge_edges)

        svg += (
            '    <text x="485" y="330" font-family="Arial, sans-serif" font-size="10" '
            'fill="#64748b" text-anchor="middle">sessionKey: auto-trader:openclaw:{id}</text>\n'
        )
        svg += (
            '    <text x="485" y="602" font-family="Arial, sans-serif" font-size="10" '
            'fill="#64748b" text-anchor="middle">Authorization: Bearer {token}</text>\n'
        )
        svg += (
            '    <text x="775" y="270" font-family="Arial, sans-serif" font-size="12" '
            'fill="#64748b" text-anchor="middle">Bearer token 인증</text>\n'
        )
        svg += (
            '    <text x="775" y="398" font-family="Arial, sans-serif" font-size="14" '
            'fill="#ede9fe" text-anchor="middle">GPT/Claude 분석 실행</text>\n'
        )
        svg += (
            '    <text x="775" y="428" font-family="Arial, sans-serif" font-size="12" '
            'fill="#d8b4fe" text-anchor="middle">message 파싱 → LLM 호출</text>\n'
        )
        svg += (
            '    <text x="775" y="560" font-family="Arial, sans-serif" font-size="12" '
            'fill="#64748b" text-anchor="middle">JSON: decision, confidence, reason</text>\n'
        )

        svg += (
            '    <rect x="995" y="700" width="355" height="150" rx="12" fill="#ffffff" '
            'stroke="#cbd5e1" stroke-width="2"/>\n'
        )
        svg += (
            '    <text x="1172" y="730" font-family="Arial, sans-serif" font-size="16" '
            'font-weight="bold" fill="#334155" text-anchor="middle">상관관계 키</text>\n'
        )
        legend_nodes = [
            (1020, 750, 140, 35, "request_id", "#3b82f6"),
            (1180, 750, 150, 35, "sessionKey", "#a855f7"),
        ]
        svg += FlowDiagram.create(legend_nodes, [])
        svg += (
            '    <text x="1172" y="820" font-family="Arial, sans-serif" font-size="12" '
            'fill="#64748b" text-anchor="middle">같은 UUID로 요청/응답 매칭</text>\n'
        )

        svg += SVGComponent.footer()
        return svg

    def create_ssh_tunnel(self) -> str:
        """SSH 터널링 다이어그램 (1200x700)"""
        width, height = 1200, 700
        svg = SVGComponent.header(width, height)
        svg += SVGComponent.background(width, height, fill="#f1f5f9")
        svg += SVGComponent.title(
            width, "개발 환경 SSH 터널 구성", y=45, font_size=28, fill="#1e293b"
        )
        svg += (
            '    <text x="600" y="75" font-family="Arial, sans-serif" font-size="16" '
            'fill="#64748b" text-anchor="middle">로컬 ↔ Raspberry Pi 양방향 포트 포워딩</text>\n'
        )

        svg += (
            '    <rect x="50" y="120" width="380" height="450" rx="16" fill="#ffffff" '
            'stroke="#0284c7" stroke-width="2"/>\n'
        )
        svg += (
            '    <text x="240" y="155" font-family="Arial, sans-serif" font-size="18" '
            'font-weight="bold" fill="#0369a1" text-anchor="middle">로컬 머신 (macOS)</text>\n'
        )
        svg += (
            '    <rect x="770" y="120" width="380" height="450" rx="16" fill="#ffffff" '
            'stroke="#dc2626" stroke-width="2"/>\n'
        )
        svg += (
            '    <text x="960" y="155" font-family="Arial, sans-serif" font-size="18" '
            'font-weight="bold" fill="#b91c1c" text-anchor="middle">Raspberry Pi 5</text>\n'
        )

        local_nodes = [
            (80, 190, 320, 80, "FastAPI (localhost:18000)", "#0ea5e9"),
            (80, 300, 320, 70, "-R 127.0.0.1:18000", "#f59e0b"),
            (80, 400, 320, 70, "-L 127.0.0.1:18789", "#22c55e"),
            (80, 490, 320, 60, "uvicorn app.main:api", "#94a3b8"),
        ]
        local_edges = [
            (0, 1, "Pi에서 콜백 수신"),
            (1, 2, "Pi 훅 접근"),
            (2, 3, "개발 서버 실행"),
        ]
        svg += FlowDiagram.create(local_nodes, local_edges)

        tunnel_nodes = [
            (490, 250, 220, 70, "SSH Tunnel", "#7c3aed"),
            (490, 350, 220, 50, "-L :18789", "#0ea5e9"),
            (490, 420, 220, 50, "-R :18000", "#22c55e"),
        ]
        tunnel_edges = [
            (0, 1, "Local → Pi"),
            (0, 2, "Pi → Local"),
        ]
        svg += FlowDiagram.create(tunnel_nodes, tunnel_edges)

        right_nodes = [
            (800, 190, 320, 80, "OpenClaw Gateway", "#ef4444"),
            (800, 300, 320, 70, "Agent (Robin)", "#ec4899"),
            (800, 400, 320, 70, "Callback → 127.0.0.1:18000", "#22c55e"),
            (800, 490, 320, 60, "SSH Server: port 2022", "#a855f7"),
        ]
        right_edges = [
            (0, 1, "LLM 분석"),
            (1, 2, "콜백"),
            (2, 3, "터널 경유"),
        ]
        svg += FlowDiagram.create(right_nodes, right_edges)

        bridge_nodes = [
            (420, 330, 50, 40, "", "#0ea5e9"),
            (730, 380, 40, 40, "", "#0ea5e9"),
            (730, 430, 40, 40, "", "#22c55e"),
            (420, 430, 50, 40, "", "#22c55e"),
        ]
        bridge_edges = [
            (0, 1, "request"),
            (2, 3, "response"),
        ]
        svg += FlowDiagram.create(bridge_nodes, bridge_edges)

        svg += (
            '    <rect x="50" y="600" width="1100" height="80" rx="12" fill="#1e293b"/>\n'
            '    <text x="600" y="630" font-family="Arial, sans-serif" font-size="14" '
            'fill="#94a3b8" text-anchor="middle">ssh -p 2022 -N -T -o ExitOnForwardFailure=yes \\</text>\n'
            '    <text x="600" y="655" font-family="Arial, sans-serif" font-size="14" '
            'fill="#38bdf8" text-anchor="middle">-L 127.0.0.1:18789:127.0.0.1:18789 -R 127.0.0.1:18000:127.0.0.1:18000 user@pi-host</text>\n'
        )

        svg += SVGComponent.footer()
        return svg

    def create_auth_flow(self) -> str:
        """인증 플로우 다이어그램 (1200x600)"""
        width, height = 1200, 600
        svg = SVGComponent.header(width, height)
        svg += SVGComponent.background(width, height, fill="#fafafa")
        svg += SVGComponent.title(
            width, "콜백 인증 플로우", y=42, font_size=26, fill="#1e293b"
        )

        entity_nodes = [
            (30, 80, 140, 50, "auto_trader", "#3b82f6"),
            (330, 80, 140, 50, "OpenClaw", "#9333ea"),
            (630, 80, 140, 50, "AuthMiddleware", "#ec4899"),
            (930, 80, 140, 50, "Callback Router", "#22c55e"),
        ]
        svg += FlowDiagram.create(entity_nodes, [])

        svg += '    <line x1="100" y1="130" x2="100" y2="550" stroke="#3b82f6" stroke-width="2" stroke-dasharray="5,5"/>\n'
        svg += '    <line x1="400" y1="130" x2="400" y2="550" stroke="#9333ea" stroke-width="2" stroke-dasharray="5,5"/>\n'
        svg += '    <line x1="700" y1="130" x2="700" y2="550" stroke="#ec4899" stroke-width="2" stroke-dasharray="5,5"/>\n'
        svg += '    <line x1="1000" y1="130" x2="1000" y2="550" stroke="#22c55e" stroke-width="2" stroke-dasharray="5,5"/>\n'

        flow_nodes = [
            (40, 170, 120, 45, "message 생성", "#3b82f6"),
            (340, 275, 120, 40, "비동기 LLM 분석", "#9333ea"),
            (640, 370, 120, 50, "PUBLIC_API_PATHS", "#ec4899"),
            (940, 465, 120, 50, "hmac.compare_digest", "#22c55e"),
            (940, 535, 120, 40, "DB 저장", "#f59e0b"),
        ]
        flow_edges = [
            (0, 1, "POST /hooks/agent"),
            (1, 2, "POST callback"),
            (2, 3, "토큰 전달"),
            (3, 4, "검증 후 저장"),
        ]
        svg += FlowDiagram.create(flow_nodes, flow_edges)

        svg += (
            '    <line x1="100" y1="240" x2="390" y2="240" stroke="#3b82f6" stroke-width="2" marker-end="url(#arrowhead)"/>\n'
            '    <text x="245" y="230" font-family="Arial, sans-serif" font-size="12" fill="#3b82f6" text-anchor="middle">POST /hooks/agent</text>\n'
            '    <text x="245" y="255" font-family="Arial, sans-serif" font-size="10" fill="#64748b" text-anchor="middle">Authorization: Bearer {OPENCLAW_TOKEN}</text>\n'
            '    <line x1="390" y1="340" x2="110" y2="340" stroke="#9333ea" stroke-width="2" stroke-dasharray="3,3" marker-end="url(#arrowhead)"/>\n'
            '    <text x="250" y="332" font-family="Arial, sans-serif" font-size="11" fill="#9333ea" text-anchor="middle">202 Accepted (즉시 응답)</text>\n'
            '    <line x1="400" y1="410" x2="690" y2="410" stroke="#9333ea" stroke-width="2" marker-end="url(#arrowhead)"/>\n'
            '    <text x="545" y="400" font-family="Arial, sans-serif" font-size="12" fill="#9333ea" text-anchor="middle">POST /api/v1/openclaw/callback</text>\n'
            '    <text x="545" y="425" font-family="Arial, sans-serif" font-size="10" fill="#64748b" text-anchor="middle">Authorization: Bearer {CALLBACK_TOKEN}</text>\n'
            '    <line x1="990" y1="595" x2="410" y2="595" stroke="#22c55e" stroke-width="2" stroke-dasharray="3,3" marker-end="url(#arrowhead)"/>\n'
            '    <text x="700" y="587" font-family="Arial, sans-serif" font-size="11" fill="#22c55e" text-anchor="middle">200 OK {"status":"ok","analysis_result_id":123}</text>\n'
        )

        svg += (
            '    <rect x="30" y="540" width="280" height="50" rx="8" fill="#ffffff" stroke="#cbd5e1" stroke-width="2"/>\n'
            '    <text x="50" y="565" font-family="Arial, sans-serif" font-size="11" fill="#1e293b" text-anchor="start">OPENCLAW_TOKEN: Gateway 인증</text>\n'
            '    <text x="50" y="582" font-family="Arial, sans-serif" font-size="11" fill="#1e293b" text-anchor="start">CALLBACK_TOKEN: 콜백 Auth Token</text>\n'
        )

        svg += SVGComponent.footer()
        return svg


if __name__ == "__main__":
    OpenClawImages("openclaw").generate()
