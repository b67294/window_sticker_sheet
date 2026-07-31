"""窗贴的画布三轴模型：画布比例 × 分栏骨架 × （版式在 prompts/ 层）。

- CANVAS_RATIOS：纯生图比例，决定生图像素画布。
- FRAMES：分栏骨架（硬分隔），决定"图案不得跨越"的纯白分隔带结构。
- PRESETS：运营用的窗型预设 = (frame, canvas, 默认安装尺寸) 的命名组合；
  旧字段 window_template 存的就是预设 id，自定义组合记为 "custom"。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


DEFAULT_WINDOW_TEMPLATE = "double"
TEMPLATE_CONSTRAINT_VERSION = "window-template-v2"
ASPECT_RATIO_TOLERANCE = 0.02


CANVAS_RATIOS: dict[str, dict[str, Any]] = {
    "1:1": {
        "id": "1:1",
        "label": "1:1 方形",
        "ratio": [1, 1],
        "generation_size": "1216x1216",
        "orientation_hint": "",
    },
    "73:87": {
        "id": "73:87",
        "label": "竖 73:87",
        "ratio": [73, 87],
        "generation_size": "1168x1392",
        "orientation_hint": "整体构图应顺应纵向延伸的画布，形成有上下层次的竖向节奏。",
    },
    "87:73": {
        "id": "87:73",
        "label": "横 87:73",
        "ratio": [87, 73],
        "generation_size": "1392x1168",
        "orientation_hint": "整体构图应横向延展，场景从左到右自然流动，形成有左右节奏的横向画面。",
    },
    "3:4": {
        "id": "3:4",
        "label": "竖 3:4",
        "ratio": [3, 4],
        "generation_size": "1056x1408",
        "orientation_hint": "整体构图应顺应纵向延伸的画布，形成有上下层次的竖向节奏。",
    },
}


FRAMES: dict[str, dict[str, Any]] = {
    "pane1": {
        "id": "pane1",
        "label": "1栏 · 无分隔带",
        "pane_count": 1,
        "gap_ratio": 0.0,
        "prompt_constraint": (
            "对应一个连续的单栏安装区域。禁止生成贯穿全高的中央纯白分隔带，"
            "禁止强行左右对称或拆成多个独立窗格。主体和组合模块可以跨越画面中轴线，"
            "应共同形成统一、完整、有层次的场景。"
        ),
    },
    "pane2": {
        "id": "pane2",
        "label": "2栏 · 中间6%分隔带",
        "pane_count": 2,
        "gap_ratio": 0.06,
        "prompt_constraint": (
            "画面由左右两个竖向内容区组成，中间必须保留约占画布总宽度6%的、"
            "从顶部贯穿到底部的连续纯白分隔带。任何图案、文字或装饰均不得跨越或侵入该分隔带。"
            "左右每侧应分别形成窄长的竖窗构图，并各自具有从上到下的主次层级、视觉锚点和留白。"
            "两侧主题与画风必须一致、视觉重量大致平衡，但不得机械镜像或简单复制。"
        ),
    },
    "pane3": {
        "id": "pane3",
        "label": "3栏 · 两条5%分隔带",
        "pane_count": 3,
        "gap_ratio": 0.05,
        "prompt_constraint": (
            "画面由三个竖向内容区组成，相邻内容区之间必须各保留一条约占画布总宽度5%的、"
            "从顶部贯穿到底部的连续纯白分隔带。任何图案、文字或装饰均不得跨越或侵入分隔带。"
            "三栏各自形成窄长的竖窗构图、有自己的主次层级与留白；三栏主题与画风一致、"
            "视觉重量大致平衡，不得机械镜像或简单复制。"
        ),
    },
}


PRESETS: dict[str, dict[str, Any]] = {
    "double": {
        "id": "double",
        "label": "双栏窗 · 1:1",
        "frame_id": "pane2",
        "canvas_id": "1:1",
        "default_mm": [600.0, 600.0],
        "description": "1:1整体画布，中间约6%连续纯白分隔带，左右各形成窄长竖向构图。",
    },
    "single_portrait": {
        "id": "single_portrait",
        "label": "单栏窗 · 竖 73:87",
        "frame_id": "pane1",
        "canvas_id": "73:87",
        "default_mm": [730.0, 870.0],
        "description": "一个连续的73:87竖向安装区域（高略大于宽），无分隔带。",
    },
    "single_landscape": {
        "id": "single_landscape",
        "label": "单栏窗 · 横 87:73",
        "frame_id": "pane1",
        "canvas_id": "87:73",
        "default_mm": [870.0, 730.0],
        "description": "一个连续的87:73横向安装区域（宽略大于高），无分隔带。",
    },
    "big_single": {
        "id": "big_single",
        "label": "大单窗 · 1:1",
        "frame_id": "pane1",
        "canvas_id": "1:1",
        "default_mm": [600.0, 600.0],
        "description": "1:1整体画布的整面大单窗，无分隔带。",
    },
    "single": {
        "id": "single",
        "label": "单窗 · 3:4（旧）",
        "frame_id": "pane1",
        "canvas_id": "3:4",
        "default_mm": [450.0, 600.0],
        "description": "一个连续的3:4竖向安装区域，无分隔带。",
    },
}


_LEGACY_SPEC: dict[str, Any] = {
    "id": "legacy",
    "label": "旧版未指定",
    "frame_id": None,
    "canvas_id": None,
    "ratio": None,
    "default_mm": None,
    "generation_size": None,
    "pane_count": None,
    "center_gap_ratio": None,
    "gap_ratio": None,
    "description": "历史任务未保存窗户模板；历史结果保持不变。",
    "prompt_constraint": "",
}


def get_frame(frame_id: str) -> dict[str, Any]:
    if frame_id not in FRAMES:
        raise ValueError(f"未知分栏骨架：{frame_id!r}，可选值为 {', '.join(FRAMES)}")
    return deepcopy(FRAMES[frame_id])


def get_canvas(canvas_id: str) -> dict[str, Any]:
    if canvas_id not in CANVAS_RATIOS:
        raise ValueError(f"未知画布比例：{canvas_id!r}，可选值为 {', '.join(CANVAS_RATIOS)}")
    return deepcopy(CANVAS_RATIOS[canvas_id])


def _match_preset(frame_id: str, canvas_id: str) -> str:
    for preset in PRESETS.values():
        if preset["frame_id"] == frame_id and preset["canvas_id"] == canvas_id:
            return preset["id"]
    return "custom"


def _build_constraint(frame: dict[str, Any], canvas: dict[str, Any]) -> str:
    width_ratio, height_ratio = canvas["ratio"]
    if width_ratio == height_ratio:
        shape_text = f"整体画布必须为{canvas['label']}。"
    else:
        orientation = "竖版" if height_ratio > width_ratio else "横版"
        shape_text = f"整体画布必须为宽{width_ratio}:高{height_ratio}的{orientation}。"
    parts = [
        f"【当前窗户模板硬约束：{frame['label']} × {canvas['label']}】",
        shape_text + frame["prompt_constraint"],
    ]
    if canvas.get("orientation_hint"):
        parts.append(canvas["orientation_hint"])
    parts.append("不要绘制真实窗框、中柱、玻璃、把手、墙面、阴影或展示环境。")
    return "\n".join(parts)


def resolve_window_spec(settings: dict[str, Any] | None, *, allow_legacy: bool = False) -> dict[str, Any]:
    """从 settings 解析出完整的画布规格（预设或自定义 frame×canvas 组合）。"""
    settings = settings or {}
    template_id = str(settings.get("window_template") or "").strip().lower()
    frame_id = str(settings.get("frame_id") or "").strip()
    canvas_id = str(settings.get("canvas_id") or "").strip()

    if template_id == "legacy" and not (frame_id and canvas_id):
        if allow_legacy:
            return deepcopy(_LEGACY_SPEC)
        raise ValueError("旧版任务未指定窗户模板")

    if not (frame_id and canvas_id):
        preset_id = template_id or DEFAULT_WINDOW_TEMPLATE
        if preset_id == "custom":
            raise ValueError("window_template=custom 时必须同时提供 frame_id 和 canvas_id")
        if preset_id not in PRESETS:
            raise ValueError(f"未知窗型预设：{preset_id!r}，可选值为 {', '.join(PRESETS)}")
        preset = PRESETS[preset_id]
        frame_id, canvas_id = preset["frame_id"], preset["canvas_id"]

    frame = get_frame(frame_id)
    canvas = get_canvas(canvas_id)
    preset_id = _match_preset(frame_id, canvas_id)
    preset = PRESETS.get(preset_id)
    if preset:
        default_mm = list(preset["default_mm"])
        label = preset["label"]
        description = preset["description"]
    else:
        width_ratio, height_ratio = canvas["ratio"]
        default_mm = [600.0, round(600.0 * height_ratio / width_ratio, 1)]
        label = f"自定义 · {frame['label']} × {canvas['label']}"
        description = f"自定义组合：{frame['label']}，画布 {canvas['label']}。"
    return {
        "id": preset_id,
        "label": label,
        "frame_id": frame_id,
        "canvas_id": canvas_id,
        "ratio": list(canvas["ratio"]),
        "default_mm": default_mm,
        "generation_size": canvas["generation_size"],
        "pane_count": frame["pane_count"],
        "gap_ratio": frame["gap_ratio"],
        # 旧字段名兼容：双栏时代的 center_gap_ratio 即单条分隔带宽度占比。
        "center_gap_ratio": frame["gap_ratio"],
        "description": description,
        "prompt_constraint": _build_constraint(frame, canvas),
    }


def get_window_template(template_id: str | None, *, allow_legacy: bool = False) -> dict[str, Any]:
    """按预设 id 取规格（兼容旧调用方；自定义组合请用 resolve_window_spec）。"""
    return resolve_window_spec({"window_template": template_id}, allow_legacy=allow_legacy)


def public_window_templates() -> list[dict[str, Any]]:
    return [get_window_template(key) for key in PRESETS]


def public_window_frames() -> list[dict[str, Any]]:
    return [deepcopy(FRAMES[key]) for key in FRAMES]


def public_canvas_ratios() -> list[dict[str, Any]]:
    return [deepcopy(CANVAS_RATIOS[key]) for key in CANVAS_RATIOS]


def expected_aspect_ratio(settings_or_template: dict[str, Any] | str | None) -> float | None:
    if isinstance(settings_or_template, dict):
        spec = resolve_window_spec(settings_or_template, allow_legacy=True)
    else:
        spec = get_window_template(settings_or_template, allow_legacy=True)
    ratio = spec.get("ratio")
    if not ratio:
        return None
    return float(ratio[0]) / float(ratio[1])


def aspect_ratio_warning(
    width_px: int,
    height_px: int,
    settings_or_template: dict[str, Any] | str | None,
    tolerance: float = ASPECT_RATIO_TOLERANCE,
) -> dict[str, Any] | None:
    expected = expected_aspect_ratio(settings_or_template)
    if expected is None or width_px <= 0 or height_px <= 0:
        return None
    actual = float(width_px) / float(height_px)
    deviation = abs(actual / expected - 1.0)
    if deviation <= tolerance:
        return None
    return {
        "code": "aspect_ratio_mismatch",
        "message": (
            f"上传图片比例为 {width_px}:{height_px}（{actual:.4f}），"
            f"与模板期望比例 {expected:.4f} 偏差 {deviation * 100:.1f}%；"
            "仍允许继续，系统不会拉伸或自动裁切上传图片。"
        ),
        "actual_size": [int(width_px), int(height_px)],
        "actual_aspect_ratio": actual,
        "expected_aspect_ratio": expected,
        "deviation_ratio": deviation,
        "tolerance_ratio": tolerance,
    }


def pane_layout(settings_or_template: dict[str, Any] | str | None, width_mm: float, height_mm: float) -> dict[str, Any]:
    if isinstance(settings_or_template, dict):
        spec = resolve_window_spec(settings_or_template, allow_legacy=True)
    else:
        spec = get_window_template(settings_or_template, allow_legacy=True)
    width = float(width_mm)
    height = float(height_mm)
    pane_count = spec.get("pane_count")
    if pane_count:
        gap_mm = width * float(spec["gap_ratio"])
        gap_total = gap_mm * (pane_count - 1)
        pane_width = (width - gap_total) / pane_count
        panes = []
        for index in range(pane_count):
            panes.append({
                "id": f"pane-{index + 1}",
                "x_mm": index * (pane_width + gap_mm),
                "y_mm": 0.0,
                "width_mm": pane_width,
                "height_mm": height,
            })
        gap_value: float | None = gap_mm if pane_count > 1 else 0.0
    else:
        panes = []
        gap_value = None
    return {
        "window_template": spec["id"],
        "frame_id": spec.get("frame_id"),
        "canvas_id": spec.get("canvas_id"),
        "pane_count": pane_count,
        "center_gap_ratio": spec.get("center_gap_ratio"),
        "center_gap_mm": gap_value,
        "installation_size_mm": [width, height],
        "panes": panes,
    }
