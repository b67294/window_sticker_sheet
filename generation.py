from __future__ import annotations

import base64
import binascii
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import requests
from PIL import Image


DEFAULT_PROMPT = """参考输入电商图片，重建其中的窗贴设计为干净的正视平面素材母版。
保留原始主题、配色、图案类别、相对尺寸、数量、文字和整体风格；删除窗户、玻璃、墙面、户外背景、反光、阴影、包装和商品场景。
所有独立图案必须完整、彼此不接触、不重叠，并保留足够空隙。不得裁切任何图案，不得增加品牌、Logo、IP角色或水印。
背景必须是完全均匀的纯 #ff00ff 色，不得有渐变、纹理、光照变化和阴影，图案内部不得使用该颜色。
输出单张正视平面窗贴素材图，不要输出商品效果图。"""

DEFAULT_DIRECT_URL = "https://gptapi.longpean.com/gptImage/generateImageDirect"
DEFAULT_UPLOAD_URL = "https://stpic.longpean.com/picture/upLoadQiNiu"
DEFAULT_COMPAT_URL = "https://test-plugin.longpean.com/v1/chat/completions"


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


def _upload_reference(source_path: Path) -> tuple[str, dict[str, Any]]:
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


def _choose_generation_size(source_path: Path) -> str:
    configured = os.getenv("LP_IMAGE_SIZE", "auto").strip() or "auto"
    if configured != "auto":
        return configured
    with Image.open(source_path) as image:
        ratio = image.width / max(image.height, 1)
    if ratio > 1.2:
        return "1536x1024"
    if ratio < 0.83:
        return "1024x1536"
    return "1024x1024"


def _generate_master_direct(source_path: Path, job_dir: Path, custom_prompt: str | None = None) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    endpoint = os.getenv("LP_IMAGE_DIRECT_URL", DEFAULT_DIRECT_URL).strip()
    if not endpoint:
        raise RuntimeError("未配置 LP_IMAGE_DIRECT_URL")
    prompt = (custom_prompt or DEFAULT_PROMPT).strip()
    reference_url, upload_response = _upload_reference(source_path)
    size = _choose_generation_size(source_path)
    request_payload = {
        "prompt": prompt,
        "size": size,
        "referenceImages": [reference_url],
        "templateCode": os.getenv("LP_IMAGE_TEMPLATE_CODE", "WINDOW_STICKER_MVP"),
        "operatorId": int(os.getenv("LP_OPERATOR_ID", "0") or 0),
        "operatorName": os.getenv("LP_OPERATOR_NAME", "Window Sticker Workbench"),
    }
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
    from io import BytesIO

    Image.open(BytesIO(image_response.content)).convert("RGB").save(master_path)
    artifacts = [
        {"name": "generation-request", "label": "插件生图请求摘要", "path": stage_dir / "request-summary.json", "kind": "json"},
        {"name": "generation-upload", "label": "参考图上传响应", "path": stage_dir / "upload-response.json", "kind": "json"},
        {"name": "generation-response", "label": "插件生图原始响应", "path": stage_dir / "raw-response.json", "kind": "json"},
        {"name": "generation-prompt", "label": "生图 Prompt", "path": stage_dir / "prompt.txt", "kind": "text"},
        {"name": "master", "label": "生成的纯色母版", "path": master_path, "kind": "image"},
    ]
    metadata = {
        "provider": "codex-gpt-image-2-direct",
        "model": data.get("model", "gpt-image-2"),
        "endpoint": endpoint,
        "size_requested": size,
        "size_actual": list(Image.open(master_path).size),
        "duration_ms": data.get("durationMs"),
        "tokens_used": data.get("tokensUsed"),
        "request_id": payload.get("requestId"),
        "internal_request_id": data.get("requestId"),
        "prompt": prompt,
    }
    return master_path, artifacts, metadata


def _generate_master_chat_compat(source_path: Path, job_dir: Path, custom_prompt: str | None = None) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    endpoint = _compat_endpoint()
    token = _compat_token()
    model = os.getenv("LP_IMAGE_MODEL", "gpt-image-2").strip() or "gpt-image-2"
    if not endpoint or not token:
        raise RuntimeError("未配置 LP_AI_BASE_URL 或 LP_AI_TOKEN；可改用“直接上传纯色母版”模式")
    prompt = (custom_prompt or DEFAULT_PROMPT).strip()
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
            "size": "auto",
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
    from io import BytesIO

    Image.open(BytesIO(image_bytes)).convert("RGB").save(master_path)
    artifacts = [
        {"name": "generation-request", "label": "生图请求摘要", "path": request_path, "kind": "json"},
        {"name": "generation-response", "label": "生图原始响应", "path": raw_path, "kind": "json"},
        {"name": "generation-prompt", "label": "生图 Prompt", "path": prompt_path, "kind": "text"},
        {"name": "master", "label": "生成的纯色母版", "path": master_path, "kind": "image"},
    ]
    metadata = {"model": model, "endpoint": endpoint, "prompt": prompt}
    return master_path, artifacts, metadata


def generate_master(source_path: Path, job_dir: Path, custom_prompt: str | None = None) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    if os.getenv("LP_IMAGE_PROVIDER", "chat_compat").strip().lower() == "direct":
        return _generate_master_direct(source_path, job_dir, custom_prompt)
    return _generate_master_chat_compat(source_path, job_dir, custom_prompt)
