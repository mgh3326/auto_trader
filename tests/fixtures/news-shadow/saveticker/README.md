`mgh3326/finance-ref-analysis` commit `619a3dd2b5d717932b79ef8f4fa6e0a7ef1d7a35`
기준 2026-11-26 Save+ 공개 앱 백엔드 캡처다.

- `real-capture-search.json`: 참여 수치 전체 객체와 ticker symbol이 포함된 raw `/api/search` 응답.
- `real-capture-top.json`: 같은 날 `/api/news/top-stories` 추출 결과. 당시 extractor의 축약 projection이며
  ticker에는 현재 계약상 매핑하지 않는 `.KR` 심볼이 포함돼 있다.

둘 다 합성 기대값이 아닌 실제 캡처다. 현행 정규화는 미국 ticker만 DB 심볼로 매핑하고
한국 기사는 `symbol=null`로 미매칭 처리한다.
