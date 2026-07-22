from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import requests


SYSTEM_PROMPT = """你是窗贴生产文件的语义分组器。你只能根据完整母版和带编号覆盖图判断哪些 primitive 在业务上必须保持原始相对位置。
只合并明确属于同一文字短语、同一不可拆构图或主体与附属物的组件。重复装饰、风格相似但可独立安装的元素不要合并。
返回严格 JSON，不要 Markdown，不要解释性前后缀，不要编造不存在的 primitive id。"""


USER_PROMPT = """第一张图是完整透明/去底母版，第二张图是在同一母版上标注 primitive 边框和 pXXX 编号的覆盖图。
请返回必须作为刚性组整体排版的关系。JSON 格式：
{
  "semantic_groups": [
    {
      "members": ["p001", "p002"],
      "mode": "rigid",
      "preserve_relative_layout": true,
      "cut_mode": "separate",
      "confidence": 0.0,
      "reason": "简短原因"
    }
  ]
}
没有可靠关系时返回 {"semantic_groups": []}。只输出 JSON。"""


def semantic_grouping_configured() -> bool:
    endpoint = os.getenv("LP_VISION_BASE_URL") or os.getenv("LP_COMPAT_BASE_URL")
    token = os.getenv("LP_VISION_TOKEN") or os.getenv("LP_COMPAT_TOKEN")
    return bool(endpoint and token)


def _data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(suffix, "image/png")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _extract_json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(value):
        if character != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("多模态模型响应中没有可解析的 JSON 对象")


def _next_semantic_group_id(groups: list[dict[str, Any]]) -> str:
    used = {item["id"] for item in groups}
    index = 1
    while f"gs{index:03d}" in used:
        index += 1
    return f"gs{index:03d}"


def apply_semantic_relations(
    primitives: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    relations: dict[str, Any],
    min_confidence: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    primitive_ids = {item["id"] for item in primitives}
    applied: list[dict[str, Any]] = []
    consumed_groups: set[str] = set()
    for relation_index, relation in enumerate(relations.get("semantic_groups", []), start=1):
        if not isinstance(relation, dict) or relation.get("mode", "rigid") != "rigid":
            continue
        confidence = float(relation.get("confidence", 0.0) or 0.0)
        members = list(dict.fromkeys(item for item in relation.get("members", []) if item in primitive_ids))
        if confidence < min_confidence or len(members) < 2:
            continue
        selected = [
            group for group in groups
            if group.get("active", True)
            and not consumed_groups.intersection({group["id"]})
            and any(member in group.get("primitive_ids", []) for member in members)
        ]
        covered = {primitive for group in selected for primitive in group.get("primitive_ids", [])}
        if not set(members).issubset(covered) or not selected:
            continue
        relation_record = {
            "source_relation_index": relation_index,
            "requested_members": members,
            "effective_members": sorted(covered),
            "confidence": confidence,
            "reason": str(relation.get("reason", ""))[:500],
            "mode": "rigid",
            "preserve_relative_layout": bool(relation.get("preserve_relative_layout", True)),
            "cut_mode": relation.get("cut_mode", "separate"),
        }
        if len(selected) == 1:
            selected[0].update({
                "origin": "semantic",
                "semantic": relation_record,
            })
            relation_record["applied_group_id"] = selected[0]["id"]
            consumed_groups.add(selected[0]["id"])
            applied.append(relation_record)
            continue
        boxes = [item["bbox"] for item in selected]
        x0 = min(item[0] for item in boxes)
        y0 = min(item[1] for item in boxes)
        x1 = max(item[0] + item[2] for item in boxes)
        y1 = max(item[1] + item[3] for item in boxes)
        for group in selected:
            group["active"] = False
            consumed_groups.add(group["id"])
        group_id = _next_semantic_group_id(groups)
        relation_record["applied_group_id"] = group_id
        groups.append({
            "id": group_id,
            "primitive_ids": sorted(covered),
            "bbox": [x0, y0, x1 - x0, y1 - y0],
            "active": True,
            "rotatable": False,
            "filler": False,
            "max_copies": 0,
            "origin": "semantic",
            "semantic": relation_record,
        })
        applied.append(relation_record)
    return groups, applied


def infer_and_apply_semantic_groups(
    job_dir: Path,
    primitives: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    endpoint = (os.getenv("LP_VISION_BASE_URL") or os.getenv("LP_COMPAT_BASE_URL") or "").strip()
    token = (os.getenv("LP_VISION_TOKEN") or os.getenv("LP_COMPAT_TOKEN") or "").strip()
    model = os.getenv("LP_VISION_MODEL", "codex-gpt-5.6-luna").strip() or "codex-gpt-5.6-luna"
    if not endpoint or not token:
        raise RuntimeError("未配置 LP_VISION_BASE_URL 或 LP_VISION_TOKEN")
    clean_path = job_dir / "key" / "foreground.png"
    overlay_path = job_dir / "components" / "components-overlay.png"
    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": USER_PROMPT},
                    {"type": "image_url", "image_url": {"url": _data_url(clean_path)}},
                    {"type": "image_url", "image_url": {"url": _data_url(overlay_path)}},
                ],
            },
        ],
        "stream": False,
    }
    started = time.perf_counter()
    response = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=request_payload,
        timeout=(30, 900),
    )
    duration = time.perf_counter() - started
    if response.status_code >= 400:
        raise RuntimeError(f"语义分组接口返回 HTTP {response.status_code}: {response.text[:2000]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("语义分组接口没有返回 JSON") from exc
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("语义分组接口响应缺少 choices[0].message.content") from exc
    relations = _extract_json_object(content)
    groups, applied = apply_semantic_relations(
        primitives,
        groups,
        relations,
        float(settings.get("semantic_min_confidence", 0.9)),
    )

    stage_dir = job_dir / "components" / "semantic"
    stage_dir.mkdir(parents=True, exist_ok=True)
    request_summary = json.loads(json.dumps(request_payload, ensure_ascii=False))
    request_summary["messages"][1]["content"][1]["image_url"]["url"] = f"<data-url:{clean_path.name}:{clean_path.stat().st_size} bytes>"
    request_summary["messages"][1]["content"][2]["image_url"]["url"] = f"<data-url:{overlay_path.name}:{overlay_path.stat().st_size} bytes>"
    paths = {
        "request": stage_dir / "request-summary.json",
        "response": stage_dir / "raw-response.json",
        "relations": stage_dir / "relations.json",
        "application": stage_dir / "application.json",
    }
    paths["request"].write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["response"].write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["relations"].write_text(json.dumps(relations, ensure_ascii=False, indent=2), encoding="utf-8")
    application = {"applied": applied, "min_confidence": float(settings.get("semantic_min_confidence", 0.9))}
    paths["application"].write_text(json.dumps(application, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = [
        {"name": "semantic-request", "label": "语义分组请求摘要", "path": paths["request"], "kind": "json"},
        {"name": "semantic-response", "label": "语义分组原始响应", "path": paths["response"], "kind": "json"},
        {"name": "semantic-relations", "label": "视觉模型关系 JSON", "path": paths["relations"], "kind": "json"},
        {"name": "semantic-application", "label": "语义关系应用结果", "path": paths["application"], "kind": "json"},
    ]
    metadata = {
        "status": "complete",
        "model": model,
        "endpoint": endpoint,
        "duration_seconds": round(duration, 3),
        "response_id": payload.get("id") if isinstance(payload, dict) else None,
        "relation_count": len(relations.get("semantic_groups", [])),
        "applied_count": len(applied),
    }
    return artifacts, groups, metadata
