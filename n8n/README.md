# n8n Workflow Automation

auto_trader의 워크플로우 오케스트레이션을 담당하는 n8n 인스턴스.

## 구조

```
n8n/
├── data/          # n8n 런타임 데이터 (.gitignore)
├── workflows/     # 워크플로우 JSON export (Git 추적)
└── README.md
```

## 역할

- **알림 파이프라인**: 체결/분석 이벤트 수신 → Discord/Telegram 라우팅
- **헬스체크**: MCP 서버, WebSocket 연결 상태 모니터링
- **리포트**: 일일/주간 포트폴리오 요약 자동 발송
- **외부 시그널**: 경제 캘린더, 뉴스 등 수집

## 워크플로우 관리

n8n UI에서 워크플로우 수정 후 export:

```bash
# 컨테이너 내부에서 export
docker exec auto_trader_n8n_prod n8n export:workflow --all --output=/data/workflows/

# 또는 호스트에서 직접 복사
cp n8n/data/.n8n/workflows/*.json n8n/workflows/
```

export된 JSON을 커밋하여 버전 관리.

## 환경변수

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `N8N_PORT` | 웹 UI 포트 | 5678 |
| `N8N_BASIC_AUTH_USER` | 기본 인증 사용자 | - |
| `N8N_BASIC_AUTH_PASSWORD` | 기본 인증 비밀번호 | - |

`.env.prod`에 추가하여 설정.
