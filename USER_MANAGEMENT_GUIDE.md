# 사용자 권한 관리 가이드

Auto Trader의 사용자 권한 관리 시스템 사용 방법입니다.

## 권한 레벨

### 1. Viewer (기본)
- **권한**: 읽기 전용
- **가능한 작업**:
  - 분석 결과 조회
  - 종목 정보 조회
  - 대시보드 확인
- **불가능한 작업**:
  - 거래 실행
  - 사용자 관리

### 2. Trader
- **권한**: Viewer + 거래 실행
- **가능한 작업**:
  - Viewer의 모든 권한
  - 매수/매도 주문 실행
  - 자동 거래 설정
- **불가능한 작업**:
  - 사용자 관리

### 3. Admin
- **권한**: 모든 권한
- **가능한 작업**:
  - Trader의 모든 권한
  - 사용자 관리 (권한 변경, 활성화/비활성화)
  - 시스템 설정

## 권한 관리 방법

### 방법 1: 웹 관리자 페이지 (권장)

1. **관리자 계정으로 로그인**
   ```
   http://localhost:8000/web-auth/login
   ```

2. **관리자 페이지 접속**
   ```
   http://localhost:8000/admin/users
   ```
   - 상단 네비게이션의 "👥 관리자" 메뉴 클릭

3. **사용자 관리**
   - **권한 변경**: 드롭다운에서 원하는 권한 선택
   - **활성화/비활성화**: "활성화"/"비활성화" 버튼 클릭
   - 실시간으로 변경사항 반영

### 방법 2: CLI 도구

#### 로컬 개발 환경

```bash
# 모든 사용자 조회
python manage_users.py list

# Trader로 승격
python manage_users.py promote <username>

# Admin으로 승격
python manage_users.py admin <username>

# Viewer로 강등
python manage_users.py demote <username>

# 사용자 활성화
python manage_users.py activate <username>

# 사용자 비활성화
python manage_users.py deactivate <username>
```

#### 실서버 (Docker Compose 환경)

```bash
# api 컨테이너에서 실행 (권장)
docker exec -it auto_trader_api_prod python manage_users.py list
docker exec -it auto_trader_api_prod python manage_users.py admin <username>
docker exec -it auto_trader_api_prod python manage_users.py promote <username>
docker exec -it auto_trader_api_prod python manage_users.py demote <username>
docker exec -it auto_trader_api_prod python manage_users.py activate <username>
docker exec -it auto_trader_api_prod python manage_users.py deactivate <username>

# 또는 worker 컨테이너 사용
docker exec -it auto_trader_worker_prod python manage_users.py list
```

<details>
<summary>대체 방법 (참고용)</summary>

```bash
# docker-compose run 사용
docker compose -f docker-compose.prod.yml run --rm api python manage_users.py list

# docker run 직접 사용
docker run --rm --env-file .env.prod --network host \
  ghcr.io/${GITHUB_REPOSITORY}:production \
  python manage_users.py list
```
</details>

#### 예시

**로컬 개발 환경:**

```bash
# bob을 admin으로 승격
python manage_users.py admin bob

# alice를 trader로 승격
python manage_users.py promote alice

# john을 viewer로 강등
python manage_users.py demote john

# 사용자 목록 확인
python manage_users.py list
```

**실서버 (가장 간단한 방법):**

```bash
# bob을 admin으로 승격
docker exec -it auto_trader_api_prod python manage_users.py admin bob

# alice를 trader로 승격
docker exec -it auto_trader_api_prod python manage_users.py promote alice

# john을 viewer로 강등
docker exec -it auto_trader_api_prod python manage_users.py demote john

# 사용자 목록 확인
docker exec -it auto_trader_api_prod python manage_users.py list
```

### 방법 3: REST API

#### 권한 변경
```bash
curl -X PUT http://localhost:8000/admin/users/3/role \
  -H "Content-Type: application/json" \
  -H "Cookie: session=<session-token>" \
  -d '{"role": "admin"}'
```

#### 활성화/비활성화 토글
```bash
curl -X PUT http://localhost:8000/admin/users/3/toggle \
  -H "Cookie: session=<session-token>"
```

#### 모든 사용자 조회
```bash
curl http://localhost:8000/admin/users/api \
  -H "Cookie: session=<session-token>"
```

## 초기 설정

### 첫 번째 관리자 생성

1. 회원가입으로 첫 사용자 생성
2. CLI 도구로 admin 권한 부여:
   ```bash
   python manage_users.py admin <username>
   ```

### 예시

```bash
# 1. 웹에서 회원가입: robin
# 2. CLI로 admin 승격
python manage_users.py admin robin

# 3. 로그인 후 관리자 페이지 접근
```

## 권한에 따른 엔드포인트 접근

### 모든 사용자 접근 가능
- `GET /analysis-json/` - 분석 결과
- `GET /stock-latest/` - 최신 종목
- `GET /health` - 헬스체크

### Trader 이상 권한 필요
- `POST /upbit-trading/buy` - 매수 주문
- `POST /upbit-trading/sell` - 매도 주문

### Admin 전용
- `GET /admin/users` - 사용자 관리 페이지
- `PUT /admin/users/{id}/role` - 권한 변경
- `PUT /admin/users/{id}/toggle` - 활성화 토글

## 코드에서 권한 체크

### 라우트에서 권한 요구하기

```python
from fastapi import APIRouter, Depends, Request
from app.auth.web_router import require_role
from app.models.trading import UserRole, User

router = APIRouter()

# Trader 이상 권한 필요
@router.post("/trade")
async def execute_trade(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    # require_role을 사용하여 권한 체크
    from app.auth.admin_router import require_admin
    user = await require_role(UserRole.trader, request, db)
    # 거래 로직...
    return {"message": "Trade executed"}

# Admin 권한 필요
@router.get("/admin-only")
async def admin_only(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from app.auth.admin_router import require_admin
    admin = await require_admin(request, db)
    # 관리자 전용 로직...
    return {"message": "Admin area"}
```

### 권한 계층 확인

```python
role_hierarchy = {
    UserRole.viewer: 0,
    UserRole.trader: 1,
    UserRole.admin: 2
}

# 사용자 권한이 최소 요구 권한 이상인지 확인
if role_hierarchy[user.role] >= role_hierarchy[UserRole.trader]:
    # Trader 이상의 권한
    pass
```

## 보안 고려사항

### 1. 자기 자신 수정 방지
- 관리자는 자신의 권한을 변경할 수 없음
- 관리자는 자신의 계정을 비활성화할 수 없음

### 2. 세션 관리
- 세션 쿠키는 HttpOnly로 설정 (XSS 방지)
- 7일 후 자동 만료
- 로그아웃 시 즉시 무효화

### 3. 권한 검증
- 모든 관리자 API는 admin 권한 필수
- 권한 없는 접근 시 403 Forbidden 반환

## 문제 해결

### Q: 관리자 페이지에 접근할 수 없어요
**A**: 현재 사용자가 admin 권한인지 확인하세요:
```bash
python manage_users.py list
```

### Q: CLI 도구가 작동하지 않아요
**A**:
1. 가상환경이 활성화되어 있는지 확인
2. 데이터베이스 연결 설정 확인 (.env 파일)
3. 권한 확인: `chmod +x manage_users.py`

### Q: 사용자를 비활성화했는데 여전히 로그인되어 있어요
**A**: 기존 세션은 만료될 때까지 유효합니다. 즉시 차단하려면:
1. 사용자에게 로그아웃 요청
2. 또는 세션 만료 대기 (최대 7일)

### Q: 모든 관리자를 실수로 강등시켰어요
**A**: CLI 도구로 다시 admin 권한 부여:
```bash
python manage_users.py admin <username>
```

## API 레퍼런스

### GET /admin/users
사용자 관리 페이지 (HTML)
- **권한**: Admin
- **반환**: HTML 페이지

### GET /admin/users/api
모든 사용자 목록 조회 (JSON)
- **권한**: Admin
- **반환**: 사용자 목록 JSON

### PUT /admin/users/{user_id}/role
사용자 권한 변경
- **권한**: Admin
- **Body**: `{"role": "viewer|trader|admin"}`
- **반환**: 업데이트된 사용자 정보

### PUT /admin/users/{user_id}/toggle
사용자 활성화 상태 토글
- **권한**: Admin
- **반환**: 업데이트된 사용자 정보

### GET /admin/stats
관리자 통계
- **권한**: Admin
- **반환**: 사용자 통계 (총 사용자, 활성 사용자, 권한별 분포)

## 모범 사례

1. **최소 권한 원칙**: 필요한 최소한의 권한만 부여
2. **정기 검토**: 주기적으로 사용자 권한 검토
3. **Admin 최소화**: Admin 권한은 꼭 필요한 사용자에게만
4. **비활성화 사용**: 삭제보다는 비활성화 사용 (이력 보존)
5. **감사 로그**: 중요한 권한 변경은 로그 기록 고려
