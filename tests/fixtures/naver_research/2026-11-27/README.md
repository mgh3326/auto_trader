# 네이버 리서치 flat 상세 응답 캡처 (2026-11-27)

2026-11-27 실제 공개 JSON 응답(`https://stock.naver.com/api/stockSecurity/researches/v2/{category}/{nid}`)을
그대로 기록했다. 상세 응답은 `researchContent` 래퍼 없이 객체 자체가 온다.

- `detail-company-98510.json`: company — `goalPrice`(문자열), `opinionText`, `opinionType`, `itemCode`/`itemName` 포함.
- `detail-industry-47246.json`: industry — `goalPrice`·`opinionText`·`opinionType` 없음, `analystName`·`categoryIndustry` 포함.
- `detail-market-38309.json`, `detail-invest-40964.json`, `detail-economy-14065.json`: plain — 목표가·의견 없음.
- `list-{category}-0.json`: 같은 날 목록 응답 중 첫 항목만 남긴 것(`hasNext`·`totalCount` 등 봉투는 그대로).

저작권상 `content` 필드는 HTML 문자 1000자까지만 남겼다(`2026-11-26/README.md`와 같은 규칙).
실제 네이버 응답을 재요청하지 않으며 오프라인 테스트에만 사용한다.
