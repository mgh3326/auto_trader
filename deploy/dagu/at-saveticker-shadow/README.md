# at-saveticker-shadow

Save+ 전용 그림자 수집 Dagu 복사본이다. 이 디렉터리는 등록되지 않으며 저장소는 Dagu에 복사하거나 등록하지 않는다.

- `at-saveticker-shadow.yaml` — KST 매일 07:00–23:59, 3분 간격으로 Save+ 공개 뉴스와 관심도를 외부 JSONL에 기록한다.
- `--skip-benzinga --skip-finnhub --skip-kis`로 기존 뉴스 소스의 호출 주기를 변경하지 않는다. Save+ API 실패는 오류 행과 실패 종료 코드로 남는다.
- 출력은 `/root/at-run/saveticker-shadow` 호스트 마운트다. 디렉터리 생성과 실행 권한은 운영자 몫이다.
- Save+ API는 비공식이며 변경될 수 있다. `www.saveticker.com`, 인증 경로, SSE는 사용하지 않는다.
- 등록은 설계 세션/운영자만 한다.
