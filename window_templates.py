from __future__ import annotations

from copy import deepcopy
from typing import Any


DEFAULT_WINDOW_TEMPLATE = "double"
TEMPLATE_CONSTRAINT_VERSION = "window-template-v1"
ASPECT_RATIO_TOLERANCE = 0.02


WINDOW_TEMPLATES: dict[str, dict[str, Any]] = {
    "single": {
        "id": "single",
        "label": "单窗 · 3:4",
        "ratio": [3, 4],
        "default_mm": [450.0, 600.0],
        "generation_size": "1056x1408",
        "pane_count": 1,
        "center_gap_ratio": 0.0,
        "description": "一个连续的3:4竖向安装区域，不设置贯穿全高的中央分隔带。",
        "prompt_constraint": """【当前窗户模板硬约束：单窗】
整体画布必须为3:4竖版，对应一个连续的单窗安装区域。禁止生成贯穿全高的中央纯白分隔带，禁止强行左右对称或拆成两个独立窗格。主体和组合模块可以跨越画面中轴线，应共同形成统一、完整、有上下层次的纵向场景。不要绘制真实窗框、玻璃、把手、墙面或展示环境。""",
    },
    "double": {
        "id": "double",
        "label": "双栏窗 · 1:1",
        "ratio": [1, 1],
        "default_mm": [600.0, 600.0],
        "generation_size": "1216x1216",
        "pane_count": 2,
        "center_gap_ratio": 0.06,
        "description": "1:1整体画布，中间约6%连续纯白分隔带，左右各形成窄长竖向构图。",
        "prompt_constraint": """【当前窗户模板硬约束：双栏窗】
整体画布必须为1:1方形。画面由左右两个竖向内容区组成，中间必须保留约占画布总宽度6%的、从顶部贯穿到底部的连续纯白分隔带。任何图案、文字或装饰均不得跨越或侵入该分隔带。左右每侧宽度约为总宽度的47%，应分别形成窄长的竖窗构图，并各自具有从上到下的主次层级、视觉锚点和留白。两侧主题与画风必须一致、视觉重量大致平衡，但不得机械镜像或简单复制。不要绘制真实窗框、中柱、玻璃、把手、阴影或展示环境。""",
    },
}


def get_window_template(template_id: str | None, *, allow_legacy: bool = False) -> dict[str, Any]:
    value = str(template_id or DEFAULT_WINDOW_TEMPLATE).strip().lower()
    if value == "legacy" and allow_legacy:
        return {
            "id": "legacy",
            "label": "旧版未指定",
            "ratio": None,
            "default_mm": None,
            "generation_size": None,
            "pane_count": None,
            "center_gap_ratio": None,
            "description": "历史任务未保存窗户模板；历史结果保持不变。",
            "prompt_constraint": "",
        }
    if value not in WINDOW_TEMPLATES:
        raise ValueError(f"未知窗户模板：{template_id!r}，可选值为 single、double")
    return deepcopy(WINDOW_TEMPLATES[value])


def public_window_templates() -> list[dict[str, Any]]:
    return [deepcopy(WINDOW_TEMPLATES[key]) for key in ("single", "double")]


def expected_aspect_ratio(template_id: str | None) -> float | None:
    template = get_window_template(template_id, allow_legacy=True)
    ratio = template.get("ratio")
    if not ratio:
        return None
    return float(ratio[0]) / float(ratio[1])


def aspect_ratio_warning(
    width_px: int,
    height_px: int,
    template_id: str | None,
    tolerance: float = ASPECT_RATIO_TOLERANCE,
) -> dict[str, Any] | None:
    expected = expected_aspect_ratio(template_id)
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


def pane_layout(template_id: str | None, width_mm: float, height_mm: float) -> dict[str, Any]:
    template = get_window_template(template_id, allow_legacy=True)
    width = float(width_mm)
    height = float(height_mm)
    if template["id"] == "single":
        panes = [{"id": "pane-1", "x_mm": 0.0, "y_mm": 0.0, "width_mm": width, "height_mm": height}]
        gap_mm = 0.0
    elif template["id"] == "double":
        gap_mm = width * float(template["center_gap_ratio"])
        pane_width = (width - gap_mm) / 2.0
        panes = [
            {"id": "pane-left", "x_mm": 0.0, "y_mm": 0.0, "width_mm": pane_width, "height_mm": height},
            {
                "id": "pane-right",
                "x_mm": pane_width + gap_mm,
                "y_mm": 0.0,
                "width_mm": pane_width,
                "height_mm": height,
            },
        ]
    else:
        panes = []
        gap_mm = None
    return {
        "window_template": template["id"],
        "pane_count": template["pane_count"],
        "center_gap_ratio": template["center_gap_ratio"],
        "center_gap_mm": gap_mm,
        "installation_size_mm": [width, height],
        "panes": panes,
    }
