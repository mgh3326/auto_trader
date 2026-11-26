# 국내 뉴스 픽스처

`mstock_mainnews_response.json`과 `mstock_stock_news_response.json`은 2026-11-26에 확인한 Naver m.stock JSON 응답 shape를 기록한다. 테스트는 이 두 응답을 사용하며 worker 실행 중 vendor HTTP 호출을 하지 않는다. Main news의 `dt`는 초 단위, 종목 뉴스의 `datetime`은 분 단위다.
