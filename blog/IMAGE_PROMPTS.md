# 블로그 이미지 생성 프롬프트 (2026-07)

11편에서 검증된 스타일을 시리즈 전체에 유지합니다. **GPT 이미지 생성 권장** (한글 텍스트 정확도가 나노바나나보다 좋았음). 생성 후 `blog/images/`에 아래 파일명으로 저장.

**공통 스타일 (모든 프롬프트 끝에 이미 포함됨):** 16:9, 다크 네이비 그라데이션 배경, 틸(#25e2a4)·바이올렛(#6c5ce7) 글로우 액센트, 플랫 벡터.

---

## 12편 — 실주문 안전장치 (blog_12_order_safety.md)

### `order_safety_thumbnail.png` (썸네일, 한글 텍스트 포함)

> A wide 16:9 blog thumbnail illustration, dark navy blue gradient background (#0d1330 to #1a2454), modern tech-editorial style. A friendly AI robot reaches toward a large glowing "BUY" button, but between its hand and the button stand a series of translucent glass security gates, each gate glowing with a different icon: an hourglass (timeout), a fingerprint hash, a shield, a magnifying glass over a receipt. One gate is lit red, actively blocking a duplicated ghost order ticket. Flat vector illustration with soft glow accents in teal (#25e2a4) and violet (#6c5ce7). Large bold Korean headline text at the top: "AI에게 주문 버튼을 줘도 될까", smaller subtitle below: "실계좌 자동매매의 안전장치 설계". Text must be crisp, correctly spelled Korean, high contrast against the dark background.

### `order_safety_gates.png` (본문 컨셉 컷, 텍스트 없음)

> A wide 16:9 tech illustration, no text anywhere. Dark navy gradient background. An isometric conveyor line carries glowing order tickets from left to right toward a stock exchange building icon. The tickets pass through four translucent checkpoint gates in sequence, each with a distinct glowing emblem: a clock, a hash fingerprint, a shield with a checkmark, a magnifying glass over a ledger book. At the second gate, a duplicate ghost ticket is being deflected downward into a reject bin, glowing red. Flat vector style, teal and violet glow accents, clean and minimal, cinematic depth.

---

## 13편 — 토스증권 Open API (blog_13_toss_openapi.md)

### `toss_openapi_thumbnail.png` (썸네일, 한글 텍스트 포함)

> A wide 16:9 blog thumbnail illustration, dark navy blue gradient background (#0d1330 to #1a2454), modern tech-editorial style. A developer character plugs a large glowing blue API cable into a sleek trading terminal showing Korean stock candlestick charts. The floor between the developer and the terminal is a walkway with several subtle trap doors slightly open, glowing warning-orange from below, with small caution-triangle icons floating above them. Flat vector illustration with soft glow accents in teal (#25e2a4) and violet (#6c5ce7). Large bold Korean headline text at the top: "토스증권 Open API로 실주문 연동하기", smaller subtitle below: "문서에 없는 함정들". Text must be crisp, correctly spelled Korean, high contrast.

### `toss_openapi_pitfalls.png` (본문 컨셉 컷, 텍스트 없음)

> A wide 16:9 isometric tech illustration, no text anywhere. Dark navy gradient background. A glowing path runs from an open book icon (documentation) on the left to a stock exchange terminal on the right. Along the path, four visual obstacles: (1) two server towers playing tug-of-war over a single glowing key token, (2) a traffic light throttling a stream of request packets into a narrow funnel, (3) a locked gate whose keyhole matches a gear-shaped account-settings icon, (4) two arrows pointing in opposite directions colliding at a blocked intersection with a red X. A small robot character navigates the path carefully. Flat vector style, teal and violet glow accents with warning-orange highlights on the obstacles.

---

## Infra-6편 — pytest-xdist 데드락 (blog_infra_6_xdist_deadlock.md)

### `xdist_deadlock_thumbnail.png` (썸네일, 한글 텍스트 포함)

> A wide 16:9 blog thumbnail illustration, dark navy blue gradient background (#0d1330 to #1a2454), modern tech-editorial style. Four small worker robots are frozen mid-stride in a circle around a large glowing PostgreSQL-style database cylinder, each robot connected to the database by a tangled luminous thread, the threads knotted together in the center above the database. One robot holds a wrench, another holds a query document. A large clock on the wall glows red showing time passing. Flat vector illustration with soft glow accents in teal (#25e2a4) and violet (#6c5ce7). Large bold Korean headline text at the top: "CI가 이유 없이 멈추던 날", smaller subtitle below: "pytest-xdist × PostgreSQL 데드락 추적기". Text must be crisp, correctly spelled Korean, high contrast.

### `xdist_deadlock_barrier.png` (본문 컨셉 컷, 텍스트 없음)

> A wide 16:9 tech illustration, no text anywhere. Dark navy gradient background. A running track with four lanes, four small worker robots crouched at a glowing starting gate barrier that spans all lanes. In front of the barrier, one lead robot finishes assembling a database cylinder (placing the last glowing table-block into it). Above the barrier, a signal light is switching from red to green. The lanes beyond the barrier stretch toward a finish flag. Conveys: all runners wait until the database setup is complete, then run in parallel safely. Flat vector style, teal and violet glow accents.

---

## 번외 — AI 에이전트 워크플로우 회고 (blog_special_agent_workflow.md)

### `agent_workflow_thumbnail.png` (썸네일, 한글 텍스트 포함)

> A wide 16:9 blog thumbnail illustration, dark navy blue gradient background (#0d1330 to #1a2454), modern tech-editorial style. One human developer sits at a central desk with coffee, reviewing a glowing pull-request panel. Around the human, four translucent hologram robot assistants work at their own smaller desks, each desk inside its own glass lane like parallel train tracks, each robot typing on its own floating code editor. Above them all, a glowing git commit graph flows across the sky like a subway map, branches merging into one main line. Flat vector illustration with soft glow accents in teal (#25e2a4) and violet (#6c5ce7). Large bold Korean headline text at the top: "AI 에이전트와 5개월, 커밋 1,774개", smaller subtitle below: "1인 개발자의 워크플로우 회고". Text must be crisp, correctly spelled Korean, high contrast.

### `agent_workflow_parallel.png` (본문 컨셉 컷, 텍스트 없음)

> A wide 16:9 tech illustration, no text anywhere. Dark navy gradient background. A git-branch diagram rendered as glowing parallel lanes in 3D space: five branch lanes fork from a main line on the left, each lane occupied by a small robot building code blocks, then the lanes converge back toward a single main line on the right. At the convergence point, a human figure with a large magnifying glass inspects each merging lane, one lane showing a red flaw being caught and sent back. Flat vector style, teal and violet glow accents, clean and minimal.

---

## (선택) 기존 인기글 썸네일 리프레시

기존 발행글은 전부 이미지를 보유하고 있어 **필수 교체는 없습니다.** 다만 유입 상위 3편의 썸네일을 새 스타일로 통일하고 싶다면:

### `kis_api_thumbnail_v2.png` — 1편 /227 (인기 1위)

> A wide 16:9 blog thumbnail illustration, dark navy blue gradient background (#0d1330 to #1a2454), modern tech-editorial style. A friendly AI robot catches a stream of glowing candlestick chart bars flowing out of a large API portal gate labeled with a generic bank-building icon, collecting them into a glass data pipeline that feeds a glowing analysis dashboard. Flat vector illustration with soft glow accents in teal (#25e2a4) and violet (#6c5ce7). Large bold Korean headline text at the top: "한투 API로 실시간 주식 데이터 수집하기", smaller subtitle below: "AI 투자 분석의 시작". Text must be crisp, correctly spelled Korean, high contrast.

### `monitoring_thumbnail_v2.png` — 6편 /233

> A wide 16:9 blog thumbnail illustration, dark navy blue gradient background (#0d1330 to #1a2454), modern tech-editorial style. A wall of glowing observability dashboards (line charts, log streams, trace waterfalls) monitored by a small robot with binoculars, one panel flashing a soft orange alert while the robot points at it. Flat vector illustration with soft glow accents in teal (#25e2a4) and violet (#6c5ce7). Large bold Korean headline text at the top: "실전 운영을 위한 모니터링 시스템 구축", smaller subtitle below: "OpenTelemetry + Grafana 관찰성 스택". Text must be crisp, correctly spelled Korean, high contrast.

### `kis_trading_thumbnail_v2.png` — 9편 /237

> A wide 16:9 blog thumbnail illustration, dark navy blue gradient background (#0d1330 to #1a2454), modern tech-editorial style. An AI robot at a trading desk presses a glowing order button while interlocking gear wheels (task queue) behind it pass order tickets along a conveyor toward two market gates, one marked with a Korean flag motif and one with a US flag motif. Flat vector illustration with soft glow accents in teal (#25e2a4) and violet (#6c5ce7). Large bold Korean headline text at the top: "KIS 국내/해외 주식 자동 매매 시스템", smaller subtitle below: "Celery + AI 분석 기반 스마트 트레이딩". Text must be crisp, correctly spelled Korean, high contrast.

---

## 생성 후 작업

1. 생성된 PNG를 `blog/images/`에 위 파일명으로 저장
2. 글 발행은 CDP 자동화 레시피로 (티스토리 발행 워크플로우 메모리 참조)
3. 썸네일 한글이 깨지면: 텍스트 없는 버전으로 재생성 후 `blog/tools/` SVG 파이프라인으로 제목만 얹기
