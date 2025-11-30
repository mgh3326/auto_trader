# 블로그 이미지 생성 가이드

블로그 글 작성 시 SVG 이미지를 생성하고 PNG로 변환하는 방법을 설명합니다.

## 디렉토리 구조

```
blog/
├── tools/                    # 재사용 가능한 도구들
│   ├── __init__.py
│   ├── image_generator.py    # BlogImageGenerator 베이스 클래스
│   └── svg_converter.py      # SVG to PNG 변환기
├── images/                   # 생성된 이미지 저장
│   ├── *.svg                 # SVG 파일들
│   └── *.png                 # PNG 파일들
└── blog_*.md                 # 블로그 글들
```

## 사용법

### 1. 새 블로그 이미지 생성기 만들기

`blog/images/` 디렉토리에 새 Python 파일을 생성합니다:

```python
#!/usr/bin/env python3
"""
{블로그 제목} 이미지 생성
"""
from blog.tools.image_generator import BlogImageGenerator, ThumbnailTemplate


class MyBlogImages(BlogImageGenerator):
    """블로그 이미지 생성기"""

    def get_images(self):
        """생성할 이미지 목록 반환"""
        return [
            ("thumbnail", 1200, 630, self.create_thumbnail),
            ("architecture", 1400, 900, self.create_architecture),
            # 필요한 만큼 추가...
        ]

    def create_thumbnail(self) -> str:
        """썸네일 이미지 (1200x630)"""
        return ThumbnailTemplate.create(
            title_line1="제목 첫째 줄",
            title_line2="제목 둘째 줄",
            subtitle="부제목",
            icons=[
                ("🎯", "기능1", "#2196F3"),
                ("📊", "기능2", "#4CAF50"),
                ("🔧", "기능3", "#FF9800"),
            ],
            tech_stack="Python • FastAPI • Redis",
        )

    def create_architecture(self) -> str:
        """아키텍처 다이어그램"""
        width, height = 1400, 900

        svg = self.svg_header(width, height)

        # 배경
        svg += self.rect(0, 0, width, height, fill="#f8f9fa", stroke="none")

        # 제목
        svg += self.text(width // 2, 50, "시스템 아키텍처",
                        font_size=32, weight="bold", anchor="middle")

        # 박스 그리기
        svg += self.rect(100, 100, 200, 80, fill="#e3f2fd", stroke="#1976D2")
        svg += self.text(200, 145, "컴포넌트 A", anchor="middle")

        # 화살표
        svg += self.line(300, 140, 400, 140, arrow=True)

        svg += self.svg_footer()
        return svg


if __name__ == "__main__":
    MyBlogImages("my_blog").generate()
```

### 2. 이미지 생성 실행

```bash
# 프로젝트 루트에서 실행
uv run python blog/images/my_blog_images.py
```

출력 예시:
```
🎨 my_blog 블로그 이미지 생성 시작...

✅ my_blog_thumbnail.svg (1200x630)
✅ my_blog_architecture.svg (1400x900)

✨ 2개 SVG 파일 생성 완료!

🔄 PNG 변환 시작...

✓ my_blog_thumbnail.svg → my_blog_thumbnail.png (1200x630px)
✓ my_blog_architecture.svg → my_blog_architecture.png (1400x900px)

✨ 2개 PNG 파일 생성 완료!

==================================================
📁 생성된 파일:
  • my_blog_thumbnail.png (1200x630, 45.2KB)
  • my_blog_architecture.png (1400x900, 78.3KB)
```

### 3. SVG만 생성하기

PNG 변환 없이 SVG만 생성하려면:

```python
if __name__ == "__main__":
    MyBlogImages("my_blog").generate(convert_png=False)
```

### 4. 기존 SVG를 PNG로 변환하기

```bash
# 단일 파일
uv run python -m blog.tools.svg_converter my_image.svg

# 여러 파일
uv run python -m blog.tools.svg_converter *.svg

# 너비 지정
uv run python -m blog.tools.svg_converter my_image.svg -w 1400

# 출력 파일명 지정
uv run python -m blog.tools.svg_converter input.svg -o output.png
```

## BlogImageGenerator 헬퍼 메서드

### SVG 구조

```python
# SVG 시작
svg = self.svg_header(width, height, defs="추가 정의")

# 내용 작성...

# SVG 종료
svg += self.svg_footer()
```

### 도형 그리기

```python
# 사각형
self.rect(x, y, width, height,
          fill="#ffffff", stroke="#333333", stroke_width=2, rx=8)

# 원
self.circle(cx, cy, r,
            fill="#ffffff", stroke="#333333", stroke_width=2)

# 선 (화살표 옵션)
self.line(x1, y1, x2, y2,
          stroke="#666666", stroke_width=2, arrow=False)
```

### 텍스트

```python
self.text(x, y, "내용",
          font_size=14, fill="#333333", anchor="start",
          weight="normal", font_family="Arial, sans-serif")
```

anchor 옵션:
- `"start"`: 왼쪽 정렬 (기본값)
- `"middle"`: 가운데 정렬
- `"end"`: 오른쪽 정렬

### 그라데이션

```python
# defs에 그라데이션 정의 추가
gradient = self.gradient_defs("myGradient", [
    (0, "#ff0000"),    # 0%에서 빨강
    (50, "#00ff00"),   # 50%에서 초록
    (100, "#0000ff"),  # 100%에서 파랑
])

svg = self.svg_header(width, height, defs=gradient)

# 그라데이션 사용
svg += self.rect(0, 0, width, height, fill="url(#myGradient)")
```

## ThumbnailTemplate

블로그 썸네일을 빠르게 생성하는 템플릿:

```python
from blog.tools.image_generator import ThumbnailTemplate

svg = ThumbnailTemplate.create(
    title_line1="첫째 줄 제목",
    title_line2="둘째 줄 제목 (선택)",
    subtitle="부제목 (선택)",
    icons=[
        ("이모지", "라벨", "배경색"),
        ("🎯", "Target", "#2196F3"),
        ("📊", "Data", "#4CAF50"),
    ],
    tech_stack="하단 기술 스택 텍스트",
    bg_gradient=("#0d1b2a", "#1b263b", "#415a77"),  # 배경 그라데이션
    accent_color="#4CAF50",  # 강조 색상
)
```

## 이미지 권장 크기

| 용도 | 크기 | 비고 |
|------|------|------|
| 썸네일 | 1200x630 | Open Graph 표준 |
| 아키텍처 다이어그램 | 1400x900 | 가로형 |
| 플로우차트 | 1200x800 | 세로 흐름 |
| ERD | 1200x800 | 테이블 관계 |
| 대시보드 스크린샷 | 1400x900 | 넓은 화면 |
| 코드 하이라이트 | 1000x600 | 작은 크기 |

## 색상 팔레트

### 기본 색상
```
배경:     #f8f9fa (밝은 회색)
텍스트:   #333333 (진한 회색)
테두리:   #666666 (중간 회색)
```

### 강조 색상
```
파랑:     #2196F3, #1976D2, #e3f2fd
초록:     #4CAF50, #388E3C, #e8f5e9
주황:     #FF9800, #F57C00, #fff3e0
빨강:     #f44336, #d32f2f, #ffebee
보라:     #9C27B0, #7B1FA2, #f3e5f5
```

### 다크 테마 (썸네일용)
```
배경:     #0d1b2a, #1b263b, #415a77
텍스트:   #ffffff
서브텍스트: #778da9
```

## 문제 해결

### Playwright 설치 오류

```bash
# Playwright 설치
uv add playwright --dev
uv run playwright install chromium
```

### 한글 폰트 깨짐

SVG 변환기는 Google Noto Sans KR 웹폰트를 자동으로 로드합니다.
폰트 로딩에 1초 대기 시간이 포함되어 있습니다.

### SVG 파일을 찾을 수 없음

이미지 생성기는 기본적으로 `blog/images/` 디렉토리를 사용합니다.
다른 디렉토리를 사용하려면:

```python
from pathlib import Path

MyBlogImages("my_blog", images_dir=Path("/custom/path")).generate()
```
