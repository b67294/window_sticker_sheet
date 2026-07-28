from __future__ import annotations

import json
import os
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from PIL import Image

from generation import upload_image_to_cloud


APP_DIR = Path(__file__).resolve().parent
DEFAULT_BASE_URL = "http://127.0.0.1:6070"
DEFAULT_WORKFLOW_PATH = APP_DIR / "comfyui" / "BiRefNet.json"


def _base_url() -> str:
    return (os.getenv("LP_COMFYUI_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/") + "/"


def _workflow_path() -> Path:
    configured = (os.getenv("LP_COMFYUI_WORKFLOW") or "").strip()
    return Path(configured) if configured else DEFAULT_WORKFLOW_PATH


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = (os.getenv("LP_COMFYUI_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def comfyui_configured() -> bool:
    return bool(_base_url()) and _workflow_path().is_file()


def _replace_template(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        result = value
        for key, replacement in variables.items():
            result = result.replace(f"#{{{key}}}", replacement)
        return result
    if isinstance(value, list):
        return [_replace_template(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _replace_template(item, variables) for key, item in value.items()}
    return value


def _history_entry(payload: Any, prompt_id: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    entry = payload.get(prompt_id)
    if isinstance(entry, dict):
        return entry
    if "outputs" in payload or "status" in payload:
        return payload
    return None


def _result_image(
    workflow: dict[str, Any], history: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    outputs = history.get("outputs") or {}
    save_nodes = [
        node_id
        for node_id, node in workflow.items()
        if isinstance(node, dict) and node.get("class_type") == "SaveImage"
    ]
    ordered_ids = save_nodes + [str(node_id) for node_id in outputs if str(node_id) not in save_nodes]
    for node_id in ordered_ids:
        output = outputs.get(node_id) or outputs.get(str(node_id)) or {}
        images = output.get("images") if isinstance(output, dict) else None
        if images and isinstance(images[0], dict) and images[0].get("filename"):
            return node_id, images[0]
    raise RuntimeError("ComfyUI 已完成，但 history.outputs 中没有找到 SaveImage 图片")


def remove_background(
    source_path: Path, job_dir: Path
) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    """Upload an opaque master, run the configured ComfyUI API workflow and download RGBA."""
    workflow_path = _workflow_path()
    if not workflow_path.is_file():
        raise RuntimeError(f"找不到 ComfyUI 工作流：{workflow_path}")

    stage_dir = job_dir / "comfyui"
    stage_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    image_url, upload_response = upload_image_to_cloud(source_path)
    workflow_template = json.loads(workflow_path.read_text(encoding="utf-8-sig"))
    workflow = _replace_template(workflow_template, {"image": image_url})
    unresolved = json.dumps(workflow, ensure_ascii=False)
    if "#{" in unresolved:
        raise RuntimeError("ComfyUI 工作流仍包含未替换的模板变量")

    client_id = uuid.uuid4().hex
    prompt_request = {
        "prompt": workflow,
        "client_id": client_id,
        "extra_data": {"window_sticker_source_url": image_url},
    }
    base_url = _base_url()
    response = requests.post(
        urljoin(base_url, "prompt"),
        headers=_headers(),
        json=prompt_request,
        timeout=(15, 60),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"ComfyUI /prompt 返回 HTTP {response.status_code}: {response.text[:2000]}")
    try:
        prompt_payload = response.json()
    except ValueError as exc:
        raise RuntimeError("ComfyUI /prompt 没有返回 JSON") from exc
    prompt_id = prompt_payload.get("prompt_id") if isinstance(prompt_payload, dict) else None
    if not prompt_id:
        errors = prompt_payload.get("node_errors") if isinstance(prompt_payload, dict) else None
        raise RuntimeError(f"ComfyUI 未返回 prompt_id；node_errors={errors}")

    timeout_seconds = float(os.getenv("LP_COMFYUI_TIMEOUT_SECONDS", "600"))
    poll_seconds = max(0.1, float(os.getenv("LP_COMFYUI_POLL_SECONDS", "1")))
    deadline = time.monotonic() + timeout_seconds
    history_payload: dict[str, Any] = {}
    history: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        history_response = requests.get(
            urljoin(base_url, f"history/{prompt_id}"),
            headers=_headers(),
            timeout=(10, 30),
        )
        if history_response.status_code >= 400:
            raise RuntimeError(
                f"ComfyUI /history 返回 HTTP {history_response.status_code}: "
                f"{history_response.text[:1000]}"
            )
        try:
            history_payload = history_response.json()
        except ValueError as exc:
            raise RuntimeError("ComfyUI /history 没有返回 JSON") from exc
        history = _history_entry(history_payload, prompt_id)
        if history:
            status = history.get("status") or {}
            completed = bool(status.get("completed"))
            if completed or history.get("outputs"):
                if status.get("status_str") not in {None, "", "success"}:
                    messages = status.get("messages") or history.get("error")
                    raise RuntimeError(f"ComfyUI 执行失败：{messages or status}")
                break
        time.sleep(poll_seconds)
    else:
        raise TimeoutError(f"等待 ComfyUI 超时（{timeout_seconds:g} 秒），prompt_id={prompt_id}")

    assert history is not None
    output_node, image_info = _result_image(workflow, history)
    view_response = requests.get(
        urljoin(base_url, "view"),
        headers=_headers(),
        params={
            "filename": image_info["filename"],
            "subfolder": image_info.get("subfolder", ""),
            "type": image_info.get("type", "output"),
        },
        timeout=(15, 120),
    )
    if view_response.status_code >= 400:
        raise RuntimeError(f"ComfyUI /view 返回 HTTP {view_response.status_code}: {view_response.text[:1000]}")
    try:
        with Image.open(BytesIO(view_response.content)) as image:
            rgba = image.convert("RGBA")
    except Exception as exc:
        raise RuntimeError("ComfyUI /view 返回的内容不是有效图片") from exc
    alpha_min, alpha_max = rgba.getchannel("A").getextrema()
    if alpha_max == 0:
        raise RuntimeError("ComfyUI 输出完全透明")
    if alpha_min == 255:
        raise RuntimeError("ComfyUI 输出没有透明背景，请检查工作流 SaveImage 是否连接透明图输出")

    result_path = stage_dir / "transparent.png"
    rgba.save(result_path)
    upload_path = stage_dir / "qiniu-upload-response.json"
    prompt_path = stage_dir / "prompt-request.json"
    response_path = stage_dir / "prompt-response.json"
    history_path = stage_dir / "history.json"
    upload_path.write_text(json.dumps(upload_response, ensure_ascii=False, indent=2), encoding="utf-8")
    prompt_path.write_text(json.dumps(prompt_request, ensure_ascii=False, indent=2), encoding="utf-8")
    response_path.write_text(json.dumps(prompt_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    history_path.write_text(json.dumps(history_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    artifacts = [
        {"name": "qiniu-upload", "label": "白底母版七牛上传响应", "path": upload_path, "kind": "json"},
        {"name": "comfyui-request", "label": "ComfyUI 工作流请求", "path": prompt_path, "kind": "json"},
        {"name": "comfyui-response", "label": "ComfyUI 提交响应", "path": response_path, "kind": "json"},
        {"name": "comfyui-history", "label": "ComfyUI 执行结果", "path": history_path, "kind": "json"},
        {"name": "comfyui-transparent", "label": "ComfyUI 透明底结果", "path": result_path, "kind": "image"},
    ]
    metadata = {
        "base_url": base_url.rstrip("/"),
        "workflow": str(workflow_path),
        "source_url": image_url,
        "prompt_id": prompt_id,
        "output_node": output_node,
        "output_image": image_info,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "alpha_range": [alpha_min, alpha_max],
    }
    return result_path, artifacts, metadata
