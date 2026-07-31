from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from window_templates import (
    TEMPLATE_CONSTRAINT_VERSION,
    resolve_window_spec,
)

LEGACY_DOUBLE_PROMPT = """参考输入的电商原图，为其中的窗贴设计创作同系列但差异明显的全新款式，并输出为干净的正视平面白底母版。本提示适用于任意节日、季节、人物、动物、植物、文字或装饰主题，不要擅自套用固定主题。

【最高优先级：主题表达不变】
先理解原设计正在表达的节庆或内容主题、情绪氛围、目标人群、核心主体、关键象征、故事关系和使用场景。创新前后必须表达同一个主题和用途，不得把原主题替换成其他节日、季节、故事或人群，不得删除决定主题识别的核心信息。保留窗贴产品类型、主角类别、核心识别点和整体画风方向。

【创新要求】
不要一比一复刻原图。应在不改变主题表达的前提下，对主体造型、动作姿态、道具组合、辅助元素、次级配色、装饰细节、组合方式和画面叙事进行重新设计；以上维度至少明显改变四项。不得只做轻微换色、替换小装饰、镜像、缩放或简单重排。结果应像同一商品系列中的全新设计，而不是换了主题的新产品，也不是原图的低差异改版。

【场景化与搭配关系】
不要生成彼此无关、尺寸相近、规则排列的图标素材合集。分析原图的安装构图逻辑和视觉层级，包括主要视觉锚点、上中下或四角区域、前后呼应、方向关系、重复节奏、大小层级、主体与装饰的依附关系以及留白。创新后的设计必须形成一套可以整体安装在窗户上的完整装饰方案：有明确主次、有大中小层级、有视觉重心，主要组合与填充元素相互呼应。
如果原图已有场景构图，保留其场景功能和区域关系并重新创作具体内容；如果原图只有零散素材，也要根据其原主题和主体关系组织成自然、协调的安装场景，但不得添加改变主题的新故事。

【双竖窗构图硬约束】
最终白底母版必须对应本次任务指定的“目标窗户／整套窗贴推荐展开尺寸”，整体画布必须采用本次任务尺寸约束中的宽高比；不得跟随方形或横向输入图的画布比例，也不得输出 1:1 方形画布。无论输入图整体画布接近方形、横向或竖向，都要把最终安装设计明确组织成左右并列的两条竖向窗格场景。整体白底母版中必须清楚看出“左侧竖窗内容区 + 中间纯白分隔带 + 右侧竖窗内容区”，不能只生成一个居中的方形主体，也不能生成没有左右归属的满版素材。
左右两个内容区都应呈明显的窄长竖向比例，并分别从上到下形成完整构图；每侧都要有自己的主要视觉锚点、陪衬元素、上下节奏和留白。两侧主题完全一致、风格统一、视觉重量大致平衡，但具体主体、动作、装饰和组合不能简单复制或机械镜像，应形成互补、呼应的双联场景。
中间分隔带保持连续、清楚的纯白留白，用来表达左右窗格边界并方便后续拆分；不要绘制真实窗框、门框、中柱、把手、玻璃、阴影或展示环境。所有主要模块必须明确归属于左侧或右侧竖窗，跨越中间分隔带的装饰禁止出现。
优先采用适合竖窗的纵向场景语法，例如顶部引导、中部主体、底部视觉基座，或沿竖向延伸的边框、藤蔓、枝条、文字与装饰节奏；具体采用哪种形式必须由输入主题决定，不得套用固定节日或固定元素。

【可生产的模块化】
依据输入内容自适应地组织为少量主要场景组合模块，加上必要的独立填充元素，不要固定模块数量。一个主要模块内部的相关元素可以接触、连接或合理遮叠，使其保持完整的语义和搭配关系；不同主要模块之间必须留出清晰、足够的纯白间隔，便于后续识别和拆分。填充元素可以独立，但必须服务于整体构图。所有模块和图案必须完整，不得被画布裁切。
【元素完整性与边缘互动】
禁止因画布空间不足造成的意外裁切、随机截断或元素越界。文字、面部、关键识别特征和主体轮廓不得被无意义切断；普通独立元素如果放不下，应缩小、移动或省略，不能直接用母版画布边缘将其切掉。
如果输入设计存在从窗户底边、侧边、顶部或中间窗缝探入的场景互动关系，可以保留或重新设计为探头、半身、局部进入、从边缘升起等构图。这类元素可以不展示完整身体，但必须具有明确的边缘互动意图，并作为一个设计完整、语义清楚的独立贴纸模块：截面位置合理，轮廓收口自然，不得切断面部、文字或关键识别特征，不能看起来像因画布不够而被随意截掉。
在生产白底母版中，必须完整展示这个“边缘互动贴纸模块”本身，并在模块四周保留可识别的纯白间隔；不要让模块的像素直接接触或穿出母版画布边缘。用模块自身经过设计的平直边、遮挡边或自然收口表达其将来依附窗户边缘的关系，而不是依靠母版画布真实裁切。其预期依附方向必须在构图中清楚可判断，例如从底部探出、依附左侧、依附右侧或靠近中间窗缝。
保持与原图相近的总体信息量、主题元素覆盖和视觉丰富度，同时避免把所有元素拆成孤立单品。

【文字与版权】
如果原图包含清晰可辨且承担主题表达的文字，只在能够逐字准确保留时使用；无法可靠识别时不要生成文字。不得新增品牌、Logo、水印、受保护IP角色或无关文字。

【输出规范】
整体画布必须与本次任务唯一尺寸约束中的物理宽高比一致；禁止输出 1:1 方形画布。画布内部必须严格呈现左右双竖窗的内容结构。背景必须是完全均匀的纯白色（#ffffff），不得有渐变、纹理、光照变化或阴影。图案边缘必须清晰，不得出现白色描边或白色光晕。
删除窗框、玻璃、墙面、户外背景、包装、商品场景、透视、反光和摄影阴影，但保留窗贴图案之间原本有价值的场景搭配关系。
只输出一张正视二维平面的双竖窗窗贴场景模块母版，不要输出商品效果图、展示样机、规则图标网格或无关联素材清单。最终输出前再次检查：画布四周必须全部为连续可见的纯白安全边距，不能有任何贴纸模块的像素接触或穿出画布边缘；允许模块内部采用具有明确场景意图、自然收口的半身或局部角色造型。"""

# ---------------------------------------------------------------------------
# Prompt 分层组装（六层模型的文字部分）：
#   prompts/core/base.md                              通用底线（全品类）
#   prompts/products/<product>/constraints.md          产品约束
#   prompts/products/<product>/layouts/<id>.md         版式（产品内可选）
# 分栏骨架与画布规格由 window_templates 在渲染时追加；内容简报现由参考图隐式携带。
# 每次组装现读文件（热加载），改文件不需要重启服务。
# ---------------------------------------------------------------------------

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
DEFAULT_PRODUCT = "window_sticker"
DEFAULT_PROMPT_STYLE = "scene"
_PROMPT_STYLE_ORDER = ["scene", "large_elements", "small_scatter"]


def _parse_style_file(path: Path) -> dict[str, str]:
    """样式文件格式：首行 `# 名称`，第二行可选 `> 一句话描述`，其余为 prompt 正文。"""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    label = path.stem
    description = ""
    body_start = 0
    if lines and lines[0].startswith("# "):
        label = lines[0][2:].strip()
        body_start = 1
    if len(lines) > body_start and lines[body_start].startswith("> "):
        description = lines[body_start][2:].strip()
        body_start += 1
    body = "\n".join(lines[body_start:]).strip()
    return {"id": path.stem, "label": label, "description": description, "text": body}


def list_prompt_styles(product: str = DEFAULT_PRODUCT) -> list[dict[str, str]]:
    layouts_dir = PROMPTS_DIR / "products" / product / "layouts"
    if not layouts_dir.is_dir():
        raise RuntimeError(f"缺少版式目录 {layouts_dir}")
    styles = {path.stem: _parse_style_file(path) for path in layouts_dir.glob("*.md")}
    if not styles:
        raise RuntimeError(f"{layouts_dir} 里没有任何版式文件")
    ordered = [styles[key] for key in _PROMPT_STYLE_ORDER if key in styles]
    ordered.extend(styles[key] for key in sorted(styles) if key not in _PROMPT_STYLE_ORDER)
    return ordered


def compose_base_prompt(style_id: str = DEFAULT_PROMPT_STYLE, product: str = DEFAULT_PRODUCT) -> str:
    """通用底线 + 产品约束 + 指定版式，构成用户可编辑的通用 prompt。"""
    core_path = PROMPTS_DIR / "core" / "base.md"
    if not core_path.is_file():
        raise RuntimeError(f"缺少通用底线文件 {core_path}")
    product_path = PROMPTS_DIR / "products" / product / "constraints.md"
    if not product_path.is_file():
        raise RuntimeError(f"缺少产品约束文件 {product_path}")
    layout_path = PROMPTS_DIR / "products" / product / "layouts" / f"{style_id}.md"
    if not layout_path.is_file():
        raise ValueError(
            f"未知 prompt 版式：{style_id!r}；可选：{', '.join(item['id'] for item in list_prompt_styles(product))}"
        )
    core = core_path.read_text(encoding="utf-8-sig").strip()
    constraints = product_path.read_text(encoding="utf-8-sig").strip()
    layout = _parse_style_file(layout_path)
    return f"{core}\n\n{constraints}\n\n{layout['text']}"


DEFAULT_PROMPT = compose_base_prompt()

DEFAULT_DIRECT_URL = "https://gptapi.longpean.com/gptImage/generateImageDirect"
DEFAULT_UPLOAD_URL = "https://stpic.longpean.com/picture/upLoadQiNiu"
DEFAULT_COMPAT_URL = "https://test-plugin.longpean.com/v1/chat/completions"
TEMPLATE_CONSTRAINT_MARKER = "【当前窗户模板硬约束"
SIZE_CONSTRAINT_MARKER = "【本次任务唯一尺寸约束】"


def render_generation_prompt(custom_prompt: str | None, settings: dict[str, Any]) -> str:
    """Append the window-template constraint to the user-editable common prompt.

    物理安装尺寸只影响后续几何/排版阶段（直接读 settings 数值），不进 prompt；
    这里保留对旧尺寸 marker 的截断，老任务的 base 残留旧段时重渲染仍然幂等。
    """
    base = (custom_prompt or DEFAULT_PROMPT).strip()
    markers = [marker for marker in (TEMPLATE_CONSTRAINT_MARKER, SIZE_CONSTRAINT_MARKER) if marker in base]
    if markers:
        base = base[: min(base.index(marker) for marker in markers)].rstrip()

    spec = resolve_window_spec(settings, allow_legacy=True)
    if spec["id"] == "legacy":
        raise ValueError("旧版任务未指定窗户模板；从生图阶段重跑前请先选择窗型预设或分栏×画布组合")
    return f"{base}\n\n{spec['prompt_constraint']}"


def _compat_endpoint() -> str:
    return (os.getenv("LP_COMPAT_BASE_URL") or os.getenv("LP_AI_BASE_URL") or DEFAULT_COMPAT_URL).strip()


def _compat_token() -> str:
    return (os.getenv("LP_COMPAT_TOKEN") or os.getenv("LP_AI_TOKEN") or "").strip()


def generation_configured() -> bool:
    provider = os.getenv("LP_IMAGE_PROVIDER", "chat_compat").strip().lower()
    direct = provider == "direct" and bool(
        os.getenv("LP_IMAGE_DIRECT_URL", DEFAULT_DIRECT_URL).strip()
        and os.getenv("LP_IMAGE_UPLOAD_URL", DEFAULT_UPLOAD_URL).strip()
    )
    compatible = provider == "chat_compat" and bool(_compat_endpoint() and _compat_token())
    return direct or compatible


def _data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(suffix, "image/png")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _decode_data_url(value: str) -> bytes | None:
    if not value.startswith("data:image/") or "," not in value:
        return None
    try:
        return base64.b64decode(value.split(",", 1)[1], validate=False)
    except (ValueError, binascii.Error):
        return None


def _looks_like_image(data: bytes) -> bool:
    try:
        from io import BytesIO

        with Image.open(BytesIO(data)) as image:
            image.verify()
        return True
    except Exception:
        return False


def _candidate_strings(payload: Any) -> list[tuple[str, str]]:
    preferred_keys = {"b64_json", "image", "image_url", "url", "data", "content", "output"}
    candidates: list[tuple[str, str]] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, str):
            if key in preferred_keys or value.startswith(("data:image/", "http://", "https://")) or len(value) > 4096:
                candidates.append((key, value))
        elif isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)

    walk(payload)
    candidates.sort(key=lambda item: (item[0] not in preferred_keys, not item[1].startswith("data:image/")))
    return candidates


def extract_image_bytes(payload: Any, timeout: int = 120) -> bytes:
    for key, value in _candidate_strings(payload):
        data = _decode_data_url(value)
        if data and _looks_like_image(data):
            return data
        if value.startswith(("http://", "https://")):
            try:
                response = requests.get(value, timeout=timeout)
                response.raise_for_status()
                if _looks_like_image(response.content):
                    return response.content
            except requests.RequestException:
                continue
        if (key == "b64_json" or len(value) > 4096) and re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", value):
            try:
                decoded = base64.b64decode(value, validate=False)
            except (ValueError, binascii.Error):
                continue
            if _looks_like_image(decoded):
                return decoded
    raise ValueError("生图接口响应中没有找到可识别的图片 URL、data URL 或 base64 数据")


def _headers(token: str = "") -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def upload_image_to_cloud(source_path: Path) -> tuple[str, dict[str, Any]]:
    upload_url = os.getenv("LP_IMAGE_UPLOAD_URL", DEFAULT_UPLOAD_URL).strip()
    if not upload_url:
        raise RuntimeError("未配置 LP_IMAGE_UPLOAD_URL；插件生图只接受远端 HTTP 参考图")
    suffix = source_path.suffix.lower() if source_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else ".png"
    filename = f"window-sticker-{uuid.uuid4().hex}{suffix}"
    response = requests.post(
        upload_url,
        headers=_headers(os.getenv("LP_IMAGE_UPLOAD_TOKEN", "").strip()),
        json={"picBytes": list(source_path.read_bytes()), "fileName": filename},
        timeout=(30, 180),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"参考图上传返回 HTTP {response.status_code}: {response.text[:1000]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("参考图上传接口没有返回 JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, str):
        url = data
    elif isinstance(data, dict):
        url = next((data.get(key) for key in ("url", "imageUrl", "imgUrl") if data.get(key)), "")
    else:
        url = ""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise RuntimeError("参考图上传响应中没有找到可访问的 HTTP 图片 URL")
    return url, payload


# Keep the private name for compatibility with older callers and tests.
_upload_reference = upload_image_to_cloud


def _wait_reference_ready(url: str, attempts: int = 3) -> None:
    """上传后预热并确认参考图 URL 可访问，缓解 CDN 首次回源超时（504）。

    确认失败不阻断——生图接口自身还有重试兜底。
    """
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, timeout=(10, 30))
            if response.status_code == 200 and response.content:
                return
        except requests.RequestException:
            pass
        if attempt < attempts:
            time.sleep(2 * attempt)


def _parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", value)
    if not match:
        raise ValueError(f"无效的生图尺寸 {value!r}，格式应为 宽x高，例如 1216x1216")
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        raise ValueError("生图尺寸必须大于0")
    return width, height


def _choose_generation_size(source_path: Path, settings: dict[str, Any] | None = None) -> str:
    del source_path  # The selected canvas spec, not the source image, controls the canvas.
    spec = resolve_window_spec(settings, allow_legacy=True)
    if spec["id"] == "legacy":
        raise ValueError("旧版任务未指定窗户模板；从生图阶段重跑前请先选择窗型预设或分栏×画布组合")
    configured = os.getenv("LP_IMAGE_SIZE", "auto").strip() or "auto"
    if configured.lower() == "auto":
        return str(spec["generation_size"])
    width, height = _parse_size(configured)
    actual_ratio = width / height
    expected_ratio = float(spec["ratio"][0]) / float(spec["ratio"][1])
    if abs(actual_ratio / expected_ratio - 1.0) > 0.02:
        raise ValueError(
            f"LP_IMAGE_SIZE={configured} 与窗户模板 {spec['id']} 的期望比例 "
            f"{spec['ratio'][0]}:{spec['ratio'][1]} 不一致；请改为 auto 或匹配比例的尺寸"
        )
    return f"{width}x{height}"


def _save_normalized_master(image_bytes: bytes, master_path: Path, requested_size: str) -> dict[str, Any]:
    from io import BytesIO

    target_width, target_height = _parse_size(requested_size)
    with Image.open(BytesIO(image_bytes)) as source:
        rgb = source.convert("RGB")
        original_size = list(rgb.size)
        original_ratio = rgb.width / max(rgb.height, 1)
        target_ratio = target_width / target_height
        ratio_deviation = abs(original_ratio / target_ratio - 1.0)
        normalized = rgb.size != (target_width, target_height)
        warning = None
        if normalized:
            scale = min(target_width / rgb.width, target_height / rgb.height)
            resized = rgb.resize(
                (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
                Image.Resampling.LANCZOS,
            )
            canvas = Image.new("RGB", (target_width, target_height), "white")
            canvas.paste(
                resized,
                ((target_width - resized.width) // 2, (target_height - resized.height) // 2),
            )
            rgb = canvas
        if ratio_deviation > 0.02:
            warning = (
                f"生图接口实际返回比例 {original_size[0]}:{original_size[1]} 与请求比例不一致；"
                "已保持内容比例并用白底容纳到目标画布，未拉伸内容。"
            )
        rgb.save(master_path)
    return {
        "generation_size_actual_original": original_size,
        "generation_size_actual": [target_width, target_height],
        "aspect_normalized": normalized,
        "aspect_ratio_warning": warning,
    }


def _generate_master_direct(
    source_path: Path,
    job_dir: Path,
    custom_prompt: str | None = None,
    settings: dict[str, Any] | None = None,
) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    endpoint = os.getenv("LP_IMAGE_DIRECT_URL", DEFAULT_DIRECT_URL).strip()
    if not endpoint:
        raise RuntimeError("未配置 LP_IMAGE_DIRECT_URL")
    prompt = (custom_prompt or DEFAULT_PROMPT).strip()
    reference_url, upload_response = _upload_reference(source_path)
    _wait_reference_ready(reference_url)
    size = _choose_generation_size(source_path, settings)
    request_payload = {
        "prompt": prompt,
        "size": size,
        "referenceImages": [reference_url],
        "templateCode": os.getenv("LP_IMAGE_TEMPLATE_CODE", "WINDOW_STICKER_MVP"),
        "operatorId": int(os.getenv("LP_OPERATOR_ID", "0") or 0),
        "operatorName": os.getenv("LP_OPERATOR_NAME", "Window Sticker Workbench"),
    }
    # 图床 CDN 刚上传后偶发 5xx（如“参考图 HTTP 504”），这类瞬时故障自动重试。
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                endpoint,
                headers=_headers(os.getenv("LP_IMAGE_TOKEN", "").strip()),
                json=request_payload,
                timeout=(30, 660),
            )
            if response.status_code >= 400:
                raise RuntimeError(f"生图接口返回 HTTP {response.status_code}: {response.text[:2000]}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("生图接口没有返回 JSON") from exc
            if not isinstance(payload, dict) or int(payload.get("success", 0)) != 1:
                message = payload.get("errorStr") if isinstance(payload, dict) else "未知错误"
                raise RuntimeError(f"生图接口业务失败: {message}")
            break
        except (requests.RequestException, RuntimeError) as exc:
            transient = re.search(r"HTTP\s*5\d{2}", str(exc)) or isinstance(exc, requests.RequestException)
            if attempt >= max_attempts or not transient:
                raise
            time.sleep(5 * attempt)
    data = payload.get("data") or {}
    image_url = data.get("imageUrl") or next(iter(data.get("imageUrls") or []), "")
    if not image_url:
        raise RuntimeError("生图接口成功响应中没有 imageUrl")
    image_response = requests.get(image_url, timeout=(30, 180))
    image_response.raise_for_status()
    if not _looks_like_image(image_response.content):
        raise RuntimeError("生图结果 URL 返回的内容不是有效图片")

    stage_dir = job_dir / "generate"
    stage_dir.mkdir(parents=True, exist_ok=True)
    request_summary = dict(request_payload)
    request_summary["referenceImages"] = [f"<uploaded:{source_path.name}:{source_path.stat().st_size} bytes>"]
    (stage_dir / "request-summary.json").write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (stage_dir / "upload-response.json").write_text(json.dumps(upload_response, ensure_ascii=False, indent=2), encoding="utf-8")
    (stage_dir / "raw-response.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (stage_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    master_path = stage_dir / "master.png"
    size_metadata = _save_normalized_master(image_response.content, master_path, size)
    artifacts = [
        {"name": "generation-request", "label": "插件生图请求摘要", "path": stage_dir / "request-summary.json", "kind": "json"},
        {"name": "generation-upload", "label": "参考图上传响应", "path": stage_dir / "upload-response.json", "kind": "json"},
        {"name": "generation-response", "label": "插件生图原始响应", "path": stage_dir / "raw-response.json", "kind": "json"},
        {"name": "generation-prompt", "label": "生图 Prompt", "path": stage_dir / "prompt.txt", "kind": "text"},
        {"name": "master", "label": "创新白底母版", "path": master_path, "kind": "image"},
    ]
    metadata = {
        "provider": "codex-gpt-image-2-direct",
        "model": data.get("model", "gpt-image-2"),
        "endpoint": endpoint,
        "size_requested": size,
        "size_actual": size_metadata["generation_size_actual"],
        **size_metadata,
        "duration_ms": data.get("durationMs"),
        "tokens_used": data.get("tokensUsed"),
        "request_id": payload.get("requestId"),
        "internal_request_id": data.get("requestId"),
        "prompt": prompt,
    }
    return master_path, artifacts, metadata


def _generate_master_chat_compat(
    source_path: Path,
    job_dir: Path,
    custom_prompt: str | None = None,
    settings: dict[str, Any] | None = None,
) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    endpoint = _compat_endpoint()
    token = _compat_token()
    model = os.getenv("LP_IMAGE_MODEL", "gpt-image-2").strip() or "gpt-image-2"
    if not endpoint or not token:
        raise RuntimeError("未配置 LP_AI_BASE_URL 或 LP_AI_TOKEN；可改用“直接上传纯色母版”模式")
    prompt = (custom_prompt or DEFAULT_PROMPT).strip()
    size = _choose_generation_size(source_path, settings)
    request_payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _data_url(source_path)}},
                ],
            }
        ],
        "stream": False,
        "image2_config": {
            "size": size,
            "template_code": os.getenv("LP_IMAGE_TEMPLATE_CODE", "WINDOW_STICKER_MVP"),
            "operator_id": int(os.getenv("LP_OPERATOR_ID", "0") or 0),
            "operator_name": os.getenv("LP_OPERATOR_NAME", "Window Sticker Workbench"),
        },
    }
    request_summary = json.loads(json.dumps(request_payload, ensure_ascii=False))
    request_summary["messages"][0]["content"][1]["image_url"]["url"] = f"<data-url:{source_path.name}:{source_path.stat().st_size} bytes>"
    response = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=request_payload,
        timeout=(30, 600),
    )
    if response.status_code >= 400:
        body = response.text[:2000]
        raise RuntimeError(f"生图接口返回 HTTP {response.status_code}: {body}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("生图接口没有返回 JSON") from exc

    stage_dir = job_dir / "generate"
    stage_dir.mkdir(parents=True, exist_ok=True)
    raw_path = stage_dir / "raw-response.json"
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    request_path = stage_dir / "request-summary.json"
    request_path.write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    prompt_path = stage_dir / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    image_bytes = extract_image_bytes(payload)
    master_path = stage_dir / "master.png"
    size_metadata = _save_normalized_master(image_bytes, master_path, size)
    artifacts = [
        {"name": "generation-request", "label": "生图请求摘要", "path": request_path, "kind": "json"},
        {"name": "generation-response", "label": "生图原始响应", "path": raw_path, "kind": "json"},
        {"name": "generation-prompt", "label": "生图 Prompt", "path": prompt_path, "kind": "text"},
        {"name": "master", "label": "创新白底母版", "path": master_path, "kind": "image"},
    ]
    metadata = {
        "model": model,
        "endpoint": endpoint,
        "prompt": prompt,
        "size_requested": size,
        "size_actual": size_metadata["generation_size_actual"],
        **size_metadata,
    }
    return master_path, artifacts, metadata


def generate_master(
    source_path: Path,
    job_dir: Path,
    custom_prompt: str | None = None,
    settings: dict[str, Any] | None = None,
) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    spec = resolve_window_spec(settings, allow_legacy=True)
    if os.getenv("LP_IMAGE_PROVIDER", "chat_compat").strip().lower() == "direct":
        result = _generate_master_direct(source_path, job_dir, custom_prompt, settings)
    else:
        result = _generate_master_chat_compat(source_path, job_dir, custom_prompt, settings)
    master_path, artifacts, metadata = result
    ratio = spec.get("ratio")
    metadata.update(
        {
            "window_template": spec["id"],
            "frame_id": spec.get("frame_id"),
            "canvas_id": spec.get("canvas_id"),
            "generation_size_requested": metadata.get("size_requested"),
            "expected_aspect_ratio": (float(ratio[0]) / float(ratio[1])) if ratio else None,
            "template_constraint_version": TEMPLATE_CONSTRAINT_VERSION,
        }
    )
    return master_path, artifacts, metadata
