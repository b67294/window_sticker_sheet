"""VLM 图→文→图 创新链路独立测试脚本（不依赖主工程代码）。

用法:
    python run.py <参考图路径> [--size 1216x1216] [--skip-generate]

流程:
    1. 参考图压缩为 WebP data URL；
    2. VLM（codex-gpt-5.6-luna，失败重试 2 次，再依次回退 sol / terra / gpt-4o）
       按 innovation_prompt.md 反推并产出 3 条衍生生图提示词；
    3. 每条提示词追加固定生产约束后，调 generateImageDirect 纯文字生图；
    4. 全部 VLM 模型失败时，兜底：默认创新提示词 + 参考图 图生图。
输出: runs/<时间戳>/ 下 brief.md、prompts.json、derived-*.png、summary.json。
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

HERE = Path(__file__).resolve().parent
WORKBENCH_ENV = HERE.parent.parent / ".env"

CHAT_URL = "https://test-plugin.longpean.com/v1/chat/completions"
IMAGE_URL = "https://gptapi.longpean.com/gptImage/generateImageDirect"
UPLOAD_URL = "https://stpic.longpean.com/picture/upLoadQiNiu"

# luna 重试 2 次（共 3 attempts），然后依次回退。
# 与 brief_lab.DEFAULT_VLM_CHAIN 保持一致：2026-08-06 起 codex-gpt-5.6-* 网关侧 502。
VLM_CHAIN = ["codex-gpt-5.5", "codex-gpt-5.5", "gpt-4o", "gpt-4o", "codex-gpt-5.6-luna"]

# 追加到每条衍生提示词后的固定生产约束（生图模型看不到原图，也看不到主工程 prompt）。
PRODUCTION_SUFFIX = (
    "\n\n输出要求：一张正视二维平面的窗贴图案母版。背景完全均匀纯白（#ffffff），"
    "无渐变、纹理、光照或阴影；图案边缘清晰，无白色描边或光晕。"
    "不绘制窗框、玻璃、墙面、实景环境、展示样机或商品照片。"
    "各元素之间保留清晰纯白间隔，画布四周保留纯白安全边距，元素不接触画布边缘。"
)

# 全部 VLM 失败时的兜底：默认创新提示词 + 参考图 图生图。
FALLBACK_PROMPT = (
    "参考输入图中的图案主题与画风，重新设计一款同系列但差异明显的全新窗贴图案。"
    "保留主角类型、节日氛围和整体风格方向，但主体造型、动作姿态、道具组合、背景元素、"
    "次级配色和构图必须至少四处明显不同，像同一系列的另一款产品而不是原图改版。"
    "画面要有明确大小层级：一至两个大型主视觉、若干中型次级组合、少量小型点缀。"
    "不得引入原图中不存在的其他文化体系元素；角色肢体结构必须正确。"
    + PRODUCTION_SUFFIX
)


def load_token() -> str:
    if WORKBENCH_ENV.is_file():
        for line in WORKBENCH_ENV.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("LP_COMPAT_TOKEN=") or line.startswith("LP_VISION_TOKEN="):
                value = line.split("=", 1)[1].strip().strip('"\'')
                if value:
                    return value
    raise SystemExit(f"未找到 LP_COMPAT_TOKEN，请检查 {WORKBENCH_ENV}")


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


def call_vlm(token: str, prompt: str, data_url: str, log) -> tuple[str, str] | None:
    """返回 (model, content)；全链路失败返回 None。"""
    for index, model in enumerate(VLM_CHAIN, start=1):
        try:
            log(f"[VLM] attempt {index}/{len(VLM_CHAIN)} model={model}")
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
        except Exception as exc:  # noqa: BLE001 - 测试脚本，逐级回退
            log(f"[VLM] {model} 失败: {exc}")
            time.sleep(3)
    return None


def parse_prompts(content: str) -> list[dict[str, str]]:
    directions = re.findall(r"【衍生方向 (\d)】\s*(.*?)\s*【生图提示词 \1】", content, re.S)
    prompts = re.findall(r"【生图提示词 (\d)】\s*(.*?)(?=【衍生方向|\Z)", content, re.S)
    direction_map = {number: text.strip() for number, text in directions}
    return [
        {"index": number, "direction": direction_map.get(number, ""), "prompt": text.strip()}
        for number, text in prompts
        if text.strip()
    ]


def upload_reference(path: Path, log) -> str:
    log("[上传] 参考图 → 图云（兜底图生图用）")
    response = requests.post(
        UPLOAD_URL,
        headers={"Content-Type": "application/json"},
        json={"picBytes": list(path.read_bytes()), "fileName": f"vlm-brief-{uuid.uuid4().hex}{path.suffix}"},
        timeout=(30, 180),
    )
    response.raise_for_status()
    url = response.json().get("data")
    if not isinstance(url, str) or not url.startswith("http"):
        raise RuntimeError(f"图云上传响应异常: {response.text[:300]}")
    return url


def generate_image(prompt: str, size: str, out_path: Path, log, reference_url: str | None = None) -> dict:
    payload = {
        "prompt": prompt,
        "size": size,
        "templateCode": "VLM_BRIEF_TEST",
        "operatorName": "vlm-brief-experiment",
    }
    if reference_url:
        payload["referenceImages"] = [reference_url]
    for attempt in range(1, 4):
        try:
            log(f"[生图] {out_path.name} attempt {attempt} (size={size}, ref={'有' if reference_url else '无'})")
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--size", default="1216x1216")
    parser.add_argument("--skip-generate", action="store_true", help="只跑 VLM 反推，不生图")
    args = parser.parse_args()
    if not args.image.is_file():
        raise SystemExit(f"找不到图片: {args.image}")

    token = load_token()
    run_dir = HERE / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "log.txt"

    def log(message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    source_copy = run_dir / f"source{args.image.suffix}"
    source_copy.write_bytes(args.image.read_bytes())
    log(f"输入: {args.image}")

    innovation_prompt = (HERE.parent.parent / "prompts" / "brief" / "innovation.md").read_text(encoding="utf-8-sig")
    data_url = image_data_url(args.image)
    vlm_result = call_vlm(token, innovation_prompt, data_url, log)

    summary: dict = {"source": str(args.image), "size": args.size, "results": []}
    if vlm_result:
        model, content = vlm_result
        (run_dir / "brief.md").write_text(content, encoding="utf-8")
        prompts = parse_prompts(content)
        log(f"[VLM] 成功 model={model}，解析出 {len(prompts)} 条提示词")
        summary["vlm_model"] = model
        summary["prompts"] = prompts
        (run_dir / "prompts.json").write_text(
            json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not args.skip_generate:
            for item in prompts[:3]:
                out = run_dir / f"derived-{item['index']}.png"
                result = generate_image(item["prompt"] + PRODUCTION_SUFFIX, args.size, out, log)
                result["index"] = item["index"]
                result["direction"] = item["direction"]
                summary["results"].append(result)
                log(f"[生图] 方向{item['index']} → {'成功' if result['ok'] else '失败'}")
    else:
        log("[VLM] 全部模型失败，走兜底：默认创新提示词 + 参考图 图生图")
        summary["vlm_model"] = None
        summary["fallback"] = True
        if not args.skip_generate:
            reference_url = upload_reference(source_copy, log)
            for index in range(1, 4):
                out = run_dir / f"derived-fallback-{index}.png"
                result = generate_image(FALLBACK_PROMPT, args.size, out, log, reference_url=reference_url)
                result["index"] = str(index)
                summary["results"].append(result)

    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ok = sum(1 for item in summary["results"] if item.get("ok"))
    log(f"完成: {run_dir}（成功出图 {ok}/{len(summary['results'])}）")


if __name__ == "__main__":
    main()
