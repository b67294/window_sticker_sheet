"""按目标小块宽高比把整图自动切块。

独立于主 pipeline 的纯工具模块：只关心比例，不涉及物理尺寸。
切块规则以后大概率要调，所有策略都集中在 plan_grids() 里，改这一个函数即可。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from PIL import Image


MAX_COLUMNS = 8
MAX_ROWS = 8


@dataclass
class GridPlan:
    columns: int
    rows: int
    tile_count: int
    # 为了让每块严格等于目标比例，需要从原图居中裁掉的面积占比（0~1）。
    crop_loss: float
    # 居中裁剪框（像素，左上右下）。
    crop_box: tuple[int, int, int, int]
    tile_width: int
    tile_height: int

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["crop_box"] = list(self.crop_box)
        return data


def plan_grids(
    image_width: int,
    image_height: int,
    ratio_width: float,
    ratio_height: float,
    max_columns: int = MAX_COLUMNS,
    max_rows: int = MAX_ROWS,
) -> list[GridPlan]:
    """枚举所有网格，按“裁得少优先，块数少次之”排序返回。

    对每个 列c×行n：整幅裁剪目标比例为 (c*ratio_w) : (n*ratio_h)，
    在原图内做最大居中裁剪，损失面积占比即为该方案的代价。
    """
    if ratio_width <= 0 or ratio_height <= 0:
        raise ValueError("小块宽高比必须为正数")
    if image_width <= 0 or image_height <= 0:
        raise ValueError("图片尺寸无效")
    plans: list[GridPlan] = []
    for columns in range(1, max_columns + 1):
        for rows in range(1, max_rows + 1):
            # 从整数边推导另一边（避免浮点截断），取放得下的最大一组。
            candidates: list[tuple[int, int]] = []
            width_first = image_width // columns
            height_derived = round(width_first * ratio_height / ratio_width)
            if width_first >= 1 and height_derived >= 1 and height_derived * rows <= image_height:
                candidates.append((width_first, height_derived))
            height_first = image_height // rows
            width_derived = round(height_first * ratio_width / ratio_height)
            if height_first >= 1 and width_derived >= 1 and width_derived * columns <= image_width:
                candidates.append((width_derived, height_first))
            if not candidates:
                continue
            tile_width, tile_height = max(candidates, key=lambda dims: dims[0] * dims[1])
            used_width = tile_width * columns
            used_height = tile_height * rows
            left = (image_width - used_width) // 2
            top = (image_height - used_height) // 2
            crop_loss = 1.0 - (used_width * used_height) / (image_width * image_height)
            plans.append(GridPlan(
                columns=columns,
                rows=rows,
                tile_count=columns * rows,
                crop_loss=round(crop_loss, 6),
                crop_box=(left, top, left + used_width, top + used_height),
                tile_width=tile_width,
                tile_height=tile_height,
            ))
    plans.sort(key=lambda plan: (round(plan.crop_loss, 4), plan.tile_count))
    return plans


def choose_plan(
    image_width: int,
    image_height: int,
    ratio_width: float,
    ratio_height: float,
    columns: int | None = None,
    rows: int | None = None,
) -> tuple[GridPlan, list[GridPlan]]:
    """自动选最优方案；指定 columns/rows 时按指定网格返回。"""
    plans = plan_grids(image_width, image_height, ratio_width, ratio_height)
    if not plans:
        raise ValueError("在允许的网格范围内找不到可行的切块方案")
    if columns or rows:
        forced = [
            plan for plan in plans
            if (not columns or plan.columns == columns) and (not rows or plan.rows == rows)
        ]
        if not forced:
            raise ValueError(f"指定的网格 {columns or '自动'}列×{rows or '自动'}行 不可行")
        return forced[0], plans
    return plans[0], plans


def resize_image(
    source_path: Path,
    output_dir: Path,
    ratio_width: float,
    ratio_height: float,
    mode: str = "stretch",
    target_width: int | None = None,
    target_height: int | None = None,
) -> dict[str, Any]:
    """把整图调整为目标宽高比。

    mode="stretch"（默认）直接拉伸到目标比例；mode="crop" 居中裁剪到目标比例。
    未指定目标像素时，取原图较长边作为输出的较长边，另一边按比例推导。
    """
    if ratio_width <= 0 or ratio_height <= 0:
        raise ValueError("宽高比必须为正数")
    if mode not in {"stretch", "crop"}:
        raise ValueError(f"不支持的 resize 模式: {mode}")
    with Image.open(source_path) as source:
        source.load()
        image = source.convert("RGBA") if source.mode in {"RGBA", "LA", "P"} else source.convert("RGB")
    original_size = [image.width, image.height]

    if target_width and target_height:
        out_width, out_height = int(target_width), int(target_height)
    elif target_width:
        out_width = int(target_width)
        out_height = max(1, round(out_width * ratio_height / ratio_width))
    elif target_height:
        out_height = int(target_height)
        out_width = max(1, round(out_height * ratio_width / ratio_height))
    else:
        longer = max(image.width, image.height)
        if ratio_height >= ratio_width:
            out_height = longer
            out_width = max(1, round(longer * ratio_width / ratio_height))
        else:
            out_width = longer
            out_height = max(1, round(longer * ratio_height / ratio_width))
    if out_width < 1 or out_height < 1:
        raise ValueError("目标尺寸无效")

    if mode == "crop":
        target = ratio_width / ratio_height
        crop_width = float(image.width)
        crop_height = crop_width / target
        if crop_height > image.height:
            crop_height = float(image.height)
            crop_width = crop_height * target
        left = (image.width - crop_width) / 2.0
        top = (image.height - crop_height) / 2.0
        image = image.crop((round(left), round(top), round(left + crop_width), round(top + crop_height)))
    resized = image.resize((out_width, out_height), Image.Resampling.LANCZOS)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "resized.png"
    resized.save(output_path)
    return {
        "source_size": original_size,
        "ratio": [float(ratio_width), float(ratio_height)],
        "mode": mode,
        "output_size": [out_width, out_height],
        "output": "resized.png",
    }


def slice_image(
    source_path: Path,
    output_dir: Path,
    ratio_width: float,
    ratio_height: float,
    columns: int | None = None,
    rows: int | None = None,
) -> dict[str, Any]:
    """切块并落盘：tiles/tile-r{行}c{列}.png + preview.jpg（带网格线）。"""
    with Image.open(source_path) as source:
        source.load()
        image = source.convert("RGBA") if source.mode in {"RGBA", "LA", "P"} else source.convert("RGB")
    plan, alternatives = choose_plan(image.width, image.height, ratio_width, ratio_height, columns, rows)

    tiles_dir = output_dir / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    cropped = image.crop(plan.crop_box)
    tile_files: list[dict[str, Any]] = []
    pdf_pages: list[Image.Image] = []
    for row in range(plan.rows):
        for column in range(plan.columns):
            box = (
                column * plan.tile_width,
                row * plan.tile_height,
                (column + 1) * plan.tile_width,
                (row + 1) * plan.tile_height,
            )
            tile = cropped.crop(box)
            name = f"tile-r{row + 1}c{column + 1}.png"
            tile.save(tiles_dir / name)
            tile_files.append({"name": name, "row": row + 1, "column": column + 1})
            # PDF 不支持透明通道，压到白底。
            if tile.mode == "RGBA":
                page = Image.new("RGB", tile.size, (255, 255, 255))
                page.paste(tile, mask=tile.getchannel("A"))
            else:
                page = tile.convert("RGB")
            pdf_pages.append(page)

    # 所有切块合并为一个多页 PDF（每块一页，行优先顺序，300 DPI 折算页面尺寸）。
    pdf_path = output_dir / "tiles.pdf"
    pdf_pages[0].save(
        pdf_path,
        save_all=True,
        append_images=pdf_pages[1:],
        resolution=300.0,
    )

    preview = image.convert("RGB").copy()
    from PIL import ImageDraw

    draw = ImageDraw.Draw(preview)
    line_width = max(2, round(min(preview.size) * 0.004))
    x0, y0, x1, y1 = plan.crop_box
    draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=(244, 111, 87), width=line_width)
    for column in range(1, plan.columns):
        x = x0 + column * plan.tile_width
        draw.line((x, y0, x, y1), fill=(244, 111, 87), width=line_width)
    for row in range(1, plan.rows):
        y = y0 + row * plan.tile_height
        draw.line((x0, y, x1, y), fill=(244, 111, 87), width=line_width)
    preview_path = output_dir / "preview.jpg"
    preview.save(preview_path, quality=90)

    return {
        "source_size": [image.width, image.height],
        "ratio": [float(ratio_width), float(ratio_height)],
        "plan": plan.to_dict(),
        "alternatives": [item.to_dict() for item in alternatives[:6]],
        "tiles": tile_files,
        "preview": "preview.jpg",
        "pdf": "tiles.pdf",
    }
