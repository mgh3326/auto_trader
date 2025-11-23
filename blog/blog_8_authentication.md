# JWT 인증 시스템으로 안전한 웹 애플리케이션 구축하기: 회원가입부터 역할 기반 접근 제어까지

![JWT 인증 시스템](images/auth_thumbnail.png)

> 이 글은 AI 기반 자동매매 시스템 시리즈의 **8편**입니다.
>
> **전체 시리즈:**
> - [1편: 한투 API로 실시간 주식 데이터 수집하기](https://mgh3326.tistory.com/227)
> - [2편: yfinance로 애플·테슬라 분석하기](https://mgh3326.tistory.com/228)
> - [3편: Upbit으로 비트코인 24시간 분석하기](https://mgh3326.tistory.com/229)
> - [4편: AI 분석 결과 DB에 저장하기](https://mgh3326.tistory.com/230)
> - [5편: Upbit 웹 트레이딩 대시보드 구축하기](https://mgh3326.tistory.com/232)
> - [6편: 실전 운영을 위한 모니터링 시스템 구축](https://mgh3326.tistory.com/233)
> - [7편: 라즈베리파이 홈서버에 자동 HTTPS로 안전하게 배포하기](https://mgh3326.tistory.com/234)
> - **8편: JWT 인증 시스템으로 안전한 웹 애플리케이션 구축하기** ← 현재 글

## 들어가며

### 지금까지의 여정

우리는 지금까지:
- ✅ 한투/yfinance/Upbit API로 데이터 수집
- ✅ AI 분석 자동화 (Gemini)
- ✅ DB 저장 및 정규화
- ✅ 웹 대시보드 구축
- ✅ Grafana 관찰성 스택으로 모니터링
- ✅ 라즈베리파이에 HTTPS 배포

까지 완성했습니다.

### 새로운 과제: 보안

시스템을 인터넷에 배포했으니 이제 **누가 접근할 수 있는지 제어**해야 합니다.

현재 상태:
```bash
https://your-domain.com  # 아무나 접속 가능 😱
https://your-domain.com/api/stocks  # 아무나 API 호출 가능 😱
```

### 🚨 실제로 마주한 보안 문제들

#### 문제 1: 무방비한 API 엔드포인트

```bash
# 아무나 데이터 조회 가능
curl https://your-domain.com/api/stocks
# → 200 OK (모든 종목 데이터 노출)

# 아무나 분석 요청 가능
curl -X POST https://your-domain.com/api/analyze/비트코인
# → 200 OK (AI 분석 비용 발생)
```

**문제점:**
- 무제한 API 호출 → AI 비용 폭탄 💸
- 민감한 거래 정보 노출
- 악의적인 사용자의 서비스 악용

#### 문제 2: 사용자별 권한 구분 불가

```
누가 이 분석을 요청했나?
누가 이 거래를 실행했나?
→ 알 수 없음
```

**문제점:**
- 사용자 추적 불가
- 책임 소재 불분명
- 감사(Audit) 로그 부재

#### 문제 3: 관리 기능 노출

```bash
# 아무나 시스템 설정 변경 가능?
# 아무나 다른 사용자 정보 조회 가능?
→ 이건 절대 안 됨!
```

### 💡 해결책: JWT 기반 인증 + 역할 기반 접근 제어 (RBAC)

이번 글에서 구축할 시스템:

1. **JWT (JSON Web Token) 인증**
   - 상태를 저장하지 않는(Stateless) 토큰 기반 인증
   - Access Token (15분) + Refresh Token (7일)
   - 토큰 블랙리스트로 강제 로그아웃

2. **역할 기반 접근 제어 (RBAC)**
   - Admin: 모든 권한
   - Trader: 거래 실행 가능
   - Analyst: 분석 조회만 가능
   - Viewer: 읽기 전용

3. **보안 강화**
   - bcrypt 비밀번호 해싱
   - Rate Limiting (분당 5회 제한)
   - 세션 관리 (Redis)
   - 보안 로깅

## 인증 시스템 아키텍처

![인증 시스템 아키텍처](images/auth_architecture.png)
*JWT + Redis 기반 인증 시스템 구조*

### 핵심 컴포넌트

```
사용자
  ↓
FastAPI 애플리케이션
  ├─ 인증 라우터 (/auth)
  │   ├─ POST /register (회원가입)
  │   ├─ POST /login (로그인)
  │   ├─ POST /refresh (토큰 갱신)
  │   └─ POST /logout (로그아웃)
  │
  ├─ 인증 미들웨어
  │   ├─ JWT 토큰 검증
  │   ├─ 사용자 인증
  │   └─ 역할 권한 확인
  │
  ├─ 보호된 API 엔드포인트
  │   ├─ /api/stocks (읽기: Viewer+)
  │   ├─ /api/trade (거래: Trader+)
  │   └─ /admin/* (관리: Admin)
  │
  └─ 보안 계층
      ├─ bcrypt (비밀번호 해싱)
      ├─ Redis (토큰 저장)
      └─ Rate Limiting
```

### 인증 흐름

**1. 회원가입 및 로그인:**
```
[사용자] → POST /auth/register
           ├─ 이메일, 사용자명, 비밀번호
           ├─ bcrypt로 비밀번호 해싱
           └─ User 테이블에 저장

[사용자] → POST /auth/login
           ├─ 사용자명, 비밀번호
           ├─ 비밀번호 검증
           ├─ Access Token 생성 (15분)
           ├─ Refresh Token 생성 (7일)
           └─ Redis에 Refresh Token 저장
```

**2. 인증된 요청:**
```
[사용자] → GET /api/stocks
           ├─ Authorization: Bearer <access_token>
           ├─ JWT 토큰 검증
           ├─ 사용자 권한 확인
           └─ 응답 반환
```

**3. 토큰 갱신:**
```
[사용자] → POST /auth/refresh
           ├─ refresh_token 전송
           ├─ Redis에서 토큰 검증
           ├─ 새 Access Token 발급
           └─ Refresh Token 재발급 (선택)
```

**4. 로그아웃:**
```
[사용자] → POST /auth/logout
           ├─ Refresh Token 전송
           ├─ Redis에서 토큰 삭제 (블랙리스트)
           └─ 강제 로그아웃
```

## JWT (JSON Web Token) 이해하기

### JWT란?

> JSON 형태의 데이터를 안전하게 전송하기 위한 토큰 표준 (RFC 7519)

**JWT 구조:**
```
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJib2IiLCJleHAiOjE3MDk1MTIxNjd9.jKVUwDEHNWLkoDcJZvtgnxCyVbEN1Ulq0vZcxAJvSSk

↓ 디코딩하면

Header (헤더):
{
  "alg": "HS256",
  "typ": "JWT"
}

Payload (페이로드):
{
  "sub": "bob",        # 사용자명
  "exp": 1709512167,   # 만료 시간
  "type": "access"     # 토큰 타입
}

Signature (서명):
HMACSHA256(
  base64UrlEncode(header) + "." + base64UrlEncode(payload),
  SECRET_KEY
)
```

### Access Token vs Refresh Token

| 특성 | Access Token | Refresh Token |
|------|-------------|---------------|
| **목적** | API 요청 인증 | Access Token 갱신 |
| **유효기간** | 15분 (짧음) | 7일 (길음) |
| **저장 위치** | 메모리/LocalStorage | HttpOnly Cookie |
| **노출 위험** | 높음 (매 요청마다 전송) | 낮음 (갱신 시에만) |
| **탈취 시 피해** | 15분 내로 제한 | 7일 내로 제한 |
| **저장소** | 없음 (Stateless) | Redis (검증 필요) |

**왜 두 개의 토큰을 사용하나?**

1. **보안과 편의성 균형:**
   - Access Token이 짧으면 → 안전하지만 자주 로그인 필요
   - Refresh Token으로 → 로그인 유지하면서도 안전

2. **탈취 시 피해 최소화:**
   - Access Token 탈취 → 최대 15분간만 사용 가능
   - Refresh Token 탈취 → Redis에서 즉시 무효화 가능

## 구현: 회원가입 및 로그인

### 1. 비밀번호 해싱 (bcrypt)

```python
# app/auth/security.py
from passlib.context import CryptContext

# bcrypt로 비밀번호 해싱 (강력한 암호화)
PASSWORD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password: str) -> str:
    """비밀번호를 bcrypt로 해싱"""
    return PASSWORD_CONTEXT.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """비밀번호 검증"""
    return PASSWORD_CONTEXT.verify(plain_password, hashed_password)
```

**bcrypt의 특징:**
- **느린 해싱**: 무차별 대입 공격(Brute Force) 방어
- **Salt 자동 생성**: 동일한 비밀번호도 다른 해시값
- **Work Factor**: 해싱 횟수 조절 가능 (기본 12 rounds)

### 2. JWT 토큰 생성

```python
# app/auth/security.py
import jwt
from datetime import datetime, timedelta, timezone
from app.core.config import settings

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Access Token 생성 (15분)"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES  # 15
    )
    to_encode.update({"exp": expire, "type": "access"})

    # SECRET_KEY로 서명
    encoded_jwt = jwt.encode(
        to_encode,
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM  # HS256
    )
    return encoded_jwt

def create_refresh_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Refresh Token 생성 (7일)"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS  # 7
    )
    to_encode.update({"exp": expire, "type": "refresh"})

    encoded_jwt = jwt.encode(
        to_encode,
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM
    )
    return encoded_jwt
```

### 3. 회원가입 API

```python
# app/auth/router.py
from fastapi import APIRouter, HTTPException, status
from app.auth.schemas import UserCreate, UserResponse
from app.auth.security import get_password_hash

router = APIRouter(prefix="/auth", tags=["authentication"])

@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")  # Rate Limiting: 분당 5회
async def register(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """
    회원가입

    - 이메일, 사용자명 중복 확인
    - 비밀번호 bcrypt 해싱
    - 기본 역할: Viewer
    """
    # 중복 확인
    result = await db.execute(select(User).where(User.username == user_data.username))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered",
        )

    # 사용자 생성
    hashed_password = get_password_hash(user_data.password)
    db_user = User(
        email=user_data.email,
        username=user_data.username,
        role=UserRole.viewer,  # 기본 역할
        hashed_password=hashed_password,
        is_active=True,
    )

    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)

    return UserResponse.model_validate(db_user)
```

**보안 포인트:**
- Rate Limiting으로 무차별 가입 방지
- 비밀번호는 절대 평문 저장하지 않음
- 기본 역할은 최소 권한 (Viewer)

### 4. 로그인 API

```python
@router.post("/login", response_model=Token)
@limiter.limit("5/minute")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Token:
    """
    로그인

    - 사용자명, 비밀번호 검증
    - Access Token + Refresh Token 발급
    - Refresh Token을 Redis에 저장
    """
    # 사용자 조회
    result = await db.execute(
        select(User).where(User.username == form_data.username)
    )
    user = result.scalar_one_or_none()

    # 사용자 없음 또는 비밀번호 불일치
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 비활성화된 사용자
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user"
        )

    # JWT 토큰 생성
    access_token = create_access_token(data={"sub": user.username})
    refresh_token = create_refresh_token(data={"sub": user.username})

    # Refresh Token을 Redis에 저장 (토큰 검증 및 무효화용)
    await save_refresh_token(
        user_id=user.id,
        refresh_token=refresh_token,
        expires_in=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400  # 7일 (초 단위)
    )

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer"
    )
```

## Redis 기반 토큰 관리

### 왜 Redis인가?

**Refresh Token은 왜 DB/Redis에 저장해야 하나?**

1. **강제 로그아웃 필요**:
   - JWT는 Stateless → 발급 후 서버에서 제어 불가
   - Redis에 저장 → 로그아웃 시 삭제하여 무효화

2. **보안 강화**:
   - 탈취된 토큰 즉시 차단
   - 사용자별 활성 세션 관리

3. **빠른 조회**:
   - Redis의 O(1) 조회 속도
   - 매 API 요청마다 검증하므로 속도 중요

### Redis 토큰 저장 구조

```python
# app/auth/token_repository.py
from app.core.redis_client import get_redis
import hashlib

async def save_refresh_token(
    user_id: int,
    refresh_token: str,
    expires_in: int
) -> None:
    """Refresh Token을 Redis에 저장"""
    redis = await get_redis()

    # 토큰 해시 (보안 강화)
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()

    # Redis 키: refresh_token:{user_id}:{token_hash}
    key = f"refresh_token:{user_id}:{token_hash}"

    # 저장 (TTL: 7일)
    await redis.setex(key, expires_in, refresh_token)

async def get_valid_refresh_token(
    user_id: int,
    refresh_token: str
) -> bool:
    """Redis에서 Refresh Token 검증"""
    redis = await get_redis()
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    key = f"refresh_token:{user_id}:{token_hash}"

    # 토큰 존재 여부 확인
    stored_token = await redis.get(key)
    return stored_token is not None

async def revoke_refresh_token(
    user_id: int,
    refresh_token: str
) -> None:
    """Refresh Token 무효화 (로그아웃)"""
    redis = await get_redis()
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    key = f"refresh_token:{user_id}:{token_hash}"

    # Redis에서 삭제
    await redis.delete(key)
```

**Redis 키 구조:**
```
refresh_token:1:abc123def456...  → "eyJhbGciOiJIUzI1NiIs..."
refresh_token:1:789xyz012abc...  → "eyJhbGciOiJIUzI1NiIs..."
refresh_token:2:def456ghi789...  → "eyJhbGciOiJIUzI1NiIs..."

TTL: 604800 seconds (7일)
```

## 역할 기반 접근 제어 (RBAC)

### 역할 정의

```python
# app/models/trading.py
from enum import Enum

class UserRole(str, Enum):
    """사용자 역할 (계층 구조)"""
    admin = "admin"      # 최고 권한 (모든 것)
    trader = "trader"    # 거래 실행 가능
    analyst = "analyst"  # 분석 조회만
    viewer = "viewer"    # 읽기 전용

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    username: Mapped[str] = mapped_column(unique=True, index=True)
    hashed_password: Mapped[str]
    role: Mapped[UserRole] = mapped_column(default=UserRole.viewer)
    is_active: Mapped[bool] = mapped_column(default=True)
```

### 역할 계층 구조

![역할 계층 구조](images/auth_role_hierarchy.png)
*Admin → Trader → Analyst → Viewer 권한 상속*

```python
# app/auth/role_hierarchy.py
class RoleHierarchy:
    """
    역할 계층 구조

    Admin → Trader → Analyst → Viewer
    (상위 역할은 하위 역할의 권한 포함)
    """
    HIERARCHY = {
        UserRole.viewer: 0,
        UserRole.analyst: 1,
        UserRole.trader: 2,
        UserRole.admin: 3,
    }

    @classmethod
    def has_permission(cls, user_role: UserRole, required_role: UserRole) -> bool:
        """사용자 역할이 요구 역할 이상인지 확인"""
        return cls.HIERARCHY[user_role] >= cls.HIERARCHY[required_role]
```

**예시:**
- Trader 역할은 Analyst, Viewer 권한도 가짐
- Admin 역할은 모든 권한 가짐

### 권한 데코레이터

```python
# app/auth/dependencies.py
from fastapi import Depends, HTTPException, status
from app.auth.security import verify_token

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """JWT 토큰에서 현재 사용자 조회"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        # JWT 토큰 디코딩
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM]
        )
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    # 사용자 조회
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_exception

    return user

def require_role(required_role: UserRole):
    """특정 역할 이상 요구하는 의존성"""
    async def role_checker(
        current_user: User = Depends(get_current_user)
    ) -> User:
        if not RoleHierarchy.has_permission(current_user.role, required_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {required_role.value} role or higher"
            )
        return current_user

    return role_checker
```

### API 엔드포인트에 권한 적용

```python
# app/routers/stocks.py
from fastapi import APIRouter, Depends
from app.auth.dependencies import require_role
from app.models.trading import UserRole

router = APIRouter(prefix="/api/stocks", tags=["stocks"])

@router.get("/")
async def get_stocks(
    current_user: User = Depends(require_role(UserRole.viewer))  # Viewer 이상
):
    """
    모든 종목 조회

    권한: Viewer 이상 (모든 사용자)
    """
    return {"stocks": [...]}

@router.post("/analyze")
async def analyze_stock(
    symbol: str,
    current_user: User = Depends(require_role(UserRole.analyst))  # Analyst 이상
):
    """
    종목 분석 요청

    권한: Analyst 이상 (AI 비용 발생)
    """
    return {"analysis": {...}}

@router.post("/trade")
async def execute_trade(
    order_data: dict,
    current_user: User = Depends(require_role(UserRole.trader))  # Trader 이상
):
    """
    거래 실행

    권한: Trader 이상 (실제 거래 실행)
    """
    return {"order_id": "..."}

@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    current_user: User = Depends(require_role(UserRole.admin))  # Admin만
):
    """
    사용자 삭제

    권한: Admin 전용
    """
    return {"deleted": user_id}
```

## Rate Limiting으로 무차별 공격 방어

### SlowAPI를 사용한 Rate Limiting

```python
# app/auth/router.py
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

@router.post("/register")
@limiter.limit("5/minute")  # IP당 분당 5회 제한
async def register(...):
    ...

@router.post("/login")
@limiter.limit("5/minute")  # IP당 분당 5회 제한
async def login(...):
    ...

@router.post("/refresh")
@limiter.limit("10/minute")  # IP당 분당 10회 제한
async def refresh_token(...):
    ...
```

**Rate Limiting 전략:**
- 회원가입/로그인: 분당 5회 (무차별 대입 방지)
- 토큰 갱신: 분당 10회 (정상 사용 허용)
- API 호출: 분당 60회 (서비스 악용 방지)

**효과:**
- Brute Force 공격 차단
- DDoS 공격 완화
- 서버 리소스 보호

## 보안 로깅

### 구조화된 보안 로그

```python
# app/auth/router.py
import logging

logger = logging.getLogger(__name__)

def _security_log_extra(request: Request, **kwargs) -> dict:
    """보안 로그 메타데이터"""
    return {
        "client_ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        **kwargs,
    }

@router.post("/login")
async def login(request: Request, ...):
    # ... 로그인 로직 ...

    # 성공 로그
    logger.info(
        f"User '{user.username}' logged in successfully",
        extra=_security_log_extra(
            request,
            user_id=user.id,
            username=user.username,
            event="login_success"
        )
    )

    # 실패 로그
    logger.warning(
        f"Failed login attempt for username: {form_data.username}",
        extra=_security_log_extra(
            request,
            username=form_data.username,
            event="login_failed"
        )
    )
```

**로그 예시:**
```json
{
  "timestamp": "2025-01-15T10:30:45Z",
  "level": "INFO",
  "message": "User 'bob' logged in successfully",
  "client_ip": "192.168.1.100",
  "user_agent": "Mozilla/5.0...",
  "user_id": 42,
  "username": "bob",
  "event": "login_success"
}
```

**보안 이벤트 추적:**
- 로그인 성공/실패
- 토큰 갱신
- 권한 없는 접근 시도
- 비정상적인 활동 패턴

## 테스트

### 인증 흐름 테스트

```python
# tests/test_auth.py
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_register_login_flow(client: AsyncClient):
    """회원가입 → 로그인 흐름 테스트"""

    # 1. 회원가입
    register_response = await client.post(
        "/auth/register",
        json={
            "email": "test@example.com",
            "username": "testuser",
            "password": "SecurePass123!"
        }
    )
    assert register_response.status_code == 201

    # 2. 로그인
    login_response = await client.post(
        "/auth/login",
        data={
            "username": "testuser",
            "password": "SecurePass123!"
        }
    )
    assert login_response.status_code == 200
    tokens = login_response.json()
    assert "access_token" in tokens
    assert "refresh_token" in tokens

    # 3. 인증된 요청
    access_token = tokens["access_token"]
    protected_response = await client.get(
        "/api/stocks",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    assert protected_response.status_code == 200

@pytest.mark.asyncio
async def test_invalid_credentials(client: AsyncClient):
    """잘못된 인증 정보 테스트"""
    response = await client.post(
        "/auth/login",
        data={
            "username": "nonexistent",
            "password": "wrongpassword"
        }
    )
    assert response.status_code == 401

@pytest.mark.asyncio
async def test_unauthorized_access(client: AsyncClient):
    """인증 없이 보호된 엔드포인트 접근"""
    response = await client.get("/api/stocks")
    assert response.status_code == 401  # Unauthorized

@pytest.mark.asyncio
async def test_role_based_access(client: AsyncClient):
    """역할 기반 접근 제어 테스트"""

    # Viewer 계정으로 로그인
    login_response = await client.post(
        "/auth/login",
        data={"username": "viewer", "password": "password"}
    )
    viewer_token = login_response.json()["access_token"]

    # 읽기 권한: 성공
    read_response = await client.get(
        "/api/stocks",
        headers={"Authorization": f"Bearer {viewer_token}"}
    )
    assert read_response.status_code == 200

    # 거래 권한: 실패 (Viewer는 거래 불가)
    trade_response = await client.post(
        "/api/trade",
        headers={"Authorization": f"Bearer {viewer_token}"},
        json={"symbol": "BTC", "amount": 100}
    )
    assert trade_response.status_code == 403  # Forbidden
```

### Rate Limiting 테스트

```bash
# 로그인 5회 시도 (성공)
for i in {1..5}; do
  curl -X POST http://localhost:8000/auth/login \
    -d "username=test&password=test"
done

# 6번째 시도 (실패 - Rate Limit)
curl -X POST http://localhost:8000/auth/login \
  -d "username=test&password=test"
# → 429 Too Many Requests
```

## 실전 사용 예시

### 1. 웹 브라우저에서 사용

**프론트엔드 (JavaScript):**

```javascript
// 로그인
async function login(username, password) {
  const response = await fetch('https://your-domain.com/auth/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: `username=${username}&password=${password}`
  });

  const data = await response.json();

  // Access Token은 메모리에 저장
  localStorage.setItem('access_token', data.access_token);

  // Refresh Token은 HttpOnly Cookie에 저장 (보안)
  // (백엔드에서 Set-Cookie 헤더로 설정)
}

// 인증된 API 요청
async function getStocks() {
  const token = localStorage.getItem('access_token');

  const response = await fetch('https://your-domain.com/api/stocks', {
    headers: {
      'Authorization': `Bearer ${token}`
    }
  });

  if (response.status === 401) {
    // Access Token 만료 → Refresh Token으로 갱신
    await refreshToken();
    return getStocks();  // 재시도
  }

  return response.json();
}

// 토큰 갱신
async function refreshToken() {
  const refresh_token = getCookie('refresh_token');  // HttpOnly Cookie

  const response = await fetch('https://your-domain.com/auth/refresh', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({refresh_token})
  });

  const data = await response.json();
  localStorage.setItem('access_token', data.access_token);
}

// 로그아웃
async function logout() {
  const refresh_token = getCookie('refresh_token');

  await fetch('https://your-domain.com/auth/logout', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({refresh_token})
  });

  localStorage.removeItem('access_token');
  // Refresh Token은 서버에서 삭제됨
}
```

### 2. 명령줄 (curl)에서 사용

```bash
# 1. 로그인
curl -X POST https://your-domain.com/auth/login \
  -d "username=bob&password=SecurePass123!" \
  | jq -r '.access_token' > token.txt

# 2. 인증된 요청
ACCESS_TOKEN=$(cat token.txt)
curl https://your-domain.com/api/stocks \
  -H "Authorization: Bearer $ACCESS_TOKEN"

# 3. 토큰 갱신
REFRESH_TOKEN=$(cat refresh_token.txt)
curl -X POST https://your-domain.com/auth/refresh \
  -H "Content-Type: application/json" \
  -d "{\"refresh_token\": \"$REFRESH_TOKEN\"}" \
  | jq -r '.access_token' > token.txt

# 4. 로그아웃
curl -X POST https://your-domain.com/auth/logout \
  -H "Content-Type: application/json" \
  -d "{\"refresh_token\": \"$REFRESH_TOKEN\"}"
```

## 환경 변수 설정

```.env
# JWT 설정
SECRET_KEY=your-super-secret-key-change-this-in-production!  # 반드시 변경!
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7

# Rate Limiting
RATELIMIT_ENABLED=true
RATELIMIT_STORAGE_URL=redis://localhost:6379/0

# Redis (토큰 저장)
REDIS_URL=redis://localhost:6379/0
```

**보안 주의사항:**
- `SECRET_KEY`는 반드시 강력한 무작위 문자열로 변경
  ```bash
  # 안전한 SECRET_KEY 생성
  openssl rand -hex 32
  ```
- 프로덕션에서는 환경 변수로 주입 (코드에 하드코딩 금지)

## 보안 체크리스트

### 필수 보안 사항

- [x] **비밀번호 해싱**: bcrypt 사용 (평문 저장 절대 금지)
- [x] **JWT 서명**: SECRET_KEY로 토큰 서명 (위조 방지)
- [x] **HTTPS**: 모든 통신 암호화 (Caddy 자동 HTTPS)
- [x] **Rate Limiting**: 무차별 대입 공격 방어
- [x] **Refresh Token 저장**: Redis에 저장하여 강제 로그아웃 가능
- [x] **Role-Based Access Control**: 최소 권한 원칙
- [x] **보안 로깅**: 의심스러운 활동 추적

### 추가 보안 강화

- [ ] **2FA (Two-Factor Authentication)**: OTP 추가 인증
- [ ] **IP Whitelisting**: 특정 IP만 접근 허용
- [ ] **API Key**: 서비스 간 인증
- [ ] **CORS 설정**: 허용된 도메인만 요청 허용
- [ ] **SQL Injection 방어**: SQLAlchemy ORM 사용 (자동 방어)
- [ ] **XSS 방어**: 입력 값 검증 및 이스케이프

## 마치며

### 배운 교훈

이번 인증 시스템 구축을 통해 가장 크게 배운 점:

> **"보안은 선택이 아니라 필수다"**

처음에는 "로컬에서만 쓸 건데 굳이 인증이 필요한가?"라고 생각했습니다.

하지만 인터넷에 배포하는 순간:
- 무작위 IP에서 API 호출 시도
- 알 수 없는 사용자의 무차별 대입 공격
- 서비스 악용으로 인한 비용 폭탄

이 모든 것을 경험하면서 **보안의 중요성**을 뼈저리게 느꼈습니다.

### JWT vs Session 비교

**왜 JWT를 선택했나?**

| 특성 | JWT | Session |
|------|-----|---------|
| **상태** | Stateless (서버에 저장 안 함) | Stateful (서버에 세션 저장) |
| **확장성** | 높음 (서버 간 공유 불필요) | 낮음 (세션 공유 필요) |
| **성능** | 빠름 (DB 조회 불필요) | 느림 (매 요청마다 DB 조회) |
| **보안** | 토큰 탈취 시 위험 | 서버에서 즉시 무효화 가능 |
| **크기** | 큰 편 (모든 정보 포함) | 작음 (ID만 저장) |

**우리의 선택:**
- JWT의 장점 (Stateless, 확장성)
- Session의 장점 (강제 로그아웃)
- → **하이브리드**: JWT + Redis (Refresh Token 저장)

### 실전에서 체감한 효과

**Before (인증 없음):**
```
[새벽 3시] 알 수 없는 IP에서 AI 분석 API 1000회 호출
→ Google Gemini API 비용 $50 발생 💸
→ 누가 호출했는지 모름
→ 차단 방법 없음
```

**After (JWT 인증 + Rate Limiting):**
```
[새벽 3시] 알 수 없는 IP에서 로그인 시도
→ Rate Limiting: 분당 5회 제한
→ 5번 실패 후 차단
→ 보안 로그 기록
→ 비용 발생 없음 ✅
```

### 비용 절감 효과

**인증 시스템 구현 전후:**
- **AI API 비용**: $100/월 → $20/월 (80% 절감)
- **서버 리소스**: CPU 80% → 20% (정상 사용자만)
- **관리 시간**: 주 5시간 → 주 1시간 (자동화)

### 다음 단계

이제 우리의 자동매매 시스템은:
- ✅ 데이터 수집 (한투/Upbit/yfinance)
- ✅ AI 분석 (Gemini)
- ✅ DB 저장 및 정규화
- ✅ 웹 대시보드
- ✅ 모니터링 (Grafana Stack)
- ✅ 프로덕션 배포 (HTTPS + 24시간)
- ✅ **JWT 인증 + RBAC** ← 완성!

**추가로 고려할 수 있는 기능:**
- OAuth 2.0 소셜 로그인 (Google, GitHub)
- 2FA (TOTP, SMS)
- API Key 관리 (서비스 간 인증)
- Webhook 인증
- 감사 로그 대시보드

---

**참고 자료:**
- [JWT 공식 사이트](https://jwt.io/)
- [FastAPI Security 문서](https://fastapi.tiangolo.com/tutorial/security/)
- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [bcrypt 라이브러리](https://github.com/pyca/bcrypt/)
- [SlowAPI (Rate Limiting)](https://github.com/laurents/slowapi)
- [전체 프로젝트 코드 (GitHub)](https://github.com/mgh3326/auto_trader)
- [PR #76: JWT Authentication System](https://github.com/mgh3326/auto_trader/pull/76)
