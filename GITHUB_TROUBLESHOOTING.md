# 🔧 GitHub Actions 문제 해결 가이드

## 🚨 "Failed to get ID token" 에러 해결

### 문제 원인
`Error: Failed to get ID token: Error message: Unable to get ACTIONS_ID_TOKEN_REQUEST_URL env variable`

이 에러는 GitHub Actions의 OIDC (OpenID Connect) 토큰 관련 권한 문제입니다.

### 🔧 해결 방법

#### 1. Repository 설정 확인

**Settings → Actions → General → Workflow permissions**
- ✅ **"Read and write permissions"** 선택
- ✅ **"Allow GitHub Actions to create and approve pull requests"** 체크

#### 2. 워크플로우 권한 설정

```yaml
permissions:
  contents: read
  packages: write
  id-token: write      # OIDC 토큰 권한
  attestations: write  # Attestation 권한
```

#### 3. Repository 가시성 확인

- **Public Repository**: 모든 기능 사용 가능
- **Private Repository**: GitHub Pro/Team/Enterprise 필요 (Attestation 기능)

### 🚀 권장 해결책

#### Option 1: 권한 추가 (추천)
기존 `deploy.yml` 파일에 권한 추가:

```yaml
permissions:
  contents: read
  packages: write
  id-token: write
  attestations: write
```

#### Option 2: 간단한 버전 사용 (빠른 해결)
`deploy-simple.yml` 사용 - Attestation 단계 제거

#### Option 3: Attestation 조건부 실행
```yaml
- name: Generate artifact attestation
  if: success() && github.event_name != 'pull_request'
  uses: actions/attest-build-provenance@v1
  # ...
  continue-on-error: true
```

## 🔍 기타 일반적인 문제들

### 1. "Package does not exist" 에러

**원인**: GHCR 패키지가 처음 생성되는 경우
**해결**: 첫 번째 푸시 후 패키지 가시성 설정

1. Repository → Packages 섹션
2. 생성된 패키지 클릭
3. **Package settings** → **Change package visibility**

### 2. "Permission denied" 에러

**원인**: `GITHUB_TOKEN` 권한 부족
**해결**: Repository Settings에서 Actions 권한 확인

### 3. Docker 빌드 타임아웃

**원인**: 의존성 설치 시간 초과
**해결**: 
```yaml
timeout-minutes: 30
platforms: linux/amd64,linux/arm64
cache-from: type=gha,scope=${{ matrix.image }}
```

### 4. "Resource not accessible by integration"

**원인**: Fine-grained personal access token 사용 시
**해결**: 
1. Settings → Developer settings → Personal access tokens
2. Repository access 권한 확인
3. Contents, Metadata, Packages 권한 활성화

## 🧪 테스트 방법

### 1. 간단한 테스트 워크플로우

```yaml
name: Test Build
on:
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo "Hello World"
```

### 2. Docker 빌드만 테스트

```yaml
- name: Test Docker build
  run: |
    docker build -f Dockerfile.api -t test:latest .
    docker run --rm test:latest python --version
```

### 3. 권한 테스트

```yaml
- name: Test permissions
  run: |
    echo "Actor: ${{ github.actor }}"
    echo "Token exists: ${{ secrets.GITHUB_TOKEN != '' }}"
    echo "Registry: ${{ env.REGISTRY }}"
```

## 📋 체크리스트

- [ ] Repository가 public이거나 Pro/Team/Enterprise 계정인가?
- [ ] Actions 권한이 "Read and write"로 설정되어 있는가?
- [ ] 워크플로우에 필요한 권한이 모두 설정되어 있는가?
- [ ] `GITHUB_TOKEN`이 올바르게 작동하는가?
- [ ] 첫 번째 빌드 후 패키지 가시성이 설정되어 있는가?

## 🔄 단계별 문제 해결

### 1단계: 간단한 워크플로우로 시작
```bash
# deploy-simple.yml 사용
git add .github/workflows/deploy-simple.yml
git commit -m "Add simple deployment workflow"
git push origin production
```

### 2단계: 성공 후 기능 추가
권한 설정 확인 후 `deploy.yml`로 업그레이드

### 3단계: 문제 지속 시
1. Repository를 public으로 변경 (테스트용)
2. 새로운 Personal Access Token 생성
3. Fine-grained token 대신 Classic token 사용

## 📞 추가 도움

- [GitHub Actions 문서](https://docs.github.com/en/actions)
- [GHCR 문서](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [OIDC 토큰 문서](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/about-security-hardening-with-openid-connect)

