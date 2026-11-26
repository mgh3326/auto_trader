# 네이버 리서치 API 캡처

실제 공개 JSON API 캡처: `mgh3326/finance-ref-analysis` commit `e5ac030`, `extracted/naver-research/2026-11-26/` (2026-11-26).

- `market-0.json`, `company-0.json`, `industry-0.json`, `invest-0.json`, `economy-0.json`: 원본 수집 도구가 만든 카테고리별 metadata/요약 projection.
- `detail-company-98500.json`, `by-items.json`: 캡처 응답의 구조와 ID는 유지했다. 저작권상 `content` 필드는 HTML 문자 1000자까지만 남겼다.
- 실제 네이버 응답을 재요청하지 않고 오프라인 테스트에만 사용한다.
