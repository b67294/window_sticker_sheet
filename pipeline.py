from __future__ import annotations

import base64
import concurrent.futures
import io
import json
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas
from shapely import affinity, make_valid
from shapely.geometry import GeometryCollection, Polygon, box, mapping, shape
from shapely.ops import unary_union


STAGES = ["input", "generate", "key", "components", "geometry", "layout"]


def default_settings() -> dict[str, Any]:
    return {
        "install_width_mm": 800.0,
        "install_height_mm": 1200.0,
        "sheet_width_mm": 381.0,
        "sheet_height_mm": 304.8,
        "sheet_margin_mm": 5.0,
        "cut_offset_mm": 1.5,
        "spacing_mm": 2.0,
        "group_gap_mm": 2.0,
        "key_low": 12.0,
        "key_high": 72.0,
        "morph_kernel": 3,
        "min_component_area": 60,
        "alpha_threshold": 64,
        "semantic_grouping_enabled": True,
        "semantic_min_confidence": 0.90,
        "simplify_mm": 0.3,
        "utilization_weight": 0.7,
        "compactness_weight": 0.65,
        "alignment_weight": 0.25,
        "balance_weight": 0.10,
        "layout_mode": "tidy_compact",
        "auto_shrink_enabled": True,
        "max_shrink_ratio": 0.08,
        "preview_dpi": 96,
        "output_dpi": 300,
        "candidate_count": 4,
    }


def merge_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    merged = default_settings()
    if settings:
        incoming = dict(settings)
        if "compactness_weight" not in incoming and "utilization_weight" in incoming:
            incoming["compactness_weight"] = incoming["utilization_weight"]
        merged.update(incoming)
    merged["key_high"] = max(float(merged["key_high"]), float(merged["key_low"]) + 1.0)
    merged["morph_kernel"] = max(1, int(merged["morph_kernel"]))
    if merged["morph_kernel"] % 2 == 0:
        merged["morph_kernel"] += 1
    merged["candidate_count"] = 4
    merged["auto_shrink_enabled"] = bool(merged.get("auto_shrink_enabled", True))
    merged["max_shrink_ratio"] = min(0.15, max(0.0, float(merged.get("max_shrink_ratio", 0.08))))
    merged["layout_mode"] = "tidy_compact"
    return merged


def mm_per_pixel(width: int, height: int, settings: dict[str, Any]) -> float:
    return min(
        float(settings["install_width_mm"]) / max(width, 1),
        float(settings["install_height_mm"]) / max(height, 1),
    )


def _relative(path: Path, job_dir: Path) -> str:
    return path.relative_to(job_dir).as_posix()


def artifact(stage: str, name: str, label: str, path: Path, job_dir: Path, kind: str = "image") -> dict[str, Any]:
    item: dict[str, Any] = {
        "stage": stage,
        "name": name,
        "label": label,
        "path": _relative(path, job_dir),
        "kind": kind,
    }
    if kind == "image":
        with Image.open(path) as image:
            item.update(width=image.width, height=image.height)
    return item


def replace_stage_artifacts(job: dict[str, Any], stage: str, items: list[dict[str, Any]]) -> None:
    job["artifacts"] = [item for item in job.get("artifacts", []) if item.get("stage") != stage]
    job["artifacts"].extend(items)


def invalidate_after(job: dict[str, Any], stage: str) -> None:
    index = STAGES.index(stage)
    invalid = set(STAGES[index + 1 :])
    job["artifacts"] = [item for item in job.get("artifacts", []) if item.get("stage") not in invalid]
    for key, owner in {
        "primitives": "components",
        "groups": "components",
        "geometry": "geometry",
        "candidates": "layout",
        "selected_candidate": "layout",
    }.items():
        if owner in invalid:
            if key in {"primitives", "groups", "geometry", "candidates"}:
                job[key] = []
            else:
                job[key] = None


def _open_bgr(path: Path) -> np.ndarray:
    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法读取图片：{path.name}")
    return image


def _write_cv(path: Path, image: np.ndarray) -> None:
    suffix = path.suffix or ".png"
    ok, encoded = cv2.imencode(suffix, image)
    if not ok:
        raise RuntimeError(f"无法写入图片：{path.name}")
    encoded.tofile(path)


def _border_pixels(image: np.ndarray, ratio: float = 0.05) -> np.ndarray:
    height, width = image.shape[:2]
    band_x = max(1, int(width * ratio))
    band_y = max(1, int(height * ratio))
    return np.concatenate(
        [
            image[:band_y].reshape(-1, 3),
            image[-band_y:].reshape(-1, 3),
            image[:, :band_x].reshape(-1, 3),
            image[:, -band_x:].reshape(-1, 3),
        ],
        axis=0,
    )


def run_chroma_key(master_path: Path, job_dir: Path, settings: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    stage_dir = job_dir / "key"
    stage_dir.mkdir(parents=True, exist_ok=True)
    bgr = _open_bgr(master_path)
    height, width = bgr.shape[:2]
    border = _border_pixels(bgr)
    key_bgr = np.median(border, axis=0).astype(np.float32)
    distance = np.linalg.norm(bgr.astype(np.float32) - key_bgr[None, None, :], axis=2)
    low = float(settings["key_low"])
    high = float(settings["key_high"])
    t = np.clip((distance - low) / max(high - low, 1.0), 0.0, 1.0)
    raw_alpha = (t * t * (3.0 - 2.0 * t) * 255.0).astype(np.uint8)
    binary = (raw_alpha >= int(settings["alpha_threshold"])).astype(np.uint8) * 255

    kernel_size = int(settings["morph_kernel"])
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    keep = np.zeros_like(binary)
    min_area = max(1, int(settings["min_component_area"]))
    for label in range(1, count):
        if int(stats[label, cv2.CC_STAT_AREA]) >= min_area:
            keep[labels == label] = 255
    softened = cv2.GaussianBlur(keep, (0, 0), sigmaX=0.8, sigmaY=0.8)
    clean_alpha = np.minimum(raw_alpha, softened)

    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32)
    alpha_float = clean_alpha.astype(np.float32) / 255.0
    edge_strength = 1.0 - alpha_float
    green_excess = np.maximum(0.0, rgb[:, :, 1] - np.maximum(rgb[:, :, 0], rgb[:, :, 2]))
    rgb[:, :, 1] -= green_excess * np.clip(edge_strength * 1.4, 0.0, 1.0)
    rgba = np.dstack([np.clip(rgb, 0, 255).astype(np.uint8), clean_alpha])

    raw_mask_path = stage_dir / "raw-alpha.png"
    clean_mask_path = stage_dir / "clean-alpha.png"
    foreground_path = stage_dir / "foreground.png"
    key_sample_path = stage_dir / "key-sample.png"
    overlay_path = stage_dir / "key-overlay.png"
    _write_cv(raw_mask_path, raw_alpha)
    _write_cv(clean_mask_path, clean_alpha)
    Image.fromarray(rgba, "RGBA").save(foreground_path)
    key_rgb = tuple(int(v) for v in key_bgr[::-1])
    Image.new("RGB", (240, 120), key_rgb).save(key_sample_path)
    overlay = bgr.copy()
    overlay[clean_alpha < int(settings["alpha_threshold"])] = (180, 0, 180)
    overlay = cv2.addWeighted(bgr, 0.55, overlay, 0.45, 0)
    _write_cv(overlay_path, overlay)

    metrics = {
        "master_width": width,
        "master_height": height,
        "key_rgb": list(key_rgb),
        "key_hex": "#%02x%02x%02x" % key_rgb,
        "foreground_pixels": int(np.count_nonzero(clean_alpha)),
        "foreground_ratio": round(float(np.count_nonzero(clean_alpha)) / float(width * height), 6),
        "mm_per_pixel": mm_per_pixel(width, height, settings),
    }
    metrics_path = stage_dir / "key-metrics.json"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = [
        artifact("key", "key-sample", "自动采样背景色", key_sample_path, job_dir),
        artifact("key", "raw-alpha", "原始软蒙版", raw_mask_path, job_dir),
        artifact("key", "clean-alpha", "清理后 Alpha", clean_mask_path, job_dir),
        artifact("key", "foreground", "去底前景", foreground_path, job_dir),
        artifact("key", "key-overlay", "背景识别覆盖层", overlay_path, job_dir),
        artifact("key", "key-metrics", "色键指标", metrics_path, job_dir, "json"),
    ]
    return artifacts, metrics


def run_alpha_passthrough(master_path: Path, job_dir: Path, settings: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Preserve a supplied alpha channel and create the standard key-stage files.

    The component and geometry stages intentionally consume a stable pair of
    files (``key/foreground.png`` and ``key/clean-alpha.png``).  Transparent
    inputs therefore pass through this adapter instead of taking the chroma-key
    path.  RGB is changed only where alpha is exactly zero, where it is
    invisible, to prevent hidden matte colours from leaking during resizing.
    """

    stage_dir = job_dir / "key"
    stage_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(master_path) as source:
        has_alpha = "A" in source.getbands() or source.info.get("transparency") is not None
        if not has_alpha:
            raise ValueError("透明底入口要求 PNG 或 WEBP 文件包含 Alpha 通道")
        rgba = np.array(source.convert("RGBA"))

    height, width = rgba.shape[:2]
    alpha = rgba[:, :, 3].copy()
    alpha_min = int(alpha.min())
    alpha_max = int(alpha.max())
    if alpha_max == 0:
        raise ValueError("图片完全透明，没有可分析的前景")
    if alpha_min == 255:
        raise ValueError("图片 Alpha 全部不透明；请改用纯色母版入口")

    foreground = rgba.copy()
    foreground[alpha == 0, :3] = 0

    raw_mask_path = stage_dir / "raw-alpha.png"
    clean_mask_path = stage_dir / "clean-alpha.png"
    foreground_path = stage_dir / "foreground.png"
    checker_path = stage_dir / "alpha-checker.png"
    metrics_path = stage_dir / "key-metrics.json"

    Image.fromarray(alpha, "L").save(raw_mask_path)
    Image.fromarray(alpha, "L").save(clean_mask_path)
    foreground_image = Image.fromarray(foreground, "RGBA")
    foreground_image.save(foreground_path)

    tile = 16
    yy, xx = np.indices((height, width))
    checker_value = np.where(((xx // tile) + (yy // tile)) % 2 == 0, 238, 205).astype(np.uint8)
    checker = np.dstack([checker_value, checker_value, checker_value, np.full_like(checker_value, 255)])
    checker_image = Image.fromarray(checker, "RGBA")
    checker_image.alpha_composite(foreground_image)
    checker_image.convert("RGB").save(checker_path, quality=92)

    metrics = {
        "mode": "alpha_passthrough",
        "master_width": width,
        "master_height": height,
        "source_has_alpha": True,
        "alpha_min": alpha_min,
        "alpha_max": alpha_max,
        "transparent_pixels": int(np.count_nonzero(alpha == 0)),
        "partial_alpha_pixels": int(np.count_nonzero((alpha > 0) & (alpha < 255))),
        "foreground_pixels": int(np.count_nonzero(alpha)),
        "foreground_ratio": round(float(np.count_nonzero(alpha)) / float(width * height), 6),
        "mm_per_pixel": mm_per_pixel(width, height, settings),
    }
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = [
        artifact("key", "raw-alpha", "上传文件的原始 Alpha", raw_mask_path, job_dir),
        artifact("key", "clean-alpha", "组件分析使用的 Alpha（未重算）", clean_mask_path, job_dir),
        artifact("key", "foreground", "保留原始 Alpha 的前景", foreground_path, job_dir),
        artifact("key", "alpha-checker", "透明边缘检查预览", checker_path, job_dir),
        artifact("key", "key-metrics", "Alpha 直通指标", metrics_path, job_dir, "json"),
    ]
    return artifacts, metrics


def _largest_external_contour(mask: np.ndarray) -> np.ndarray | None:
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    return max(contours, key=cv2.contourArea)


def _contour_to_polygon(points: Iterable[Iterable[float]]) -> Any:
    coords = [(float(point[0]), float(point[1])) for point in points]
    if len(coords) < 3:
        return Polygon()
    polygon = Polygon(coords)
    if not polygon.is_valid:
        # OpenCV contours may self-touch where an 8-connected component only
        # meets diagonally.  ``buffer(0)`` can silently keep just one lobe;
        # make_valid preserves every polygonal part.
        polygon = make_valid(polygon)
    return polygon


def run_components(job: dict[str, Any], job_dir: Path, settings: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    stage_dir = job_dir / "components"
    assets_dir = stage_dir / "assets"
    masks_dir = stage_dir / "masks"
    assets_dir.mkdir(parents=True, exist_ok=True)
    masks_dir.mkdir(parents=True, exist_ok=True)
    foreground_path = job_dir / "key" / "foreground.png"
    alpha_path = job_dir / "key" / "clean-alpha.png"
    foreground = np.array(Image.open(foreground_path).convert("RGBA"))
    alpha = np.array(Image.open(alpha_path).convert("L"))
    binary = (alpha >= int(settings["alpha_threshold"])).astype(np.uint8) * 255
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    primitives: list[dict[str, Any]] = []
    overlay = cv2.cvtColor(foreground[:, :, :3], cv2.COLOR_RGB2BGR)
    tint = np.zeros_like(overlay)
    rng = random.Random(20260721)
    mm_px = mm_per_pixel(foreground.shape[1], foreground.shape[0], settings)
    min_area = max(1, int(settings["min_component_area"]))

    for label_index in range(1, count):
        area = int(stats[label_index, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        x = int(stats[label_index, cv2.CC_STAT_LEFT])
        y = int(stats[label_index, cv2.CC_STAT_TOP])
        width = int(stats[label_index, cv2.CC_STAT_WIDTH])
        height = int(stats[label_index, cv2.CC_STAT_HEIGHT])
        local_mask = np.where(labels[y : y + height, x : x + width] == label_index, alpha[y : y + height, x : x + width], 0).astype(np.uint8)
        binary_local = (local_mask >= int(settings["alpha_threshold"])).astype(np.uint8) * 255
        contour = _largest_external_contour(binary_local)
        if contour is None or len(contour) < 3:
            continue
        contour_global = contour.reshape(-1, 2) + np.array([x, y])
        rect = cv2.minAreaRect(contour.astype(np.float32))
        primitive_id = f"p{len(primitives) + 1:03d}"
        mask_file = masks_dir / f"{primitive_id}.png"
        asset_file = assets_dir / f"{primitive_id}.png"
        Image.fromarray(local_mask, "L").save(mask_file)
        crop = foreground[y : y + height, x : x + width].copy()
        crop[:, :, 3] = local_mask
        Image.fromarray(crop, "RGBA").save(asset_file)
        color = (rng.randint(40, 235), rng.randint(40, 235), rng.randint(40, 235))
        tint[labels == label_index] = color
        cv2.rectangle(overlay, (x, y), (x + width, y + height), color, 2)
        cv2.putText(overlay, primitive_id, (x, max(14, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
        primitives.append(
            {
                "id": primitive_id,
                "area_px": area,
                "area_mm2": round(area * mm_px * mm_px, 4),
                "centroid": [round(float(centroids[label_index][0]), 3), round(float(centroids[label_index][1]), 3)],
                "bbox": [x, y, width, height],
                "rotated_bbox": {
                    "center": [round(float(rect[0][0] + x), 3), round(float(rect[0][1] + y), 3)],
                    "size": [round(float(rect[1][0]), 3), round(float(rect[1][1]), 3)],
                    "angle": round(float(rect[2]), 3),
                },
                "contour": [[int(px), int(py)] for px, py in contour_global.tolist()],
                "asset_path": _relative(asset_file, job_dir),
                "mask_path": _relative(mask_file, job_dir),
            }
        )

    overlay = cv2.addWeighted(overlay, 0.72, tint, 0.28, 0)
    overlay_path = stage_dir / "components-overlay.png"
    _write_cv(overlay_path, overlay)
    primitives_path = stage_dir / "primitives.json"
    primitives_path.write_text(json.dumps(primitives, ensure_ascii=False, indent=2), encoding="utf-8")
    groups = auto_group_primitives(primitives, mm_px, float(settings["group_gap_mm"]), foreground.shape[1], foreground.shape[0])
    groups_path = stage_dir / "groups.json"
    groups_path.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = [
        artifact("components", "components-overlay", "连通域标注", overlay_path, job_dir),
        artifact("components", "primitives", "原始组件数据", primitives_path, job_dir, "json"),
        artifact("components", "groups", "自动分组数据", groups_path, job_dir, "json"),
    ]
    return artifacts, primitives, groups


def auto_group_primitives(primitives: list[dict[str, Any]], mm_px: float, gap_mm: float, master_width: int, master_height: int) -> list[dict[str, Any]]:
    if not primitives:
        return []
    parents = list(range(len(primitives)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parents[root_right] = root_left

    polygons = [_contour_to_polygon(item["contour"]) for item in primitives]
    gap_px = gap_mm / max(mm_px, 1e-9)
    for left in range(len(primitives)):
        for right in range(left + 1, len(primitives)):
            if polygons[left].distance(polygons[right]) <= gap_px:
                union(left, right)
    buckets: dict[int, list[str]] = {}
    for index, item in enumerate(primitives):
        buckets.setdefault(find(index), []).append(item["id"])
    by_id = {item["id"]: item for item in primitives}
    groups: list[dict[str, Any]] = []
    for primitive_ids in buckets.values():
        boxes = [by_id[item]["bbox"] for item in primitive_ids]
        x0 = min(item[0] for item in boxes)
        y0 = min(item[1] for item in boxes)
        x1 = max(item[0] + item[2] for item in boxes)
        y1 = max(item[1] + item[3] for item in boxes)
        width, height = x1 - x0, y1 - y0
        groups.append(
            {
                "id": f"g{len(groups) + 1:03d}",
                "primitive_ids": primitive_ids,
                "bbox": [x0, y0, width, height],
                "active": True,
                "rotatable": False,
                "filler": False,
                "max_copies": 2,
                "origin": "auto",
            }
        )
    return groups


def _geometry_polygons(geometry: Any) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type == "MultiPolygon":
        return list(geometry.geoms)
    if geometry.geom_type == "GeometryCollection":
        result: list[Polygon] = []
        for item in geometry.geoms:
            result.extend(_geometry_polygons(item))
        return result
    return []


def run_geometry(job: dict[str, Any], job_dir: Path, settings: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stage_dir = job_dir / "geometry"
    assets_dir = stage_dir / "groups"
    assets_dir.mkdir(parents=True, exist_ok=True)
    foreground = np.array(Image.open(job_dir / "key" / "foreground.png").convert("RGBA"))
    master_height, master_width = foreground.shape[:2]
    mm_px = mm_per_pixel(master_width, master_height, settings)
    primitives = {item["id"]: item for item in job.get("primitives", [])}
    overlay = cv2.cvtColor(foreground[:, :, :3], cv2.COLOR_RGB2BGR)
    geometries: list[dict[str, Any]] = []

    for group in job.get("groups", []):
        if not group.get("active", True):
            continue
        selected = [primitives[item] for item in group["primitive_ids"] if item in primitives]
        if not selected:
            continue
        pixel_polygons = [_contour_to_polygon(item["contour"]) for item in selected]
        visible_px = unary_union([item for item in pixel_polygons if not item.is_empty])
        if visible_px.is_empty:
            continue
        # The asset canvas must cover the complete primitive masks.  Shapely's
        # repaired contour bounds can be smaller than OpenCV's component bbox
        # for self-touching contours, so mask bboxes are authoritative here.
        selected_boxes = [item["bbox"] for item in selected]
        bx0 = max(0, min(int(item[0]) for item in selected_boxes))
        by0 = max(0, min(int(item[1]) for item in selected_boxes))
        bx1 = min(master_width, max(int(item[0] + item[2]) for item in selected_boxes))
        by1 = min(master_height, max(int(item[1] + item[3]) for item in selected_boxes))
        visible_local_px = affinity.translate(visible_px, xoff=-bx0, yoff=-by0)
        visible_mm = affinity.scale(visible_local_px, xfact=mm_px, yfact=mm_px, origin=(0, 0))
        simplify_mm = max(0.0, float(settings["simplify_mm"]))
        visible_mm = visible_mm.simplify(simplify_mm, preserve_topology=True).buffer(0)
        cutline = visible_mm.buffer(float(settings["cut_offset_mm"]), join_style=1).buffer(0)
        occupancy = cutline.buffer(float(settings["spacing_mm"]) / 2.0, join_style=1).buffer(0)
        if occupancy.is_empty:
            continue
        occ_min_x, occ_min_y, occ_max_x, occ_max_y = occupancy.bounds
        occupancy_norm = affinity.translate(occupancy, xoff=-occ_min_x, yoff=-occ_min_y)
        cutline_norm = affinity.translate(cutline, xoff=-occ_min_x, yoff=-occ_min_y)
        visible_norm = affinity.translate(visible_mm, xoff=-occ_min_x, yoff=-occ_min_y)
        # Packing uses a conservative low-vertex polygon. Fine snowflake edges
        # remain available in visible/cutline, while collision checks stay fast
        # and can never place two detailed cutlines closer than requested.
        packing_norm = occupancy_norm.convex_hull.simplify(max(0.5, simplify_mm), preserve_topology=True)

        group_mask = np.zeros((by1 - by0, bx1 - bx0), dtype=np.uint8)
        for primitive in selected:
            mask = np.array(Image.open(job_dir / primitive["mask_path"]).convert("L"))
            px, py, pw, ph = primitive["bbox"]
            ix0, iy0 = px - bx0, py - by0
            dst_x0, dst_y0 = max(0, ix0), max(0, iy0)
            dst_x1 = min(group_mask.shape[1], ix0 + pw, dst_x0 + mask.shape[1])
            dst_y1 = min(group_mask.shape[0], iy0 + ph, dst_y0 + mask.shape[0])
            if dst_x1 <= dst_x0 or dst_y1 <= dst_y0:
                continue
            src_x0, src_y0 = dst_x0 - ix0, dst_y0 - iy0
            src_x1, src_y1 = src_x0 + (dst_x1 - dst_x0), src_y0 + (dst_y1 - dst_y0)
            current = group_mask[dst_y0:dst_y1, dst_x0:dst_x1]
            np.maximum(current, mask[src_y0:src_y1, src_x0:src_x1], out=current)
        crop = foreground[by0:by1, bx0:bx1].copy()
        crop[:, :, 3] = group_mask
        asset_path = assets_dir / f"{group['id']}.png"
        Image.fromarray(crop, "RGBA").save(asset_path)

        cv_contours = [np.array(poly.exterior.coords, dtype=np.int32).reshape((-1, 1, 2)) for poly in _geometry_polygons(visible_px)]
        if cv_contours:
            cv2.drawContours(overlay, cv_contours, -1, (0, 220, 255), 2)
        cv2.rectangle(overlay, (bx0, by0), (bx1, by1), (255, 100, 30), 2)
        cv2.putText(overlay, group["id"], (bx0, max(14, by0 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 100, 30), 1, cv2.LINE_AA)

        min_rect = visible_mm.minimum_rotated_rectangle
        geometries.append(
            {
                "group_id": group["id"],
                "primitive_ids": list(group["primitive_ids"]),
                "asset_path": _relative(asset_path, job_dir),
                "source_bbox_px": [bx0, by0, bx1 - bx0, by1 - by0],
                "asset_size_mm": [round((bx1 - bx0) * mm_px, 4), round((by1 - by0) * mm_px, 4)],
                "visible_offset_mm": [round(-occ_min_x, 4), round(-occ_min_y, 4)],
                "visible": mapping(visible_norm),
                "cutline": mapping(cutline_norm),
                "occupancy": mapping(occupancy_norm),
                "packing": mapping(packing_norm),
                "occupancy_bounds_mm": [round(value, 4) for value in occupancy_norm.bounds],
                "area_mm2": round(float(visible_mm.area), 4),
                "cutline_area_mm2": round(float(cutline.area), 4),
                "minimum_rotated_rectangle": mapping(min_rect),
                "origin": group.get("origin", "auto"),
                "semantic": group.get("semantic"),
                "rotatable": bool(group.get("rotatable", False)),
                "filler": bool(group.get("filler", False)),
                "max_copies": max(0, int(group.get("max_copies", 2))),
            }
        )

    overlay_path = stage_dir / "groups-contours-overlay.png"
    _write_cv(overlay_path, overlay)
    geometry_path = stage_dir / "geometry.json"
    geometry_path.write_text(json.dumps(geometries, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = [
        artifact("geometry", "geometry-overlay", "分组、轮廓与外接框", overlay_path, job_dir),
        artifact("geometry", "geometry-json", "排样几何数据", geometry_path, job_dir, "json"),
    ]
    return artifacts, geometries


@dataclass
class PackedItem:
    group_id: str
    page: int
    x: float
    y: float
    angle: int
    copy_index: int
    occupancy: Any
    cutline: Any
    source: dict[str, Any]


def _normalized_rotation(geometry: Any, angle: int) -> tuple[Any, float, float]:
    rotated = affinity.rotate(geometry, angle, origin=(0, 0), use_radians=False)
    min_x, min_y, _, _ = rotated.bounds
    normalized = affinity.translate(rotated, xoff=-min_x, yoff=-min_y)
    return normalized, min_x, min_y


def _scaled_layout_source(source: dict[str, Any], settings: dict[str, Any], layout_scale: float) -> dict[str, Any]:
    """Scale artwork while rebuilding fixed-width cut and spacing buffers."""
    visible_normalized = shape(source["visible"])
    original_offset = source.get("visible_offset_mm", [0.0, 0.0])
    visible_local = affinity.translate(
        visible_normalized,
        xoff=-float(original_offset[0]),
        yoff=-float(original_offset[1]),
    )
    visible_scaled = affinity.scale(visible_local, xfact=layout_scale, yfact=layout_scale, origin=(0, 0))
    cutline = visible_scaled.buffer(float(settings["cut_offset_mm"]), join_style=1).buffer(0)
    occupancy = cutline.buffer(float(settings["spacing_mm"]) / 2.0, join_style=1).buffer(0)
    min_x, min_y, _, _ = occupancy.bounds
    occupancy_norm = affinity.translate(occupancy, xoff=-min_x, yoff=-min_y)
    cutline_norm = affinity.translate(cutline, xoff=-min_x, yoff=-min_y)
    visible_norm = affinity.translate(visible_scaled, xoff=-min_x, yoff=-min_y)
    simplify_mm = max(0.5, float(settings.get("simplify_mm", 0.3)))
    scaled = dict(source)
    scaled.update(
        {
            "visible": mapping(visible_norm),
            "cutline": mapping(cutline_norm),
            "occupancy": mapping(occupancy_norm),
            "packing": mapping(occupancy_norm.convex_hull.simplify(simplify_mm, preserve_topology=True)),
            "occupancy_bounds_mm": [round(value, 4) for value in occupancy_norm.bounds],
            "cutline_area_mm2": round(float(cutline_norm.area), 4),
            "asset_size_mm": [float(value) * layout_scale for value in source["asset_size_mm"]],
            "asset_offset_mm": [round(-min_x, 4), round(-min_y, 4)],
            "layout_scale": layout_scale,
        }
    )
    return scaled


def _split_free_rectangles(
    placed: list[PackedItem], margin: float, sheet_width: float, sheet_height: float
) -> list[tuple[float, float, float, float]]:
    free = [(margin, margin, sheet_width - margin, sheet_height - margin)]
    for item in placed:
        ix0, iy0, ix1, iy1 = item.occupancy.bounds
        next_free: list[tuple[float, float, float, float]] = []
        for x0, y0, x1, y1 in free:
            if ix1 <= x0 or ix0 >= x1 or iy1 <= y0 or iy0 >= y1:
                next_free.append((x0, y0, x1, y1))
                continue
            if ix0 > x0:
                next_free.append((x0, y0, ix0, y1))
            if ix1 < x1:
                next_free.append((ix1, y0, x1, y1))
            if iy0 > y0:
                next_free.append((x0, y0, x1, iy0))
            if iy1 < y1:
                next_free.append((x0, iy1, x1, y1))
        cleaned = [rect for rect in next_free if rect[2] - rect[0] > 0.05 and rect[3] - rect[1] > 0.05]
        free = [
            rect
            for index, rect in enumerate(cleaned)
            if not any(
                index != other_index
                and rect[0] >= other[0] - 1e-6
                and rect[1] >= other[1] - 1e-6
                and rect[2] <= other[2] + 1e-6
                and rect[3] <= other[3] + 1e-6
                for other_index, other in enumerate(cleaned)
            )
        ]
    return free


def _candidate_positions(
    placed: list[PackedItem],
    margin: float,
    sheet_width: float,
    sheet_height: float,
    item_width: float,
    item_height: float,
    strategy: str,
) -> list[tuple[float, float]]:
    xs = {round(margin, 4), round(max(margin, sheet_width - margin - item_width), 4)}
    ys = {round(margin, 4), round(max(margin, sheet_height - margin - item_height), 4)}
    for item in placed:
        bounds = item.occupancy.bounds
        xs.add(round(bounds[2], 4))
        ys.add(round(bounds[3], 4))
        xs.add(round(max(margin, bounds[0]), 4))
        ys.add(round(max(margin, bounds[1]), 4))
        xs.add(round(max(margin, bounds[0] - item_width), 4))
        ys.add(round(max(margin, bounds[1] - item_height), 4))
        xs.add(round(max(margin, bounds[2] - item_width), 4))
        ys.add(round(max(margin, bounds[3] - item_height), 4))
    max_x = sheet_width - margin - item_width
    max_y = sheet_height - margin - item_height
    xs = sorted(value for value in xs if margin - 1e-6 <= value <= max_x + 1e-6)
    ys = sorted(value for value in ys if margin - 1e-6 <= value <= max_y + 1e-6)
    anchors = [(x, y) for y in ys for x in xs]
    maxrect_positions: list[tuple[float, float]] = [(round(margin, 4), round(margin, 4))]
    for item in placed:
        x0, y0, x1, y1 = item.occupancy.bounds
        maxrect_positions.extend(
            [
                (round(x1, 4), round(y0, 4)),
                (round(x0, 4), round(y1, 4)),
                (round(max(margin, x0 - item_width), 4), round(y0, 4)),
                (round(x0, 4), round(max(margin, y0 - item_height), 4)),
            ]
        )
    if strategy == "tidy_rows":
        return list(dict.fromkeys(maxrect_positions + anchors[: min(48, len(anchors))]))
    if strategy in {"maxrects", "hybrid_fast"}:
        return list(dict.fromkeys(maxrect_positions + anchors[: min(48, len(anchors))]))
    if len(anchors) > 12:
        stride = math.ceil(len(anchors) / 12)
        anchors = anchors[::stride]
    grid_positions: list[tuple[float, float]] = []
    if placed:
        extent_x = min(max_x, max(item.occupancy.bounds[2] for item in placed))
        extent_y = min(max_y, max(item.occupancy.bounds[3] for item in placed))
        y = margin
        while y <= extent_y + 1e-6:
            x = margin
            while x <= extent_x + 1e-6:
                grid_positions.append((round(x, 4), round(y, 4)))
                x += 2.0
            y += 2.0
        if len(grid_positions) > 6:
            stride = math.ceil(len(grid_positions) / 6)
            grid_positions = grid_positions[::stride]
    return list(dict.fromkeys(maxrect_positions + anchors + grid_positions))

    # Legacy sparse-anchor implementation retained below for source-history
    # readability; the compact return above is authoritative.
    xs = sorted(value for value in xs if value < sheet_width - margin)
    ys = sorted(value for value in ys if value < sheet_height - margin)
    positions = [(x, y) for y in ys for x in xs]
    if len(positions) <= max_positions:
        return positions

    # A full Cartesian product grows quadratically and makes a 40–80 element
    # master unnecessarily slow. Keep the strongest bottom-left anchors and a
    # deterministic sample of the rest so the alternative strategies still
    # see positions across the page.
    head_count = max_positions * 2 // 3
    sampled_count = max_positions - head_count
    head = positions[:head_count]
    tail = positions[head_count:]
    stride = max(1, len(tail) // sampled_count)
    sampled = tail[::stride][:sampled_count]
    return list(dict.fromkeys(head + sampled))


def _has_positive_overlap(candidate: Any, placed: list[PackedItem]) -> bool:
    candidate_bounds = candidate.bounds
    for item in placed:
        other_bounds = item.occupancy.bounds
        if (
            candidate_bounds[2] <= other_bounds[0] + 1e-6
            or other_bounds[2] <= candidate_bounds[0] + 1e-6
            or candidate_bounds[3] <= other_bounds[1] + 1e-6
            or other_bounds[3] <= candidate_bounds[1] + 1e-6
        ):
            continue
        if candidate.relate_pattern(item.occupancy, "T********"):
            return True
    return False


def _placement_objective(strategy: str, candidate: Any, placed: list[PackedItem], sheet_width: float, sheet_height: float, rng: random.Random) -> float:
    min_x, min_y, max_x, max_y = candidate.bounds
    if strategy == "tidy_rows":
        return min_y * 10000.0 + min_x
    used_max_x = max([item.occupancy.bounds[2] for item in placed] + [max_x])
    used_max_y = max([item.occupancy.bounds[3] for item in placed] + [max_y])
    extent_cost = used_max_x * used_max_y
    alignment_bonus = 0.0
    for item in placed:
        bx0, by0, bx1, by1 = item.occupancy.bounds
        if min(abs(min_x - bx0), abs(max_x - bx1)) <= 2.0:
            alignment_bonus += 40.0
        if min(abs(min_y - by0), abs(max_y - by1)) <= 2.0:
            alignment_bonus += 40.0
    if strategy == "maxrects":
        return extent_cost + min_y * 4.0 + min_x - alignment_bonus
    jitter = rng.random() * max(sheet_width, sheet_height) * (0.02 if strategy == "hybrid_search" else 0.0)
    return extent_cost + min_y * 2.0 + min_x - alignment_bonus + jitter


def _place_one(
    source: dict[str, Any],
    pages: list[list[PackedItem]],
    settings: dict[str, Any],
    strategy: str,
    rng: random.Random,
    copy_index: int = 0,
    allow_new_page: bool = True,
) -> PackedItem | None:
    sheet_width = float(settings["sheet_width_mm"])
    sheet_height = float(settings["sheet_height_mm"])
    margin = float(settings["sheet_margin_mm"])
    collision_geometry = source["occupancy"] if strategy in {"hybrid_fill", "hybrid_search", "hybrid_fast"} else source.get("packing", source["occupancy"])
    base_occupancy = shape(collision_geometry)
    base_cutline = shape(source["cutline"])
    angles = [0, 90] if source.get("rotatable", False) else [0]
    best: tuple[float, int, float, float, int, Any, Any] | None = None
    for page_index, placed in enumerate(pages):
        for angle in angles:
            rotated_occ, occ_shift_x, occ_shift_y = _normalized_rotation(base_occupancy, angle)
            rotated_cut = affinity.rotate(base_cutline, angle, origin=(0, 0), use_radians=False)
            rotated_cut = affinity.translate(rotated_cut, xoff=-occ_shift_x, yoff=-occ_shift_y)
            width = rotated_occ.bounds[2] - rotated_occ.bounds[0]
            height = rotated_occ.bounds[3] - rotated_occ.bounds[1]
            positions = _candidate_positions(
                placed, margin, sheet_width, sheet_height, width, height, strategy
            )
            for x, y in positions:
                candidate = affinity.translate(rotated_occ, xoff=x, yoff=y)
                bounds = candidate.bounds
                if bounds[0] < margin - 1e-6 or bounds[1] < margin - 1e-6:
                    continue
                if bounds[2] > sheet_width - margin + 1e-6 or bounds[3] > sheet_height - margin + 1e-6:
                    continue
                if _has_positive_overlap(candidate, placed):
                    continue
                objective = _placement_objective(strategy, candidate, placed, sheet_width, sheet_height, rng)
                if best is None or objective < best[0]:
                    cutline = affinity.translate(rotated_cut, xoff=x, yoff=y)
                    best = (objective, page_index, x, y, angle, candidate, cutline)
    if best is None and allow_new_page:
        pages.append([])
        new_page_index = len(pages) - 1
        placed = pages[new_page_index]
        for angle in angles:
            rotated_occ, occ_shift_x, occ_shift_y = _normalized_rotation(base_occupancy, angle)
            rotated_cut = affinity.rotate(base_cutline, angle, origin=(0, 0), use_radians=False)
            rotated_cut = affinity.translate(rotated_cut, xoff=-occ_shift_x, yoff=-occ_shift_y)
            candidate = affinity.translate(rotated_occ, xoff=margin, yoff=margin)
            bounds = candidate.bounds
            if bounds[2] <= sheet_width - margin + 1e-6 and bounds[3] <= sheet_height - margin + 1e-6:
                best = (0.0, new_page_index, margin, margin, angle, candidate, affinity.translate(rotated_cut, xoff=margin, yoff=margin))
                break
    if best is None:
        return None
    _, page_index, x, y, angle, occupancy, cutline = best
    packed = PackedItem(source["group_id"], page_index, x, y, angle, copy_index, occupancy, cutline, source)
    pages[page_index].append(packed)
    return packed


def _sort_items(items: list[dict[str, Any]], strategy: str, rng: random.Random) -> list[dict[str, Any]]:
    if strategy == "tidy_rows":
        return sorted(items, key=lambda item: max(item["occupancy_bounds_mm"][2], item["occupancy_bounds_mm"][3]), reverse=True)
    if strategy == "hybrid_search":
        ordered = sorted(items, key=lambda item: item["cutline_area_mm2"], reverse=True)
        result: list[dict[str, Any]] = []
        for start in range(0, len(ordered), 4):
            bucket = ordered[start : start + 4]
            rng.shuffle(bucket)
            result.extend(bucket)
        return result
    return sorted(items, key=lambda item: item["cutline_area_mm2"], reverse=True)


def _translate_packed(item: PackedItem, dx: float, dy: float) -> None:
    item.x += dx
    item.y += dy
    item.occupancy = affinity.translate(item.occupancy, xoff=dx, yoff=dy)
    item.cutline = affinity.translate(item.cutline, xoff=dx, yoff=dy)


def _compact_pages(pages: list[list[PackedItem]], settings: dict[str, Any]) -> None:
    margin = float(settings["sheet_margin_mm"])
    for placed in pages:
        if len(placed) < 2:
            continue
        for _ in range(3):
            for item in sorted(placed, key=lambda value: (value.occupancy.bounds[1], value.occupancy.bounds[0])):
                others = [other for other in placed if other is not item]
                for axis in ("x", "y"):
                    bounds = item.occupancy.bounds
                    current = bounds[0] if axis == "x" else bounds[1]
                    size = (bounds[2] - bounds[0]) if axis == "x" else (bounds[3] - bounds[1])
                    targets = {margin}
                    for other in others:
                        other_bounds = other.occupancy.bounds
                        start = other_bounds[0] if axis == "x" else other_bounds[1]
                        end = other_bounds[2] if axis == "x" else other_bounds[3]
                        targets.add(end)
                        targets.add(max(margin, start - size))
                    for target in sorted(value for value in targets if value < current - 1e-6):
                        shift = target - current
                        candidate = affinity.translate(
                            item.occupancy,
                            xoff=shift if axis == "x" else 0.0,
                            yoff=shift if axis == "y" else 0.0,
                        )
                        if _has_positive_overlap(candidate, others):
                            continue
                        _translate_packed(item, shift if axis == "x" else 0.0, shift if axis == "y" else 0.0)
                        break


def _largest_empty_rectangle(mask: np.ndarray) -> int:
    if mask.size == 0:
        return 0
    heights = [0] * mask.shape[1]
    best = 0
    for row in mask:
        for column, occupied in enumerate(row):
            heights[column] = 0 if occupied else heights[column] + 1
        stack: list[int] = []
        for index in range(len(heights) + 1):
            height = heights[index] if index < len(heights) else 0
            while stack and heights[stack[-1]] > height:
                top = stack.pop()
                width = index if not stack else index - stack[-1] - 1
                best = max(best, heights[top] * width)
            stack.append(index)
    return best


def _page_shape_metrics(placed: list[PackedItem], settings: dict[str, Any]) -> tuple[float, float, float]:
    if not placed:
        return 0.0, 1.0, 0.0
    margin = float(settings["sheet_margin_mm"])
    printable_width = float(settings["sheet_width_mm"]) - 2 * margin
    printable_height = float(settings["sheet_height_mm"]) - 2 * margin
    resolution = 2.0
    grid_width = max(1, math.ceil(printable_width / resolution))
    grid_height = max(1, math.ceil(printable_height / resolution))
    raster = Image.new("L", (grid_width, grid_height), 0)
    draw = ImageDraw.Draw(raster)
    for item in placed:
        for polygon in _geometry_polygons(item.occupancy):
            points = [
                (
                    round((x - margin) / resolution),
                    round((y - margin) / resolution),
                )
                for x, y in polygon.exterior.coords
            ]
            if len(points) >= 3:
                draw.polygon(points, fill=1)
    occupied = np.array(raster, dtype=bool)
    ys, xs = np.where(occupied)
    if not len(xs):
        return 0.0, 1.0, 0.0
    cropped = occupied[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    extent_fill = float(cropped.mean())
    largest_empty_cells = _largest_empty_rectangle(cropped)
    largest_void_ratio = largest_empty_cells / max(grid_width * grid_height, 1)
    internal_void_ratio = largest_empty_cells / max(cropped.size, 1)
    compactness = max(0.0, min(1.0, 0.7 * extent_fill + 0.3 * (1.0 - internal_void_ratio)))

    bounds = [item.occupancy.bounds for item in placed]
    aligned_axes = 0
    tolerance = 2.0
    for index, current in enumerate(bounds):
        x_aligned = abs(current[0] - margin) <= tolerance or any(
            min(abs(current[0] - other[0]), abs(current[2] - other[2])) <= tolerance
            for other_index, other in enumerate(bounds)
            if other_index != index
        )
        y_aligned = abs(current[1] - margin) <= tolerance or any(
            min(abs(current[1] - other[1]), abs(current[3] - other[3])) <= tolerance
            for other_index, other in enumerate(bounds)
            if other_index != index
        )
        aligned_axes += int(x_aligned) + int(y_aligned)
    alignment = aligned_axes / max(2 * len(bounds), 1)
    return compactness, largest_void_ratio, alignment


def _layout_shape_metrics(pages: list[list[PackedItem]], settings: dict[str, Any]) -> tuple[float, float, float]:
    metrics = [_page_shape_metrics(page, settings) for page in pages if page]
    if not metrics:
        return 0.0, 1.0, 0.0
    return (
        float(np.mean([value[0] for value in metrics])),
        float(max(value[1] for value in metrics)),
        float(np.mean([value[2] for value in metrics])),
    )


def _balance_score(pages: list[list[PackedItem]], settings: dict[str, Any]) -> float:
    sheet_width = float(settings["sheet_width_mm"])
    sheet_height = float(settings["sheet_height_mm"])
    margin = float(settings["sheet_margin_mm"])
    page_scores: list[float] = []
    for placed in pages:
        if not placed:
            continue
        cell_values: list[float] = []
        for row in range(3):
            for column in range(3):
                x0 = margin + column * (sheet_width - 2 * margin) / 3.0
                x1 = margin + (column + 1) * (sheet_width - 2 * margin) / 3.0
                y0 = margin + row * (sheet_height - 2 * margin) / 3.0
                y1 = margin + (row + 1) * (sheet_height - 2 * margin) / 3.0
                cell = box(x0, y0, x1, y1)
                used = sum(item.cutline.intersection(cell).area for item in placed)
                cell_values.append(used / max(cell.area, 1e-9))
        mean = float(np.mean(cell_values))
        cv = float(np.std(cell_values)) / max(mean, 1e-6)
        grid_score = 1.0 / (1.0 + cv)
        total_area = sum(item.cutline.area for item in placed)
        cx = sum(item.cutline.centroid.x * item.cutline.area for item in placed) / max(total_area, 1e-9)
        cy = sum(item.cutline.centroid.y * item.cutline.area for item in placed) / max(total_area, 1e-9)
        offset = math.hypot(cx - sheet_width / 2.0, cy - sheet_height / 2.0)
        max_offset = math.hypot(sheet_width / 2.0, sheet_height / 2.0)
        center_score = max(0.0, 1.0 - offset / max_offset)
        page_scores.append(0.6 * grid_score + 0.4 * center_score)
    return float(np.mean(page_scores)) if page_scores else 0.0


def _pack_at_scale(
    items: list[dict[str, Any]],
    settings: dict[str, Any],
    strategy: str,
    seed: int,
    layout_scale: float,
    order_variant: int = 0,
    compact: bool = True,
) -> dict[str, Any]:
    rng = random.Random(seed + order_variant * 997)
    scaled_items = [_scaled_layout_source(item, settings, layout_scale) for item in items]
    ordered = _sort_items(scaled_items, strategy, rng)
    placement_strategy = "hybrid_fast" if strategy == "hybrid_search" and order_variant > 0 else strategy
    pages: list[list[PackedItem]] = []
    oversized: list[str] = []
    for source in ordered:
        placed = _place_one(source, pages, settings, placement_strategy, rng)
        if placed is None:
            oversized.append(source["group_id"])
    if oversized:
        raise ValueError("以下贴纸组大于可打印区域，请减小安装尺寸或取消分组：" + ", ".join(oversized))

    if compact:
        _compact_pages(pages, settings)
    for source in ordered:
        if not source.get("filler"):
            continue
        for copy_index in range(1, max(0, int(source.get("max_copies", 0))) + 1):
            _place_one(source, pages, settings, placement_strategy, rng, copy_index=copy_index, allow_new_page=False)
    if compact:
        _compact_pages(pages, settings)

    printable_area = (float(settings["sheet_width_mm"]) - 2 * float(settings["sheet_margin_mm"])) * (
        float(settings["sheet_height_mm"]) - 2 * float(settings["sheet_margin_mm"])
    )
    used_pages = [page for page in pages if page]
    cutline_area = sum(item.cutline.area for page in used_pages for item in page)
    utilization = cutline_area / max(printable_area * len(used_pages), 1e-9)
    balance = _balance_score(used_pages, settings)
    compactness, largest_void_ratio, alignment = _layout_shape_metrics(used_pages, settings)
    compactness_weight = float(settings["compactness_weight"])
    alignment_weight = float(settings["alignment_weight"])
    balance_weight = float(settings["balance_weight"])
    total_weight = max(compactness_weight + alignment_weight + balance_weight, 1e-9)
    score = (
        compactness * compactness_weight
        + alignment * alignment_weight
        + balance * balance_weight
    ) / total_weight
    placements = [
        {
            "group_id": item.group_id,
            "page": page_index,
            "x_mm": round(item.x, 4),
            "y_mm": round(item.y, 4),
            "angle": item.angle,
            "copy_index": item.copy_index,
            "occupancy": mapping(item.occupancy),
            "cutline": mapping(item.cutline),
            "asset_size_mm": [round(float(value), 4) for value in item.source["asset_size_mm"]],
            "asset_offset_mm": [round(float(value), 4) for value in item.source["asset_offset_mm"]],
            "occupancy_size_mm": [
                round(float(item.source["occupancy_bounds_mm"][2] - item.source["occupancy_bounds_mm"][0]), 4),
                round(float(item.source["occupancy_bounds_mm"][3] - item.source["occupancy_bounds_mm"][1]), 4),
            ],
        }
        for page_index, page in enumerate(used_pages)
        for item in page
    ]
    return {
        "strategy": strategy,
        "packing_strategy": strategy,
        "seed": seed,
        "order_variant": order_variant,
        "layout_scale": round(layout_scale, 4),
        "page_count": len(used_pages),
        "utilization": round(utilization, 6),
        "compactness": round(compactness, 6),
        "alignment": round(alignment, 6),
        "largest_void_ratio": round(largest_void_ratio, 6),
        "balance": round(balance, 6),
        "score": round(score, 6),
        "placements": placements,
    }


def _layout_scales(settings: dict[str, Any]) -> list[float]:
    if not bool(settings.get("auto_shrink_enabled", True)):
        return [1.0]
    minimum = 1.0 - float(settings.get("max_shrink_ratio", 0.08))
    scales = [1.0]
    value = 0.98
    while value >= minimum - 1e-9:
        scales.append(round(max(minimum, value), 4))
        value -= 0.02
    if scales[-1] > minimum + 1e-9:
        scales.append(round(minimum, 4))
    return list(dict.fromkeys(scales))


def _candidate_rank(result: dict[str, Any]) -> tuple[int, float, float, float]:
    return (
        int(result["page_count"]),
        -float(result["layout_scale"]),
        -float(result["score"]),
        float(result["largest_void_ratio"]),
    )


def _quick_shelf_rank(
    items: list[dict[str, Any]], settings: dict[str, Any], seed: int, order_variant: int
) -> tuple[int, float, int]:
    rng = random.Random(seed + order_variant * 997)
    ordered = _sort_items(items, "hybrid_search", rng)
    margin = float(settings["sheet_margin_mm"])
    max_width = float(settings["sheet_width_mm"]) - 2 * margin
    max_height = float(settings["sheet_height_mm"]) - 2 * margin
    pages: list[list[list[float]]] = []
    extent_area = 0.0
    for source in ordered:
        width = float(source["occupancy_bounds_mm"][2] - source["occupancy_bounds_mm"][0])
        height = float(source["occupancy_bounds_mm"][3] - source["occupancy_bounds_mm"][1])
        best: tuple[float, int, int] | None = None
        for page_index, shelves in enumerate(pages):
            for shelf_index, shelf in enumerate(shelves):
                y, shelf_height, used_width = shelf
                if height <= shelf_height + 1e-6 and used_width + width <= max_width + 1e-6:
                    waste = (shelf_height - height) * width + (max_width - used_width - width)
                    if best is None or waste < best[0]:
                        best = (waste, page_index, shelf_index)
            used_height = sum(shelf[1] for shelf in shelves)
            if used_height + height <= max_height + 1e-6:
                waste = max_width - width
                if best is None or waste < best[0]:
                    best = (waste, page_index, len(shelves))
        if best is None:
            pages.append([[0.0, height, width]])
            continue
        _, page_index, shelf_index = best
        if shelf_index == len(pages[page_index]):
            pages[page_index].append([0.0, height, width])
        else:
            pages[page_index][shelf_index][2] += width
    for shelves in pages:
        extent_area += max((shelf[2] for shelf in shelves), default=0.0) * sum(shelf[1] for shelf in shelves)
    return len(pages), extent_area, order_variant


def _pack_candidate(items: list[dict[str, Any]], settings: dict[str, Any], strategy: str, seed: int) -> dict[str, Any]:
    if strategy in {"hybrid_fill", "hybrid_search"}:
        scale_baselines = [
            _pack_at_scale(items, settings, "maxrects", seed, layout_scale, 0, compact=False)
            for layout_scale in _layout_scales(settings)
        ]
        best_pages = min(result["page_count"] for result in scale_baselines)
        chosen_scale = max(result["layout_scale"] for result in scale_baselines if result["page_count"] == best_pages)
        if strategy == "hybrid_fill":
            return _pack_at_scale(items, settings, strategy, seed, chosen_scale, 0, compact=True)
        scaled_items = [_scaled_layout_source(item, settings, chosen_scale) for item in items]
        quick_ranks = sorted(
            (_quick_shelf_rank(scaled_items, settings, seed, variant) for variant in range(48)),
            key=lambda value: (value[0], value[1], value[2]),
        )
        return _pack_at_scale(
            items,
            settings,
            strategy,
            seed,
            chosen_scale,
            int(quick_ranks[0][2]),
            compact=True,
        )

    baselines: list[dict[str, Any]] = []
    last_error: Exception | None = None
    for layout_scale in _layout_scales(settings):
        try:
            baselines.append(_pack_at_scale(items, settings, strategy, seed, layout_scale, 0, compact=strategy != "hybrid_search"))
        except ValueError as error:
            last_error = error
    if not baselines:
        if last_error:
            raise last_error
        raise ValueError("No layout candidates were generated")

    best_pages = min(result["page_count"] for result in baselines)
    chosen_scale = max(result["layout_scale"] for result in baselines if result["page_count"] == best_pages)
    eligible = [
        result
        for result in baselines
        if result["page_count"] == best_pages and result["layout_scale"] == chosen_scale
    ]
    return min(eligible, key=_candidate_rank)


def _timed_pack_candidate(items: list[dict[str, Any]], settings: dict[str, Any], strategy: str, seed: int) -> dict[str, Any]:
    started = time.perf_counter()
    result = _pack_candidate(items, settings, strategy, seed)
    result["packing_seconds"] = round(time.perf_counter() - started, 4)
    return result


def _rgba_tile_for_geometry(source: dict[str, Any], placement: dict[str, Any], job_dir: Path, dpi: int) -> Image.Image:
    scale = dpi / 25.4
    width_mm = max(float(placement["occupancy_size_mm"][0]), 0.1)
    height_mm = max(float(placement["occupancy_size_mm"][1]), 0.1)
    canvas = Image.new("RGBA", (max(1, round(width_mm * scale)), max(1, round(height_mm * scale))), (0, 0, 0, 0))
    asset = Image.open(job_dir / source["asset_path"]).convert("RGBA")
    asset_width = max(1, round(float(placement["asset_size_mm"][0]) * scale))
    asset_height = max(1, round(float(placement["asset_size_mm"][1]) * scale))
    asset = asset.resize((asset_width, asset_height), Image.Resampling.LANCZOS)
    offset_x = round(float(placement["asset_offset_mm"][0]) * scale)
    offset_y = round(float(placement["asset_offset_mm"][1]) * scale)
    canvas.alpha_composite(asset, (offset_x, offset_y))
    return canvas


def render_candidate(
    candidate: dict[str, Any],
    geometries: list[dict[str, Any]],
    job_dir: Path,
    output_dir: Path,
    dpi: int,
    final: bool = False,
    tile_cache: dict[tuple[str, int, float], Image.Image] | None = None,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    by_id = {item["group_id"]: item for item in geometries}
    scale = dpi / 25.4
    width_px = round(float(candidate["settings"]["sheet_width_mm"]) * scale)
    height_px = round(float(candidate["settings"]["sheet_height_mm"]) * scale)
    page_paths: list[Path] = []
    for page_index in range(candidate["page_count"]):
        sheet = Image.new("RGBA", (width_px, height_px), (255, 255, 255, 0 if final else 255))
        draw = ImageDraw.Draw(sheet)
        margin_px = round(float(candidate["settings"]["sheet_margin_mm"]) * scale)
        draw.rectangle((margin_px, margin_px, width_px - margin_px, height_px - margin_px), outline=(45, 94, 110, 150), width=max(1, round(scale * 0.3)))
        for placement in [item for item in candidate["placements"] if item["page"] == page_index]:
            source = by_id[placement["group_id"]]
            cache_key = (source["group_id"], dpi, float(candidate.get("layout_scale", 1.0)))
            if tile_cache is not None and cache_key in tile_cache:
                tile = tile_cache[cache_key]
            else:
                tile = _rgba_tile_for_geometry(source, placement, job_dir, dpi)
                if tile_cache is not None:
                    tile_cache[cache_key] = tile
            if placement["angle"]:
                tile = tile.rotate(-int(placement["angle"]), expand=True, resample=Image.Resampling.BICUBIC)
            x_px = round(float(placement["x_mm"]) * scale)
            y_px = round(float(placement["y_mm"]) * scale)
            sheet.alpha_composite(tile, (x_px, y_px))
            if not final:
                polygon = shape(placement["occupancy"])
                for poly in _geometry_polygons(polygon):
                    points = [(round(x * scale), round(y * scale)) for x, y in poly.exterior.coords]
                    draw.line(points, fill=(255, 91, 71, 160), width=max(1, round(scale * 0.35)), joint="curve")
        suffix = "png" if final else "jpg"
        path = output_dir / f"sheet-{page_index + 1:02d}.{suffix}"
        if final:
            sheet.save(path)
        else:
            background = Image.new("RGB", sheet.size, (235, 246, 247))
            background.paste(sheet, mask=sheet.getchannel("A"))
            background.save(path, quality=92)
        page_paths.append(path)
    return page_paths


def make_contact_sheet(paths: list[Path], output_path: Path) -> None:
    images = [Image.open(path).convert("RGB") for path in paths]
    if not images:
        return
    thumb_width = 480
    thumbs = []
    for image in images:
        ratio = thumb_width / image.width
        thumbs.append(image.resize((thumb_width, max(1, round(image.height * ratio))), Image.Resampling.LANCZOS))
    gap = 20
    columns = 2 if len(thumbs) > 1 else 1
    rows = math.ceil(len(thumbs) / columns)
    cell_height = max(item.height for item in thumbs)
    canvas = Image.new("RGB", (columns * thumb_width + (columns + 1) * gap, rows * cell_height + (rows + 1) * gap), "white")
    for index, thumb in enumerate(thumbs):
        x = gap + (index % columns) * (thumb_width + gap)
        y = gap + (index // columns) * (cell_height + gap)
        canvas.paste(thumb, (x, y))
    canvas.save(output_path, quality=92)


def run_layout(job: dict[str, Any], job_dir: Path, settings: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    stage_dir = job_dir / "layout"
    stage_dir.mkdir(parents=True, exist_ok=True)
    geometries = job.get("geometry", [])
    if not geometries:
        raise ValueError("没有可排版的贴纸组")
    strategies = ["tidy_rows", "maxrects", "hybrid_fill", "hybrid_search"]
    seeds = [11, 23, 37, 53]
    candidates: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    # Each heuristic is independent and CPU-heavy. Separate processes avoid the
    # Python GIL and keep four-candidate latency close to one candidate's cost.
    with concurrent.futures.ProcessPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_timed_pack_candidate, geometries, settings, strategy, seed)
            for strategy, seed in zip(strategies, seeds)
        ]
        packed_candidates = [future.result() for future in futures]

    preview_tile_cache: dict[tuple[str, int, float], Image.Image] = {}
    for index, candidate in enumerate(packed_candidates, start=1):
        strategy = strategies[index - 1]
        candidate_id = f"candidate-{index}"
        candidate["id"] = candidate_id
        candidate["settings"] = {
            "sheet_width_mm": float(settings["sheet_width_mm"]),
            "sheet_height_mm": float(settings["sheet_height_mm"]),
            "sheet_margin_mm": float(settings["sheet_margin_mm"]),
        }
        candidate_dir = stage_dir / candidate_id
        paths = render_candidate(
            candidate,
            geometries,
            job_dir,
            candidate_dir,
            int(settings["preview_dpi"]),
            final=False,
            tile_cache=preview_tile_cache,
        )
        candidate["preview_paths"] = [_relative(path, job_dir) for path in paths]
        contact_path = candidate_dir / "contact-sheet.jpg"
        make_contact_sheet(paths, contact_path)
        candidate["contact_sheet_path"] = _relative(contact_path, job_dir)
        layout_path = candidate_dir / "layout.json"
        layout_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts.append(artifact("layout", f"{candidate_id}-contact", f"候选 {index} · {strategy}", contact_path, job_dir))
        artifacts.append(artifact("layout", f"{candidate_id}-json", f"候选 {index} 布局数据", layout_path, job_dir, "json"))
        candidates.append(candidate)

    selected = min(candidates, key=_candidate_rank)
    final_outputs = render_selected_outputs(selected, geometries, job_dir, settings)
    summary_path = stage_dir / "candidates.json"
    summary_path.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts.append(artifact("layout", "candidates-json", "四候选评分数据", summary_path, job_dir, "json"))
    artifacts.extend(selected_output_artifacts(final_outputs, job_dir, "自动选中方案"))
    return artifacts, candidates, selected["id"]


def _remove_stale_selected_outputs(final_dir: Path) -> None:
    """Remove generated Sheet files so a newly selected shorter plan has no stale pages."""
    for directory, pattern in (
        (final_dir / "transparent", "sheet-*.png"),
        (final_dir / "white", "sheet-*.jpg"),
        (final_dir / "pdf", "sheet-*.pdf"),
    ):
        if directory.exists():
            for path in directory.glob(pattern):
                path.unlink(missing_ok=True)
    (final_dir / "pdf" / "print-sheets.pdf").unlink(missing_ok=True)


def _render_print_pdfs(white_paths: list[Path], final_dir: Path, settings: dict[str, Any]) -> tuple[Path, list[Path]]:
    """Create exact-size per-Sheet PDFs and one combined multipage PDF."""
    if not white_paths:
        raise ValueError("没有可输出到 PDF 的 Sheet 页面")
    pdf_dir = final_dir / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    page_size = (
        float(settings["sheet_width_mm"]) * 72.0 / 25.4,
        float(settings["sheet_height_mm"]) * 72.0 / 25.4,
    )

    def draw_page(document: pdf_canvas.Canvas, image_path: Path) -> None:
        document.drawImage(
            ImageReader(str(image_path)), 0, 0,
            width=page_size[0], height=page_size[1],
            preserveAspectRatio=False, mask="auto",
        )
        document.showPage()

    combined_path = pdf_dir / "print-sheets.pdf"
    combined = pdf_canvas.Canvas(str(combined_path), pagesize=page_size, pageCompression=1)
    page_paths: list[Path] = []
    for index, image_path in enumerate(white_paths, start=1):
        draw_page(combined, image_path)
        page_path = pdf_dir / f"sheet-{index:02d}.pdf"
        single = pdf_canvas.Canvas(str(page_path), pagesize=page_size, pageCompression=1)
        draw_page(single, image_path)
        single.save()
        page_paths.append(page_path)
    combined.save()
    return combined_path, page_paths


def selected_output_artifacts(outputs: dict[str, Any], job_dir: Path, prefix: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    transparent_paths = outputs.get("transparent_paths", [])
    white_paths = outputs.get("white_paths", [])
    if transparent_paths:
        items.append(artifact("layout", "selected-transparent", f"{prefix} · 透明首张", transparent_paths[0], job_dir))
    if white_paths:
        items.append(artifact("layout", "selected-white", f"{prefix} · 白底首张", white_paths[0], job_dir))
    combined_pdf = outputs.get("combined_pdf")
    if combined_pdf:
        items.append(artifact("layout", "selected-pdf", f"{prefix} · 全部 Sheet PDF", combined_pdf, job_dir, "pdf"))
    for index, page_path in enumerate(outputs.get("page_pdfs", []), start=1):
        items.append(artifact("layout", f"selected-pdf-page-{index:02d}", f"Sheet {index:02d} · 单页 PDF", page_path, job_dir, "pdf"))
    return items


def render_selected_outputs(candidate: dict[str, Any], geometries: list[dict[str, Any]], job_dir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    final_dir = job_dir / "final"
    transparent_dir = final_dir / "transparent"
    white_dir = final_dir / "white"
    _remove_stale_selected_outputs(final_dir)
    output_tile_cache: dict[tuple[str, int, float], Image.Image] = {}
    transparent_paths = render_candidate(
        candidate,
        geometries,
        job_dir,
        transparent_dir,
        int(settings["output_dpi"]),
        final=True,
        tile_cache=output_tile_cache,
    )
    white_dir.mkdir(parents=True, exist_ok=True)
    white_paths: list[Path] = []
    for path in transparent_paths:
        rgba = Image.open(path).convert("RGBA")
        white = Image.new("RGB", rgba.size, "white")
        white.paste(rgba, mask=rgba.getchannel("A"))
        white_path = white_dir / f"{path.stem}.jpg"
        white.save(white_path, quality=95, dpi=(int(settings["output_dpi"]), int(settings["output_dpi"])))
        white_paths.append(white_path)
        rgba.save(path, dpi=(int(settings["output_dpi"]), int(settings["output_dpi"])))
    combined_pdf, page_pdfs = _render_print_pdfs(white_paths, final_dir, settings)
    manifest = {
        "selected_candidate": candidate["id"],
        "page_count": candidate["page_count"],
        "layout_scale": candidate.get("layout_scale", 1.0),
        "compactness": candidate.get("compactness", 0.0),
        "alignment": candidate.get("alignment", 0.0),
        "largest_void_ratio": candidate.get("largest_void_ratio", 0.0),
        "packing_strategy": candidate.get("packing_strategy", candidate.get("strategy")),
        "settings": settings,
        "placements": candidate["placements"],
        "files": {
            "transparent_png": [_relative(path, job_dir) for path in transparent_paths],
            "white_jpg": [_relative(path, job_dir) for path in white_paths],
            "multipage_pdf": _relative(combined_pdf, job_dir),
            "page_pdfs": [_relative(path, job_dir) for path in page_pdfs],
        },
    }
    (final_dir / "layout.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "transparent_paths": transparent_paths,
        "white_paths": white_paths,
        "combined_pdf": combined_pdf,
        "page_pdfs": page_pdfs,
    }
