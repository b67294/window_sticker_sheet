from __future__ import annotations

import json
import mimetypes
import os
import shutil
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


def _load_local_env(path: Path) -> None:
    """Load the git-ignored project .env without ever returning its secrets to clients."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


_load_local_env(Path(__file__).resolve().with_name(".env"))

import pipeline
from generation import DEFAULT_PROMPT, generate_master, generation_configured
from semantic_grouping import infer_and_apply_semantic_groups, semantic_grouping_configured


APP_DIR = Path(__file__).resolve().parent
RUNS_DIR = APP_DIR / "runs"
STATIC_DIR = APP_DIR / "static"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

app = FastAPI(title="Window Sticker Sheet Workbench", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def job_dir(job_id: str) -> Path:
    return RUNS_DIR / job_id


def save_job(job: dict[str, Any]) -> None:
    with _lock:
        job["updated_at"] = now_iso()
        directory = job_dir(job["id"])
        directory.mkdir(parents=True, exist_ok=True)
        # A fixed job.json.tmp lets concurrent requests overwrite or replace
        # the same temporary file. Windows then raises PermissionError. Keep
        # writes serialized and give every save its own temporary path.
        temporary = directory / f".job-{uuid.uuid4().hex}.tmp"
        target = directory / "job.json"
        try:
            temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            for attempt in range(5):
                try:
                    temporary.replace(target)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.03 * (attempt + 1))
        finally:
            temporary.unlink(missing_ok=True)
        _jobs[job["id"]] = job


def ensure_job_mutable(job: dict[str, Any]) -> None:
    if job.get("status") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="任务运行中，暂时不能修改参数、分组或候选方案")


def load_jobs() -> None:
    for path in RUNS_DIR.glob("*/job.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("status") in {"queued", "running"}:
                job["status"] = "interrupted"
                job["error"] = "服务重启中断了上次运行，可从任一步重新运行"
            _jobs[job["id"]] = job
        except Exception:
            continue


load_jobs()


def require_job(job_id: str) -> dict[str, Any]:
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


def append_log(job: dict[str, Any], message: str) -> None:
    job.setdefault("logs", []).append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
    save_job(job)


def expose_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(job, ensure_ascii=False))
    for item in payload.get("artifacts", []):
        item["url"] = f"/api/jobs/{job['id']}/files/{item['path']}"
    for primitive in payload.get("primitives", []):
        primitive["asset_url"] = f"/api/jobs/{job['id']}/files/{primitive['asset_path']}"
        primitive["mask_url"] = f"/api/jobs/{job['id']}/files/{primitive['mask_path']}"
    for geometry in payload.get("geometry", []):
        geometry["asset_url"] = f"/api/jobs/{job['id']}/files/{geometry['asset_path']}"
    for candidate in payload.get("candidates", []):
        candidate["contact_sheet_url"] = f"/api/jobs/{job['id']}/files/{candidate['contact_sheet_path']}"
        candidate["preview_urls"] = [f"/api/jobs/{job['id']}/files/{path}" for path in candidate.get("preview_paths", [])]
    selected = next((item for item in payload.get("candidates", []) if item.get("id") == payload.get("selected_candidate")), None)
    if selected:
        page_count = int(selected.get("page_count", 0))
        pdf_root = job_dir(job["id"]) / "final" / "pdf"
        combined = pdf_root / "print-sheets.pdf"
        if combined.is_file():
            payload["final_pdf_url"] = f"/api/jobs/{job['id']}/files/final/pdf/print-sheets.pdf"
        payload["final_pdf_page_urls"] = [
            f"/api/jobs/{job['id']}/files/final/pdf/sheet-{index:02d}.pdf"
            for index in range(1, page_count + 1)
            if (pdf_root / f"sheet-{index:02d}.pdf").is_file()
        ]
    payload["download_url"] = f"/api/jobs/{job['id']}/download"
    return payload


def clear_from(job: dict[str, Any], stage: str) -> None:
    index = pipeline.STAGES.index(stage)
    invalid = set(pipeline.STAGES[index:])
    job["artifacts"] = [item for item in job.get("artifacts", []) if item.get("stage") not in invalid]
    if "components" in invalid:
        job["primitives"] = []
        job["groups"] = []
        job["semantic_grouping"] = None
    if "geometry" in invalid:
        job["geometry"] = []
    if "layout" in invalid:
        job["candidates"] = []
        job["selected_candidate"] = None
    job["current_stage"] = pipeline.STAGES[max(0, index - 1)]


def _artifact_from_external(stage: str, item: dict[str, Any], directory: Path) -> dict[str, Any]:
    return pipeline.artifact(stage, item["name"], item["label"], item["path"], directory, item["kind"])


def _master_path(job: dict[str, Any]) -> Path:
    directory = job_dir(job["id"])
    if job["input_mode"] in {"master", "alpha"}:
        return directory / job["uploads"][job["input_mode"]]
    generated = directory / "generate" / "master.png"
    if not generated.exists():
        raise RuntimeError("尚未生成纯色母版")
    return generated


def execute_job(job_id: str, through_stage: str, from_stage: str | None = None) -> None:
    job = require_job(job_id)
    directory = job_dir(job_id)
    settings = pipeline.merge_settings(job.get("settings"))
    job["settings"] = settings
    try:
        job["status"] = "running"
        job["error"] = None
        save_job(job)
        through_index = pipeline.STAGES.index("layout" if through_stage == "all" else through_stage)
        start_index = pipeline.STAGES.index(from_stage) if from_stage else 1

        if job["input_mode"] == "source" and through_index >= pipeline.STAGES.index("generate") and start_index <= pipeline.STAGES.index("generate"):
            append_log(job, "调用内部 gpt-image-2 重建纯色底窗贴母版")
            stage_started = time.perf_counter()
            clear_from(job, "generate")
            source = directory / job["uploads"]["source"]
            master_path, raw_artifacts, metadata = generate_master(source, directory, job.get("generation_prompt"))
            job["generation"] = metadata
            pipeline.replace_stage_artifacts(job, "generate", [_artifact_from_external("generate", item, directory) for item in raw_artifacts])
            job["current_stage"] = "generate"
            save_job(job)
            append_log(job, f"生图阶段完成，用时 {time.perf_counter() - stage_started:.2f}s")

        if through_index >= pipeline.STAGES.index("key") and start_index <= pipeline.STAGES.index("key"):
            if job["input_mode"] == "alpha":
                append_log(job, "保留上传文件原始 Alpha，跳过色键")
            else:
                append_log(job, "采样背景色并生成软 Alpha 蒙版")
            stage_started = time.perf_counter()
            clear_from(job, "key")
            if job["input_mode"] == "alpha":
                artifacts, metrics = pipeline.run_alpha_passthrough(_master_path(job), directory, settings)
            else:
                artifacts, metrics = pipeline.run_chroma_key(_master_path(job), directory, settings)
            pipeline.replace_stage_artifacts(job, "key", artifacts)
            job["key_metrics"] = metrics
            job["current_stage"] = "key"
            save_job(job)
            append_log(job, f"色键阶段完成，用时 {time.perf_counter() - stage_started:.2f}s")

        if through_index >= pipeline.STAGES.index("components") and start_index <= pipeline.STAGES.index("components"):
            append_log(job, "执行连通域分析并生成原始组件")
            stage_started = time.perf_counter()
            clear_from(job, "components")
            artifacts, primitives, groups = pipeline.run_components(job, directory, settings)
            semantic_metadata: dict[str, Any] | None = None
            if settings.get("semantic_grouping_enabled", True) and semantic_grouping_configured():
                semantic_started = time.perf_counter()
                try:
                    semantic_artifacts, groups, semantic_metadata = infer_and_apply_semantic_groups(
                        directory, primitives, groups, settings
                    )
                    artifacts.extend(_artifact_from_external("components", item, directory) for item in semantic_artifacts)
                    (directory / "components" / "groups.json").write_text(
                        json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    job.setdefault("logs", []).append(
                        f"[{datetime.now().strftime('%H:%M:%S')}] 语义分组完成，用时 {time.perf_counter() - semantic_started:.2f}s，应用 {semantic_metadata['applied_count']} 条关系"
                    )
                except Exception as semantic_error:
                    semantic_metadata = {"status": "failed", "error": str(semantic_error)}
                    job.setdefault("logs", []).append(
                        f"[{datetime.now().strftime('%H:%M:%S')}] 语义分组失败，保留距离分组：{semantic_error}"
                    )
            elif settings.get("semantic_grouping_enabled", True):
                semantic_metadata = {"status": "skipped", "reason": "not_configured"}
            pipeline.replace_stage_artifacts(job, "components", artifacts)
            job["primitives"] = primitives
            job["groups"] = groups
            job["semantic_grouping"] = semantic_metadata
            job["current_stage"] = "components"
            save_job(job)
            append_log(job, f"组件阶段完成，用时 {time.perf_counter() - stage_started:.2f}s，共 {len(primitives)} 个 primitive")

        if through_index >= pipeline.STAGES.index("geometry") and start_index <= pipeline.STAGES.index("geometry"):
            append_log(job, "合并组件并生成可见、裁切和占用轮廓")
            stage_started = time.perf_counter()
            clear_from(job, "geometry")
            artifacts, geometry = pipeline.run_geometry(job, directory, settings)
            pipeline.replace_stage_artifacts(job, "geometry", artifacts)
            job["geometry"] = geometry
            job["current_stage"] = "geometry"
            save_job(job)
            append_log(job, f"轮廓阶段完成，用时 {time.perf_counter() - stage_started:.2f}s，共 {len(geometry)} 个组")

        if through_index >= pipeline.STAGES.index("layout") and start_index <= pipeline.STAGES.index("layout"):
            append_log(job, "生成四套候选 Sheet 并计算利用率与视觉平衡")
            stage_started = time.perf_counter()
            clear_from(job, "layout")
            artifacts, candidates, selected = pipeline.run_layout(job, directory, settings)
            pipeline.replace_stage_artifacts(job, "layout", artifacts)
            job["candidates"] = candidates
            job["selected_candidate"] = selected
            job["current_stage"] = "layout"
            save_job(job)
            append_log(job, f"排版阶段完成，用时 {time.perf_counter() - stage_started:.2f}s")

        job["status"] = "complete"
        append_log(job, f"运行完成，当前阶段：{job.get('current_stage')}")
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        append_log(job, f"失败：{exc}")


class RunRequest(BaseModel):
    through_stage: Literal["generate", "key", "components", "geometry", "layout", "all"] = "all"
    from_stage: Literal["generate", "key", "components", "geometry", "layout"] | None = None
    sync: bool = False


class SettingsPatch(BaseModel):
    settings: dict[str, Any]


class GroupPatch(BaseModel):
    action: Literal["merge", "ungroup", "update", "delete", "restore"]
    group_ids: list[str] = Field(default_factory=list)
    group_id: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    import cv2
    import shapely

    return {"ok": True, "opencv": cv2.__version__, "shapely": shapely.__version__, "jobs": len(_jobs)}


@app.get("/api/defaults")
def defaults() -> dict[str, Any]:
    return {
        "settings": pipeline.default_settings(),
        "generation_prompt": DEFAULT_PROMPT,
        "generation_configured": generation_configured(),
        "semantic_grouping_configured": semantic_grouping_configured(),
    }


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    jobs = sorted(_jobs.values(), key=lambda item: item.get("created_at", ""), reverse=True)
    return [
        {
            "id": item["id"],
            "status": item.get("status"),
            "input_mode": item.get("input_mode"),
            "current_stage": item.get("current_stage"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        }
        for item in jobs[:30]
    ]


@app.post("/api/jobs")
async def create_job(
    input_mode: Literal["source", "master", "alpha"] = Form(...),
    file: UploadFile = File(...),
    settings_json: str = Form("{}"),
    generation_prompt: str = Form(DEFAULT_PROMPT),
) -> dict[str, Any]:
    suffix = Path(file.filename or "upload.png").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="仅支持 PNG、JPG、JPEG、WEBP")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件不能超过 20 MB")
    try:
        settings = pipeline.merge_settings(json.loads(settings_json or "{}"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="settings_json 格式错误") from exc
    job_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    directory = job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    upload_name = f"upload-{input_mode}{suffix}"
    upload_path = directory / upload_name
    upload_path.write_bytes(content)
    try:
        from PIL import Image

        with Image.open(upload_path) as image:
            image.verify()
        if input_mode == "alpha":
            with Image.open(upload_path) as image:
                has_alpha = "A" in image.getbands() or image.info.get("transparency") is not None
                if not has_alpha:
                    raise ValueError("没有 Alpha 通道")
                alpha = image.convert("RGBA").getchannel("A")
                alpha_min, alpha_max = alpha.getextrema()
                if alpha_max == 0:
                    raise ValueError("图片完全透明")
                if alpha_min == 255:
                    raise ValueError("Alpha 全部不透明")
    except Exception as exc:
        upload_path.unlink(missing_ok=True)
        detail = f"透明底图片无效：{exc}" if input_mode == "alpha" else "上传文件不是有效图片"
        raise HTTPException(status_code=400, detail=detail) from exc
    created = now_iso()
    job = {
        "id": job_id,
        "status": "ready",
        "input_mode": input_mode,
        "uploads": {input_mode: upload_name},
        "settings": settings,
        "generation_prompt": generation_prompt.strip() or DEFAULT_PROMPT,
        "generation": None,
        "key_metrics": None,
        "semantic_grouping": None,
        "primitives": [],
        "groups": [],
        "geometry": [],
        "candidates": [],
        "selected_candidate": None,
        "current_stage": "input",
        "artifacts": [pipeline.artifact(
            "input",
            "upload",
            {"source": "上传的电商图", "master": "上传的纯色母版", "alpha": "上传的透明底母版"}[input_mode],
            upload_path,
            directory,
        )],
        "logs": [f"[{datetime.now().strftime('%H:%M:%S')}] 创建任务，输入模式：{input_mode}"],
        "error": None,
        "created_at": created,
        "updated_at": created,
    }
    save_job(job)
    return expose_job(job)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    return expose_job(require_job(job_id))


@app.post("/api/jobs/{job_id}/run")
def run_job(job_id: str, request: RunRequest) -> dict[str, Any]:
    job = require_job(job_id)
    if job.get("status") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="任务正在运行")
    if request.from_stage and pipeline.STAGES.index(request.from_stage) > pipeline.STAGES.index("layout" if request.through_stage == "all" else request.through_stage):
        raise HTTPException(status_code=400, detail="from_stage 不能晚于 through_stage")
    if request.sync:
        execute_job(job_id, request.through_stage, request.from_stage)
    else:
        job["status"] = "queued"
        save_job(job)
        threading.Thread(target=execute_job, args=(job_id, request.through_stage, request.from_stage), daemon=True).start()
    return expose_job(require_job(job_id))


@app.patch("/api/jobs/{job_id}/settings")
def update_settings(job_id: str, patch: SettingsPatch) -> dict[str, Any]:
    job = require_job(job_id)
    ensure_job_mutable(job)
    old = pipeline.merge_settings(job.get("settings"))
    new = pipeline.merge_settings({**old, **patch.settings})
    job["settings"] = new
    key_fields = {"key_low", "key_high", "morph_kernel", "min_component_area", "alpha_threshold"}
    component_fields = {
        "install_width_mm", "install_height_mm", "group_gap_mm",
        "semantic_grouping_enabled", "semantic_min_confidence",
    }
    geometry_fields = {"cut_offset_mm", "spacing_mm", "simplify_mm"}
    changed = {key for key in new if new.get(key) != old.get(key)}
    if job.get("input_mode") == "alpha":
        changed -= {"key_low", "key_high", "morph_kernel"}
        key_fields = set()
        component_fields |= {"min_component_area", "alpha_threshold"}
    if changed & key_fields:
        clear_from(job, "key")
    elif changed & component_fields:
        clear_from(job, "components")
    elif changed & geometry_fields:
        clear_from(job, "geometry")
    elif changed:
        clear_from(job, "layout")
    append_log(job, "更新参数：" + ", ".join(sorted(changed)) if changed else "参数未变化")
    return expose_job(job)


def _next_group_id(groups: list[dict[str, Any]]) -> str:
    used = {item["id"] for item in groups}
    index = 1
    while f"gm{index:03d}" in used:
        index += 1
    return f"gm{index:03d}"


@app.patch("/api/jobs/{job_id}/groups")
def update_groups(job_id: str, patch: GroupPatch) -> dict[str, Any]:
    job = require_job(job_id)
    ensure_job_mutable(job)
    groups = job.get("groups", [])
    by_id = {item["id"]: item for item in groups}
    target_id = patch.group_id or (patch.group_ids[0] if patch.group_ids else None)
    if patch.action == "merge":
        selected = [by_id[item] for item in patch.group_ids if item in by_id and by_id[item].get("active", True)]
        if len(selected) < 2:
            raise HTTPException(status_code=400, detail="至少选择两个有效分组")
        primitive_ids = sorted({primitive for group in selected for primitive in group["primitive_ids"]})
        for group in selected:
            group["active"] = False
        boxes = [item["bbox"] for item in selected]
        x0 = min(item[0] for item in boxes)
        y0 = min(item[1] for item in boxes)
        x1 = max(item[0] + item[2] for item in boxes)
        y1 = max(item[1] + item[3] for item in boxes)
        groups.append(
            {
                "id": _next_group_id(groups),
                "primitive_ids": primitive_ids,
                "bbox": [x0, y0, x1 - x0, y1 - y0],
                "active": True,
                "rotatable": False,
                "filler": False,
                "max_copies": 2,
                "origin": "manual-merge",
            }
        )
    elif patch.action == "ungroup":
        if not target_id or target_id not in by_id:
            raise HTTPException(status_code=404, detail="分组不存在")
        target = by_id[target_id]
        target["active"] = False
        primitive_map = {item["id"]: item for item in job.get("primitives", [])}
        for primitive_id in target["primitive_ids"]:
            primitive = primitive_map.get(primitive_id)
            if not primitive:
                continue
            existing = next((item for item in groups if item.get("primitive_ids") == [primitive_id] and item.get("origin") == "manual-split"), None)
            if existing:
                existing["active"] = True
                continue
            groups.append(
                {
                    "id": _next_group_id(groups),
                    "primitive_ids": [primitive_id],
                    "bbox": list(primitive["bbox"]),
                    "active": True,
                    "rotatable": False,
                    "filler": False,
                    "max_copies": 2,
                    "origin": "manual-split",
                }
            )
    elif patch.action in {"delete", "restore"}:
        if not target_id or target_id not in by_id:
            raise HTTPException(status_code=404, detail="分组不存在")
        by_id[target_id]["active"] = patch.action == "restore"
    elif patch.action == "update":
        if not target_id or target_id not in by_id:
            raise HTTPException(status_code=404, detail="分组不存在")
        allowed = {"rotatable", "filler", "max_copies", "active"}
        for key, value in patch.values.items():
            if key in allowed:
                by_id[target_id][key] = max(0, min(10, int(value))) if key == "max_copies" else bool(value)
    job["groups"] = groups
    clear_from(job, "geometry")
    groups_path = job_dir(job_id) / "components" / "groups.json"
    groups_path.parent.mkdir(parents=True, exist_ok=True)
    groups_path.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")
    append_log(job, f"分组操作：{patch.action}")
    return expose_job(job)


@app.post("/api/jobs/{job_id}/candidates/{candidate_id}/select")
def select_candidate(job_id: str, candidate_id: str) -> dict[str, Any]:
    job = require_job(job_id)
    ensure_job_mutable(job)
    candidate = next((item for item in job.get("candidates", []) if item["id"] == candidate_id), None)
    if not candidate:
        raise HTTPException(status_code=404, detail="候选方案不存在")
    outputs = pipeline.render_selected_outputs(candidate, job.get("geometry", []), job_dir(job_id), job["settings"])
    selected_names = {"selected-transparent", "selected-white", "selected-pdf"}
    job["artifacts"] = [
        item for item in job.get("artifacts", [])
        if item.get("name") not in selected_names and not item.get("name", "").startswith("selected-pdf-page-")
    ]
    job["artifacts"].extend(pipeline.selected_output_artifacts(outputs, job_dir(job_id), "手动选中方案"))
    job["selected_candidate"] = candidate_id
    append_log(job, f"手动选择候选方案：{candidate_id}；已生成 {len(outputs['page_pdfs'])} 张单页 PDF 和 1 份多页 PDF")
    return expose_job(job)


@app.get("/api/jobs/{job_id}/files/{file_path:path}")
def get_file(job_id: str, file_path: str) -> FileResponse:
    require_job(job_id)
    root = job_dir(job_id).resolve()
    target = (root / file_path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    media_type, _ = mimetypes.guess_type(target.name)
    return FileResponse(target, media_type=media_type)


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str) -> FileResponse:
    require_job(job_id)
    root = job_dir(job_id)
    archive = root / f"{job_id}-artifacts.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in root.rglob("*"):
            if path.is_file() and path != archive and not path.name.endswith(".tmp"):
                output.write(path, path.relative_to(root))
    return FileResponse(archive, media_type="application/zip", filename=archive.name)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8790, reload=False)
