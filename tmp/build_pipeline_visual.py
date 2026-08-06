from __future__ import annotations

import base64
import html
import io
from pathlib import Path

from PIL import Image, ImageOps


ROOT = Path(r"F:\Longpean-AIGC\19-脚本代码\window_sticker_sheet_workbench")
RUN = ROOT / "runs" / "20260729-193043-06645399"
OUTPUT = ROOT / "docs" / "assets" / "window-sticker-summary" / "full-production-pipeline.svg"

STAGES = [
    ("输入", "电商场景原图", RUN / "upload-source.webp", "INPUT", "#3168D8"),
    ("创新生图", "生成同系列白底母版", RUN / "generate" / "master.png", "01", "#7C5CC4"),
    ("去背景", "提取透明 Alpha", RUN / "key" / "alpha-checker.png", "02", "#23875A"),
    ("组件分析", "识别并标记独立元素", RUN / "components" / "components-overlay.png", "03", "#3168D8"),
    ("轮廓", "生成分组与裁切边界", RUN / "geometry" / "groups-contours-overlay.png", "04", "#B7790B"),
    ("排版候选", "比较多套 Sheet 方案", RUN / "layout" / "candidate-4" / "contact-sheet.jpg", "05", "#7C5CC4"),
    ("生产文件", "导出 300 DPI 成品", RUN / "final" / "white" / "sheet-01.jpg", "06", "#23875A"),
]


def thumbnail_data(path: Path, size: tuple[int, int] = (210, 150)) -> str:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        stream = io.BytesIO()
        image.save(stream, format="JPEG", quality=84, optimize=True)
    return base64.b64encode(stream.getvalue()).decode("ascii")


def esc(value: str) -> str:
    return html.escape(value, quote=True)


def main() -> None:
    width, height = 1800, 590
    card_w, card_h, gap = 230, 284, 18
    left = (width - (len(STAGES) * card_w + (len(STAGES) - 1) * gap)) / 2
    card_y = 190
    rail_y = 154

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<defs>',
        '<pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="1.5" cy="1.5" r="1.3" fill="#DDE5EF"/></pattern>',
        '<filter id="shadow" x="-20%" y="-20%" width="140%" height="150%"><feDropShadow dx="0" dy="5" stdDeviation="7" flood-color="#172033" flood-opacity="0.09"/></filter>',
        '<marker id="arrow" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto"><path d="M0,0 L9,4.5 L0,9 Z" fill="#98A6B8"/></marker>',
        '<clipPath id="thumb"><rect x="0" y="0" width="210" height="150" rx="9"/></clipPath>',
        '</defs>',
        '<rect width="1800" height="590" rx="22" fill="#F8FAFC"/>',
        '<rect width="1800" height="590" rx="22" fill="url(#grid)" opacity="0.72"/>',
        '<text x="70" y="64" font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif" font-size="34" font-weight="700" fill="#172033">从一张电商图到可生产文件</text>',
        '<text x="70" y="100" font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif" font-size="17" fill="#667085">输入只是起点；后续六个步骤把创意图逐层转换为可裁切、可排版、可交付的生产资产。</text>',
        f'<line x1="{left + 20}" y1="{rail_y}" x2="{width - left - 20}" y2="{rail_y}" stroke="#B8C4D1" stroke-width="3" marker-end="url(#arrow)"/>',
    ]

    for index, (title, detail, path, badge, tone) in enumerate(STAGES):
        x = left + index * (card_w + gap)
        cx = x + card_w / 2
        image_data = thumbnail_data(path)
        parts.extend([
            f'<circle cx="{cx}" cy="{rail_y}" r="15" fill="#F8FAFC" stroke="{tone}" stroke-width="4"/>',
            f'<circle cx="{cx}" cy="{rail_y}" r="5" fill="{tone}"/>',
            f'<rect x="{x}" y="{card_y}" width="{card_w}" height="{card_h}" rx="14" fill="#FFFFFF" stroke="#D8E0EA" filter="url(#shadow)"/>',
            f'<rect x="{x}" y="{card_y}" width="{card_w}" height="5" rx="3" fill="{tone}"/>',
            f'<g transform="translate({x + 10},{card_y + 14})"><image width="210" height="150" href="data:image/jpeg;base64,{image_data}" clip-path="url(#thumb)"/></g>',
            f'<rect x="{x + 14}" y="{card_y + 180}" width="{46 if badge != "INPUT" else 62}" height="25" rx="12.5" fill="{tone}" opacity="0.12"/>',
            f'<text x="{x + 14 + (23 if badge != "INPUT" else 31)}" y="{card_y + 198}" text-anchor="middle" font-family="Inter, Microsoft YaHei, sans-serif" font-size="12" font-weight="700" fill="{tone}">{esc(badge)}</text>',
            f'<text x="{x + 14}" y="{card_y + 232}" font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif" font-size="19" font-weight="700" fill="#172033">{esc(title)}</text>',
            f'<text x="{x + 14}" y="{card_y + 260}" font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif" font-size="13.5" fill="#667085">{esc(detail)}</text>',
        ])

    parts.extend([
        '<rect x="70" y="510" width="1660" height="52" rx="12" fill="#FFFFFF" stroke="#D8E0EA"/>',
        '<rect x="70" y="510" width="6" height="52" rx="3" fill="#3168D8"/>',
        '<text x="96" y="542" font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif" font-size="16" font-weight="600" fill="#213F60">这不是一次 API 调用，而是一条包含生成、视觉处理、结构分析、排版决策与生产导出的完整流水线。</text>',
        '</svg>',
    ])
    OUTPUT.write_text("".join(parts), encoding="utf-8")


if __name__ == "__main__":
    main()
