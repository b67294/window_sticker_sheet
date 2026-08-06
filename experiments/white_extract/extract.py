"""白色剪影窗贴提取实验：深色审稿底 → 白图案透明 PNG。

用法: python extract.py <图片或目录> [更多图片或目录 ...]
  传目录时递归处理其中所有 PNG（自动跳过非深底图、已生成的 out/ 产物）。
步骤: ①列统计定位中央分隔带并移除 ②按已知底色反解 Alpha（保留柔和边缘）
输出: 同目录 out/ 下 <名>-extracted.png（透明底）+ <名>-preview.png（深底预览）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image


def extract(path: Path) -> None:
    out_dir = path.parent / "out"
    out_dir.mkdir(exist_ok=True)
    rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
    height, width, _ = rgb.shape
    luma = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)

    # 底色取四角 12x12 中值；白电平取 99.5 分位。
    corners = np.concatenate([
        luma[:12, :12].ravel(), luma[:12, -12:].ravel(),
        luma[-12:, :12].ravel(), luma[-12:, -12:].ravel(),
    ])
    bg_level = float(np.median(corners))
    white_level = float(np.percentile(luma, 99.5))
    if bg_level > 120 or white_level - bg_level < 60:
        raise ValueError(f"跳过（非深底白图：bg={bg_level:.0f}, white={white_level:.0f}）")

    # ① 分隔带：中部 60% 宽度内，整列近白占比 > 95% 的连续列区间。
    near_white = luma > bg_level + 0.85 * (white_level - bg_level)
    column_fraction = near_white.mean(axis=0)
    center = np.arange(width)
    candidates = (column_fraction > 0.95) & (center > width * 0.2) & (center < width * 0.8)
    divider_columns = np.flatnonzero(candidates)
    if divider_columns.size:
        # 扩一圈过渡像素，避免留下抗锯齿灰边。
        left, right = divider_columns.min() - 2, divider_columns.max() + 2
        print(f"{path.name}: 分隔带列区间 [{left}, {right}] 宽 {right - left + 1}px（占比 {(right-left+1)/width:.1%}）")
    else:
        left, right = -1, -2
        print(f"{path.name}: 未检测到分隔带（单栏图？），跳过移除")

    # ② 反解 Alpha：线性映射 + 两端 5% 收紧，压掉底噪。
    alpha = (luma - bg_level) / (white_level - bg_level)
    alpha = np.clip((alpha - 0.05) / 0.90, 0.0, 1.0)
    if divider_columns.size:
        alpha[:, left:right + 1] = 0.0

    result = np.zeros((height, width, 4), dtype=np.uint8)
    result[..., :3] = 255  # 图案设为纯白，由 alpha 表达形状与边缘
    result[..., 3] = np.round(alpha * 255).astype(np.uint8)
    extracted_path = out_dir / f"{path.stem}-extracted.png"
    Image.fromarray(result, "RGBA").save(extracted_path)

    preview_bg = np.empty_like(rgb)
    preview_bg[...] = (31, 41, 55)  # #1f2937 深蓝灰
    preview = (alpha[..., None] * 255.0 + (1.0 - alpha[..., None]) * preview_bg).astype(np.uint8)
    preview_path = out_dir / f"{path.stem}-preview.png"
    Image.fromarray(preview, "RGB").save(preview_path)

    coverage = float((alpha > 0.5).mean())
    print(f"  bg亮度={bg_level:.0f} 白电平={white_level:.0f} | 图案覆盖率 {coverage:.1%} | → {extracted_path.name}, {preview_path.name}")


def collect_images(args: list[str]) -> list[Path]:
    images: list[Path] = []
    for arg in args:
        target = Path(arg)
        if target.is_dir():
            images.extend(sorted(target.rglob("*.png")))
        elif target.is_file():
            images.append(target)
        else:
            print(f"找不到: {arg}")
    # 跳过自身产物与任务源图（source.* 是上传的电商原图，不是提取对象）。
    return [
        item for item in images
        if item.parent.name != "out"
        and not item.stem.endswith(("-extracted", "-preview"))
        and not item.stem.startswith("source")
    ]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python extract.py <图片或目录>...")
    targets = collect_images(sys.argv[1:])
    if not targets:
        raise SystemExit("没有找到可处理的 PNG")
    done = skipped = failed = 0
    for item in targets:
        try:
            extract(item)
            done += 1
        except ValueError as reason:
            print(f"{item.name}: {reason}")
            skipped += 1
        except Exception as error:  # noqa: BLE001 - 批处理单张失败不中断
            print(f"{item.name}: 失败 {error}")
            failed += 1
    print(f"\n完成 {done} 张，跳过 {skipped} 张，失败 {failed} 张（共扫描 {len(targets)} 张）")
