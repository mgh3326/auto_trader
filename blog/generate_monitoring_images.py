#!/usr/bin/env python3
"""
모니터링 시스템 블로그 이미지 생성 스크립트

이 스크립트는 blog_6_monitoring.md에 필요한 이미지들을 생성합니다.
PIL (Pillow) 라이브러리를 사용하여 프로그래밍 방식으로 이미지를 생성합니다.

사용법:
    python blog/generate_monitoring_images.py

생성되는 이미지:
    - blog/images/monitoring_thumbnail.png (1200x630)
    - blog/images/before_after_monitoring.png (1200x800)
    - blog/images/monitoring_architecture.png (1400x1000)
"""

from PIL import Image, ImageDraw, ImageFont
import os


def create_thumbnail():
    """썸네일 이미지 생성 (1200x630)"""
    width, height = 1200, 630
    img = Image.new('RGB', (width, height), color='#1a1a2e')
    draw = ImageDraw.Draw(img)

    # 제목
    try:
        font_title = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 60)
        font_subtitle = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 35)
        font_small = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 25)
    except:
        font_title = ImageFont.load_default()
        font_subtitle = ImageFont.load_default()
        font_small = ImageFont.load_default()

    # 배경 그라디언트 효과 (간단한 사각형들로)
    colors = ['#0f3460', '#16213e', '#1a1a2e']
    for i, color in enumerate(colors):
        y_start = i * (height // 3)
        draw.rectangle([0, y_start, width, y_start + (height // 3)], fill=color)

    # 제목
    title = "실전 운영을 위한"
    draw.text((width // 2, 150), title, fill='#ffffff', font=font_title, anchor='mm')

    title2 = "모니터링 시스템 구축"
    draw.text((width // 2, 240), title2, fill='#ffffff', font=font_title, anchor='mm')

    # 부제목
    subtitle = "OpenTelemetry + Grafana 관찰성 스택"
    draw.text((width // 2, 340), subtitle, fill='#00d4ff', font=font_subtitle, anchor='mm')

    # 하단 텍스트
    bottom_text = "Grafana • Tempo • Loki • Prometheus"
    draw.text((width // 2, 480), bottom_text, fill='#a8dadc', font=font_small, anchor='mm')

    # 아이콘 영역 (간단한 사각형들로 표현)
    icon_y = 540
    icon_spacing = 200
    icon_colors = ['#F46800', '#E91E63', '#00ACC1', '#E6522C']  # Grafana, Tempo, Loki, Prometheus
    icon_labels = ['Grafana', 'Tempo', 'Loki', 'Prometheus']

    start_x = (width - (len(icon_colors) - 1) * icon_spacing) // 2
    for i, (color, label) in enumerate(zip(icon_colors, icon_labels)):
        x = start_x + i * icon_spacing
        # 원 그리기
        draw.ellipse([x - 20, icon_y - 20, x + 20, icon_y + 20], fill=color)

    return img


def create_before_after():
    """모니터링 전후 비교 이미지 생성 (1200x800)"""
    width, height = 1200, 800
    img = Image.new('RGB', (width, height), color='#f8f9fa')
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 50)
        font_section = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 35)
        font_text = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 22)
    except:
        font_title = ImageFont.load_default()
        font_section = ImageFont.load_default()
        font_text = ImageFont.load_default()

    # 제목
    draw.text((width // 2, 50), "모니터링 시스템 구축 전 vs 후", fill='#1a1a2e', font=font_title, anchor='mm')

    # 왼쪽: Before
    left_x = width // 4
    draw.text((left_x, 150), "❌ Before", fill='#e63946', font=font_section, anchor='mm')
    draw.rectangle([50, 200, left_x * 2 - 50, height - 50], outline='#e63946', width=3)

    before_texts = [
        "• 에러 발견: 6시간 후",
        "• 서버 접속해서 로그 확인",
        "• 성능 저하 인지 불가",
        "• 문제 원인 파악 어려움",
        "• 불안한 운영",
        "• 수동 모니터링 필요"
    ]

    y_pos = 250
    for text in before_texts:
        draw.text((left_x, y_pos), text, fill='#333333', font=font_text, anchor='mm')
        y_pos += 80

    # 오른쪽: After
    right_x = width * 3 // 4
    draw.text((right_x, 150), "✅ After", fill='#06d6a0', font=font_section, anchor='mm')
    draw.rectangle([left_x * 2 + 50, 200, width - 50, height - 50], outline='#06d6a0', width=3)

    after_texts = [
        "• 에러 발견: 1초 이내",
        "• Telegram 즉시 알림",
        "• Grafana로 실시간 확인",
        "• Trace로 원인 즉시 파악",
        "• 안심하고 운영",
        "• 자동 모니터링"
    ]

    y_pos = 250
    for text in after_texts:
        draw.text((right_x, y_pos), text, fill='#333333', font=font_text, anchor='mm')
        y_pos += 80

    return img


def create_architecture():
    """아키텍처 다이어그램 생성 (1400x1000)"""
    width, height = 1400, 1000
    img = Image.new('RGB', (width, height), color='#ffffff')
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 40)
        font_box = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 24)
        font_small = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 18)
    except:
        font_title = ImageFont.load_default()
        font_box = ImageFont.load_default()
        font_small = ImageFont.load_default()

    # 제목
    draw.text((width // 2, 40), "Grafana 관찰성 스택 아키텍처", fill='#1a1a2e', font=font_title, anchor='mm')

    # 박스 정의 (x, y, width, height, color, label)
    boxes = [
        # FastAPI Layer
        (50, 120, 280, 150, '#4CAF50', 'FastAPI App\n+ Middleware'),
        (50, 300, 280, 80, '#66BB6A', 'TelemetryManager'),
        (50, 400, 280, 80, '#81C784', 'ErrorReporter'),

        # OTLP Layer
        (420, 250, 200, 100, '#9C27B0', 'OTLP Exporter\ngRPC: 4317'),

        # Grafana Stack Layer
        (750, 120, 200, 120, '#F46800', 'Grafana\nDashboard\n:3000'),
        (1000, 120, 200, 120, '#E91E63', 'Tempo\nTraces\n:3200'),
        (750, 280, 200, 120, '#00ACC1', 'Loki\nLogs\n:3100'),
        (1000, 280, 200, 120, '#E6522C', 'Prometheus\nMetrics\n:9090'),
        (875, 440, 200, 100, '#26A69A', 'Promtail\nLog Collector'),

        # Docker Layer
        (875, 580, 200, 80, '#607D8B', 'Docker\nContainers'),

        # Telegram Layer
        (50, 520, 280, 80, '#0088CC', 'Telegram Bot'),
    ]

    for x, y, w, h, color, label in boxes:
        # 그림자 효과
        draw.rectangle([x + 5, y + 5, x + w + 5, y + h + 5], fill='#cccccc')
        # 박스
        draw.rectangle([x, y, x + w, y + h], fill=color, outline='#333333', width=2)
        # 텍스트
        draw.text((x + w // 2, y + h // 2), label, fill='#ffffff', font=font_box, anchor='mm')

    # 화살표 그리기 (간단한 선으로)
    arrows = [
        # FastAPI -> OTLP
        (330, 210, 420, 300, '#333333'),
        (330, 340, 420, 300, '#333333'),

        # OTLP -> Grafana Stack
        (620, 280, 750, 180, '#E91E63'),  # to Grafana
        (620, 300, 1000, 180, '#E91E63'),  # to Tempo
        (620, 300, 1000, 340, '#E6522C'),  # to Prometheus

        # ErrorReporter -> Telegram
        (190, 480, 190, 520, '#0088CC'),

        # Docker -> Promtail -> Loki
        (975, 580, 975, 540, '#26A69A'),
        (975, 440, 850, 400, '#00ACC1'),

        # Grafana connections (dotted - 짧은 선들로 표현)
        (850, 180, 1000, 180, '#666666'),
        (850, 240, 850, 280, '#666666'),
        (950, 240, 1100, 280, '#666666'),
    ]

    for x1, y1, x2, y2, color in arrows:
        draw.line([x1, y1, x2, y2], fill=color, width=3)
        # 화살표 끝 (간단한 삼각형)
        if x2 > x1:
            draw.polygon([x2, y2, x2 - 10, y2 - 5, x2 - 10, y2 + 5], fill=color)
        elif x2 < x1:
            draw.polygon([x2, y2, x2 + 10, y2 - 5, x2 + 10, y2 + 5], fill=color)
        elif y2 > y1:
            draw.polygon([x2, y2, x2 - 5, y2 - 10, x2 + 5, y2 - 10], fill=color)
        else:
            draw.polygon([x2, y2, x2 - 5, y2 + 10, x2 + 5, y2 + 10], fill=color)

    # 범례
    legend_y = 720
    draw.text((width // 2, legend_y), "핵심 기능:", fill='#333333', font=font_small, anchor='mm')

    legend_items = [
        "• Trace-to-Log 연동으로 트레이스와 로그 통합",
        "• Promtail이 Docker 로그 자동 수집",
        "• Telegram으로 실시간 에러 알림",
        "• Raspberry Pi 5 최적화 (CPU/메모리 제한)"
    ]

    y_pos = legend_y + 40
    for item in legend_items:
        draw.text((width // 2, y_pos), item, fill='#555555', font=font_small, anchor='mm')
        y_pos += 35

    return img


def main():
    """메인 함수"""
    # images 디렉토리 생성
    images_dir = os.path.join(os.path.dirname(__file__), 'images')
    os.makedirs(images_dir, exist_ok=True)

    print("🎨 모니터링 시스템 이미지 생성 중...")

    # 1. 썸네일 생성
    print("  1/3 썸네일 이미지 생성 중...")
    thumbnail = create_thumbnail()
    thumbnail_path = os.path.join(images_dir, 'monitoring_thumbnail.png')
    thumbnail.save(thumbnail_path)
    print(f"  ✅ 저장: {thumbnail_path}")

    # 2. Before/After 비교 생성
    print("  2/3 Before/After 비교 이미지 생성 중...")
    before_after = create_before_after()
    before_after_path = os.path.join(images_dir, 'before_after_monitoring.png')
    before_after.save(before_after_path)
    print(f"  ✅ 저장: {before_after_path}")

    # 3. 아키텍처 다이어그램 생성
    print("  3/3 아키텍처 다이어그램 생성 중...")
    architecture = create_architecture()
    architecture_path = os.path.join(images_dir, 'monitoring_architecture.png')
    architecture.save(architecture_path)
    print(f"  ✅ 저장: {architecture_path}")

    print("\n✨ 모든 이미지 생성 완료!")
    print("\n생성된 이미지:")
    print(f"  - {thumbnail_path}")
    print(f"  - {before_after_path}")
    print(f"  - {architecture_path}")
    print("\n📝 블로그 글에서 이미지를 확인하세요:")
    print("  - blog/blog_6_monitoring.md")


if __name__ == '__main__':
    main()
