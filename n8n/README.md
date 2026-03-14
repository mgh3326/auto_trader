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

## 인증

n8n 2.x는 owner 기반 User Management를 사용합니다.
첫 접속 시 owner 계정 생성 화면이 표시됩니다.

- **내부 전용**: `N8N_LISTEN_ADDRESS=127.0.0.1`로 localhost만 바인딩
- 외부 접근: SSH 터널 사용 (`ssh -L 5678:localhost:5678 user@host`)

## 워크플로우 관리

n8n UI에서 워크플로우 수정 후 export:

```bash
# 전체 워크플로우를 개별 JSON으로 export
docker exec auto_trader_n8n_prod n8n export:workflow \
  --all --separate --output=/home/node/.n8n/workflows/

# 호스트의 n8n/data/workflows/ → n8n/workflows/로 복사
cp n8n/data/workflows/*.json n8n/workflows/

# Git 커밋
git add n8n/workflows/ && git commit -m "chore: export n8n workflows"
```

import (복원 시):
```bash
docker exec auto_trader_n8n_prod n8n import:workflow \
  --input=/home/node/.n8n/workflows/
```

## 환경변수 (.env.prod)

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `N8N_PORT` | 웹 UI 포트 | 5678 |

## 배포

```bash
# .env.prod 와 함께 실행
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d n8n
```
