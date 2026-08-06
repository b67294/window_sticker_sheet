from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageOps


ROOT = Path(r"F:\Longpean-AIGC\19-脚本代码\window_sticker_sheet_workbench")
RUN = ROOT / "runs" / "20260729-193043-06645399"
OUTPUT = ROOT / "docs" / "assets" / "window-sticker-summary" / "semantic-grouping-highlight.svg"


def crop_data(path: Path) -> str:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        # HAPPY HALLOWEEN occupies the same region in both diagnostic overlays.
        image = image.crop((48, 326, 580, 585))
        image = ImageOps.fit(image, (650, 300), method=Image.Resampling.LANCZOS)
        stream = io.BytesIO()
        image.save(stream, format="JPEG", quality=90, optimize=True)
    return base64.b64encode(stream.getvalue()).decode("ascii")


def main() -> None:
    left = crop_data(RUN / "components" / "components-overlay.png")
    right = crop_data(RUN / "geometry" / "groups-contours-overlay.png")
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="720" viewBox="0 0 1600 720">
<defs>
  <pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="1.5" cy="1.5" r="1.3" fill="#DDE5EF"/></pattern>
  <filter id="shadow" x="-20%" y="-20%" width="140%" height="150%"><feDropShadow dx="0" dy="5" stdDeviation="8" flood-color="#172033" flood-opacity="0.10"/></filter>
  <clipPath id="leftCrop"><rect x="72" y="216" width="650" height="300" rx="12"/></clipPath>
  <clipPath id="rightCrop"><rect x="878" y="216" width="650" height="300" rx="12"/></clipPath>
</defs>
<rect width="1600" height="720" rx="22" fill="#F8FAFC"/>
<rect width="1600" height="720" rx="22" fill="url(#grid)" opacity="0.72"/>
<text x="70" y="62" font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif" font-size="34" font-weight="700" fill="#172033">像素不相连，不等于业务上无关</text>
<text x="70" y="98" font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif" font-size="17" fill="#667085">同一句文字在像素层面是碎片，在安装与裁切时却必须保持为一个整体。</text>

<rect x="52" y="132" width="690" height="430" rx="18" fill="#FFFFFF" stroke="#D8E0EA" filter="url(#shadow)"/>
<rect x="52" y="132" width="690" height="6" rx="3" fill="#C04444"/>
<rect x="72" y="158" width="136" height="30" rx="15" fill="#FBEAEA"/>
<text x="140" y="179" text-anchor="middle" font-family="Microsoft YaHei, sans-serif" font-size="13" font-weight="700" fill="#A63838">仅按像素 / 距离</text>
<text x="226" y="181" font-family="Microsoft YaHei, sans-serif" font-size="20" font-weight="700" fill="#172033">整句 = 13 个编号组件</text>
<image x="72" y="216" width="650" height="300" href="data:image/jpeg;base64,{left}" clip-path="url(#leftCrop)"/>
<text x="72" y="544" font-family="Microsoft YaHei, sans-serif" font-size="15" fill="#667085">每个框都可能被单独移动、旋转或排到不同位置</text>

<circle cx="800" cy="347" r="36" fill="#3168D8"/>
<path d="M784 347h28m-9-10 10 10-10 10" fill="none" stroke="#FFFFFF" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
<text x="800" y="404" text-anchor="middle" font-family="Microsoft YaHei, sans-serif" font-size="13" font-weight="700" fill="#3168D8">语义判断</text>

<rect x="858" y="132" width="690" height="430" rx="18" fill="#FFFFFF" stroke="#D8E0EA" filter="url(#shadow)"/>
<rect x="858" y="132" width="690" height="6" rx="3" fill="#23875A"/>
<rect x="878" y="158" width="120" height="30" rx="15" fill="#E5F4EC"/>
<text x="938" y="179" text-anchor="middle" font-family="Microsoft YaHei, sans-serif" font-size="13" font-weight="700" fill="#1F7650">语义分组后</text>
<text x="1016" y="181" font-family="Microsoft YaHei, sans-serif" font-size="20" font-weight="700" fill="#172033">13 个组件 = 1 个刚性组</text>
<image x="878" y="216" width="650" height="300" href="data:image/jpeg;base64,{right}" clip-path="url(#rightCrop)"/>
<text x="878" y="544" font-family="Microsoft YaHei, sans-serif" font-size="15" fill="#667085">整句共同移动与排版，保留原始相对位置</text>

<rect x="52" y="592" width="1496" height="88" rx="14" fill="#FFFFFF" stroke="#D8E0EA"/>
<rect x="52" y="592" width="6" height="88" rx="3" fill="#3168D8"/>
<text x="78" y="622" font-family="Microsoft YaHei, sans-serif" font-size="13" font-weight="700" fill="#3168D8">模型原始理由 · confidence 0.99</text>
<text x="78" y="653" font-family="Microsoft YaHei, sans-serif" font-size="18" font-weight="600" fill="#213F60">“共同组成两行 ‘HAPPY HALLOWEEN’ 完整文字短语”</text>
<rect x="1114" y="610" width="104" height="28" rx="14" fill="#E3EBF5"/><text x="1166" y="629" text-anchor="middle" font-family="Microsoft YaHei, sans-serif" font-size="12" font-weight="700" fill="#213F60">阈值 ≥ 0.90</text>
<rect x="1230" y="610" width="134" height="28" rx="14" fill="#E3EBF5"/><text x="1297" y="629" text-anchor="middle" font-family="Microsoft YaHei, sans-serif" font-size="12" font-weight="700" fill="#213F60">只合并已有编号</text>
<rect x="1376" y="610" width="146" height="28" rx="14" fill="#E3EBF5"/><text x="1449" y="629" text-anchor="middle" font-family="Microsoft YaHei, sans-serif" font-size="12" font-weight="700" fill="#213F60">失败保留距离分组</text>
</svg>'''
    OUTPUT.write_text(svg, encoding="utf-8")


if __name__ == "__main__":
    main()
