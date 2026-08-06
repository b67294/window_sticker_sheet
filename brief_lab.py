"""VLM 内容简报实验室：图→文→图创新链路的独立模块。

与主 pipeline 零耦合，只被 /brief 实验页和 experiments/vlm_brief 脚本使用。
创新指导提示词在 prompts/brief/innovation.md（热加载，改文件即生效）。
"""
from __future__ import annotations

import base64
import os
import re
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

import requests
from PIL import Image

from generation import PROMPTS_DIR, list_prompt_styles
from window_templates import get_canvas, get_frame, public_window_frames

APP_DIR = Path(__file__).resolve().parent
INNOVATION_PROMPT_PATH = APP_DIR / "prompts" / "brief" / "innovation.md"
BRIEF_LAYOUT_IDS = (
    "large_elements",
    "scene",
    "white_silhouette_scene",
    "small_scatter",
)
BRIEF_LAYOUT_LABELS = {
    "large_elements": "大元素",
    "scene": "场景式",
    "small_scatter": "小元素满铺",
    "white_silhouette_scene": "白色剪影场景",
}
DEFAULT_BRIEF_LAYOUT = "large_elements"
DEFAULT_BRIEF_CANVAS = "1:1"
DEFAULT_BRIEF_FRAME = "pane1"


def brief_frames() -> list[dict[str, Any]]:
    """分栏骨架选项：几何参数来自 window_templates，约束文本来自 frames/*.md（热加载）。"""
    return public_window_frames()

CHAT_URL = "https://test-plugin.longpean.com/v1/chat/completions"
IMAGE_URL = "https://gptapi.longpean.com/gptImage/generateImageDirect"
UPLOAD_URL = "https://stpic.longpean.com/picture/upLoadQiNiu"

# 模型链（按顺序尝试，可用 LP_BRIEF_VLM_MODELS 覆盖，逗号分隔）。
# 2026-08-06 实测：codex-gpt-5.6-* 全部 502（网关 Codex CLI 版本过旧，报
# "requires a newer version of Codex"），故默认链改用已验证可用的 5.5 与 gpt-4o；
# 末位保留一次 5.6-luna 探测，网关修复后无需改代码即可自动恢复使用。
DEFAULT_VLM_CHAIN = [
    "codex-gpt-5.5",
    "codex-gpt-5.5",
    "gpt-4o",
    "gpt-4o",
    "codex-gpt-5.6-luna",
]


class VLMChainError(RuntimeError):
    """整条模型链都失败；消息里带每个模型的原始报错，便于页面直接展示。"""


def vlm_chain() -> list[str]:
    configured = os.getenv("LP_BRIEF_VLM_MODELS", "").strip()
    if configured:
        models = [item.strip() for item in configured.split(",") if item.strip()]
        if models:
            return models
    return list(DEFAULT_VLM_CHAIN)


# 兼容旧引用；实际调用请用 vlm_chain()。
VLM_CHAIN = DEFAULT_VLM_CHAIN

# 独立脚本（experiments/vlm_brief）用的简化生产约束；实验页走五段式可见组装。
PRODUCTION_SUFFIX = (
    "\n\n输出要求：一张正视二维平面的窗贴图案母版。背景完全均匀纯白（#ffffff），"
    "无渐变、纹理、光照或阴影；图案边缘清晰，无白色描边或光晕。"
    "不绘制窗框、玻璃、墙面、实景环境、展示样机或商品照片。"
    "各元素之间保留清晰纯白间隔，画布四周保留纯白安全边距，元素不接触画布边缘。"
)

# 全部 VLM 模型失败时的兜底内容简报（生成时附参考图，其余约束由五段组装提供）。
FALLBACK_PROMPT = (
    "参考输入图中的图案主题与画风，重新设计一款同系列但差异明显的全新窗贴图案。"
    "保留主角类型、节日氛围和整体风格方向，但主体造型、动作姿态、道具组合、背景元素、"
    "次级配色和构图必须至少四处明显不同，像同一系列的另一款产品而不是原图改版。"
)

# VLM 失败时仍然给出三个各有侧重的方向，保证"一张输入 = 三个衍生方向"的预期不变。
FALLBACK_DIRECTIONS = [
    ("兜底 · 换主体动作与道具", "本款重点改变主角的动作姿态、道具组合与配件细节，构图与场景保持同类但不照搬。"),
    ("兜底 · 换场景与背景主题", "本款重点改变背景主题与场景元素，把主角放进同一节日下的另一处情境。"),
    ("兜底 · 换构图与色彩氛围", "本款重点改变构图布局、大小层级与配色氛围，形成与原图明显不同的视觉节奏。"),
]


def fallback_directions() -> list[dict[str, Any]]:
    return [
        {
            "index": index,
            "direction": label,
            "prompt": f"{FALLBACK_PROMPT}{extra}",
            "status": "idle",
            "images": [],
        }
        for index, (label, extra) in enumerate(FALLBACK_DIRECTIONS, start=1)
    ]


def _read_prompt(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(f"缺少 Prompt 文件 {path}")
    return path.read_text(encoding="utf-8-sig").strip()


def brief_prompt_styles() -> list[dict[str, str]]:
    """返回创新简报实验室可见且可单独保存的版式。"""
    styles = {item["id"]: item for item in list_prompt_styles()}
    result = []
    for style_id in BRIEF_LAYOUT_IDS:
        if style_id not in styles:
            raise RuntimeError(f"缺少创新简报版式 {style_id}")
        item = dict(styles[style_id])
        item["label"] = BRIEF_LAYOUT_LABELS[style_id]
        result.append(item)
    return result


def default_brief_prompt_parts() -> dict[str, Any]:
    styles = {item["id"]: item for item in brief_prompt_styles()}
    return {
        "core_prompt": _read_prompt(PROMPTS_DIR / "core" / "base.md"),
        # 创新简报实验室暂不追加产品约束；入口保留给人工试验。
        "product_prompt": "",
        "prompt_style": DEFAULT_BRIEF_LAYOUT,
        "layout_prompt": styles[DEFAULT_BRIEF_LAYOUT]["text"],
        "frame_id": DEFAULT_BRIEF_FRAME,
        "frame_prompt": get_frame(DEFAULT_BRIEF_FRAME)["prompt_constraint"],
        "canvas_id": DEFAULT_BRIEF_CANVAS,
    }


def brief_canvas_spec(canvas_id: str | None) -> dict[str, Any]:
    canvas = get_canvas((canvas_id or DEFAULT_BRIEF_CANVAS).strip())
    width_ratio, height_ratio = canvas["ratio"]
    if width_ratio == height_ratio:
        shape = f"整体画布必须为{canvas['label']}。"
    else:
        orientation = "竖版" if height_ratio > width_ratio else "横版"
        shape = f"整体画布必须为宽{width_ratio}:高{height_ratio}的{orientation}。"
    lines = [shape]
    if canvas.get("orientation_hint"):
        lines.append(canvas["orientation_hint"])
    lines.append("本段只约束分栏结构、画布比例与构图方向，不新增题材内容。")
    return {
        "id": canvas["id"],
        "label": canvas["label"],
        "ratio": list(canvas["ratio"]),
        "generation_size": canvas["generation_size"],
        "prompt_constraint": "\n".join(lines),
    }


def assemble_generation_prompt(
    brief_text: str,
    prompt_style: str = DEFAULT_BRIEF_LAYOUT,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按前端可见的五段组装，不追加任何隐藏 Prompt。第 5 段 = 分栏骨架 × 画布比例。"""
    settings = settings or {}
    styles = {item["id"]: item for item in brief_prompt_styles()}
    if prompt_style not in styles:
        raise ValueError(f"未知创新简报版式：{prompt_style!r}；可选：{', '.join(BRIEF_LAYOUT_IDS)}")
    defaults = default_brief_prompt_parts()
    core_value = settings.get("core_prompt")
    product_value = settings.get("product_prompt")
    layout_value = settings.get("layout_prompt")
    frame_value = settings.get("frame_prompt")
    core_prompt = str(defaults["core_prompt"] if core_value is None else core_value).strip()
    product_prompt = str(defaults["product_prompt"] if product_value is None else product_value).strip()
    layout_prompt = str(styles[prompt_style]["text"] if layout_value is None else layout_value).strip()
    content_prompt = brief_text.strip()
    if not core_prompt:
        raise ValueError("通用底线不能为空")
    if not layout_prompt:
        raise ValueError("版式 Prompt 不能为空")
    if not content_prompt:
        raise ValueError("内容简报不能为空")
    frame = get_frame(str(settings.get("frame_id") or DEFAULT_BRIEF_FRAME).strip())
    frame_prompt = str(frame["prompt_constraint"] if frame_value is None else frame_value).strip()
    if not frame_prompt:
        raise ValueError("分栏骨架 Prompt 不能为空")
    spec = brief_canvas_spec(settings.get("canvas_id"))
    spec["frame_id"] = frame["id"]
    spec["frame_label"] = frame["label"]
    spec_text = f"{frame_prompt}\n{spec['prompt_constraint']}"
    sections = [
        {"id": "core", "label": "1 通用底线", "text": core_prompt},
        {"id": "product", "label": "2 产品约束", "text": product_prompt},
        {"id": "layout", "label": f"3 版式 · {styles[prompt_style]['label']}", "text": layout_prompt},
        {"id": "content", "label": "4 内容简报", "text": content_prompt},
        {"id": "spec", "label": f"5 规格 · {frame['label']} × {spec['label']}", "text": spec_text},
    ]
    prompt = "\n\n".join(
        f"【{section['label']}】\n{section['text'] or '（无）'}"
        for section in sections
    )
    return {"prompt": prompt, "sections": sections, "spec": spec}


def compose_generation_prompt(
    brief_text: str,
    prompt_style: str = DEFAULT_BRIEF_LAYOUT,
    settings: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    assembled = assemble_generation_prompt(brief_text, prompt_style, settings)
    return assembled["prompt"], assembled["spec"]


def vlm_token() -> str:
    token = (os.getenv("LP_VISION_TOKEN") or os.getenv("LP_COMPAT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("未配置 LP_COMPAT_TOKEN / LP_VISION_TOKEN")
    return token


def innovation_prompt() -> str:
    if not INNOVATION_PROMPT_PATH.is_file():
        raise RuntimeError(f"缺少创新指导提示词 {INNOVATION_PROMPT_PATH}")
    return INNOVATION_PROMPT_PATH.read_text(encoding="utf-8-sig")


def image_data_url(path: Path, max_side: int = 1536, quality: int = 80) -> str:
    with Image.open(path) as image:
        image.load()
        if max(image.size) > max_side:
            scale = max_side / max(image.size)
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
        buffer = BytesIO()
        image.save(buffer, "WEBP", quality=quality)
    return "data:image/webp;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def call_vlm(
    image_path: Path,
    log: Callable[[str], None] = print,
    prompt: str | None = None,
) -> tuple[str, str] | None:
    """返回 (model, content)；全链路失败返回 None。"""
    token = vlm_token()
    prompt = (prompt or innovation_prompt()).strip()
    if not prompt:
        raise ValueError("创新反推 Prompt 不能为空")
    data_url = image_data_url(image_path)
    chain = vlm_chain()
    errors: list[str] = []
    for index, model in enumerate(chain, start=1):
        try:
            log(f"[VLM] attempt {index}/{len(chain)} model={model}")
            response = requests.post(
                CHAT_URL,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }],
                    "stream": False,
                },
                timeout=(120, 900),
            )
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            content = response.json()["choices"][0]["message"]["content"]
            if not content or "【生图提示词 1】" not in content:
                raise RuntimeError("响应缺少期望的定界符")
            return model, content
        except Exception as exc:  # noqa: BLE001 - 实验链路，逐级回退
            message = f"{model}: {str(exc)[:200]}"
            errors.append(message)
            log(f"[VLM] 失败 {message}")
            time.sleep(3)
    # 把逐个模型的失败原因带出去，便于页面直接显示而不必翻服务日志。
    raise VLMChainError("VLM 全部模型失败：" + "；".join(errors))


def parse_brief(content: str) -> dict[str, Any]:
    summary_match = re.search(r"【反推重点】\s*(.*?)\s*【衍生方向", content, re.S)
    directions_meta = re.findall(r"【衍生方向 (\d)】\s*(.*?)\s*【生图提示词 \1】", content, re.S)
    prompts = re.findall(r"【生图提示词 (\d)】\s*(.*?)(?=【衍生方向|\Z)", content, re.S)
    direction_map = {number: text.strip() for number, text in directions_meta}
    return {
        "summary": summary_match.group(1).strip() if summary_match else "",
        "directions": [
            {"index": int(number), "direction": direction_map.get(number, ""), "prompt": text.strip()}
            for number, text in prompts
            if text.strip()
        ],
    }


def upload_reference(path: Path, log: Callable[[str], None] = print) -> str:
    log("[上传] 参考图 → 图云")
    response = requests.post(
        UPLOAD_URL,
        headers={"Content-Type": "application/json"},
        json={"picBytes": list(path.read_bytes()), "fileName": f"brief-{uuid.uuid4().hex}{path.suffix}"},
        timeout=(30, 180),
    )
    response.raise_for_status()
    url = response.json().get("data")
    if not isinstance(url, str) or not url.startswith("http"):
        raise RuntimeError(f"图云上传响应异常: {response.text[:300]}")
    return url


def generate_image(
    prompt: str,
    size: str,
    out_path: Path,
    log: Callable[[str], None] = print,
    reference_url: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "prompt": prompt,
        "size": size,
        "templateCode": "VLM_BRIEF_LAB",
        "operatorName": "brief-lab",
    }
    if reference_url:
        payload["referenceImages"] = [reference_url]
    for attempt in range(1, 4):
        try:
            log(f"[生图] {out_path.name} attempt {attempt}")
            response = requests.post(IMAGE_URL, json=payload, timeout=(60, 660))
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            data = response.json()
            if int(data.get("success", 0)) != 1:
                raise RuntimeError(f"业务失败: {data.get('errorStr')}")
            image_url = data["data"].get("imageUrl") or next(iter(data["data"].get("imageUrls") or []), "")
            image_bytes = requests.get(image_url, timeout=(30, 180)).content
            out_path.write_bytes(image_bytes)
            return {
                "ok": True,
                "image_url": image_url,
                "duration_ms": data["data"].get("durationMs"),
                "request_id": data.get("requestId"),
            }
        except Exception as exc:  # noqa: BLE001
            log(f"[生图] 失败: {exc}")
            if attempt < 3 and re.search(r"HTTP\s*5\d{2}|aborted|timed out|Timeout", str(exc)):
                time.sleep(5 * attempt)
                continue
            return {"ok": False, "error": str(exc)[:500]}
    return {"ok": False, "error": "unreachable"}
