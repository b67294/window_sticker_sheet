from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
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
from comfyui_client import comfyui_configured, remove_background
from generation import DEFAULT_PROMPT, generate_master, generation_configured, render_generation_prompt
from semantic_grouping import infer_and_apply_semantic_groups, semantic_grouping_configured
from window_templates import (
    DEFAULT_WINDOW_TEMPLATE,
    TEMPLATE_CONSTRAINT_VERSION,
    aspect_ratio_warning,
    get_window_template,
    public_window_templates,
)


APP_DIR = Path(__file__).resolve().parent
RUNS_DIR = APP_DIR / "runs"
STATIC_DIR = APP_DIR / "static"
REVIEWS_DIR = APP_DIR / "reviews"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

app = FastAPI(title="Window Sticker Sheet Workbench", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_jobs: dict[str, dict[str, Any]] = {}
_batches: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def job_dir(job_id: str) -> Path:
    return RUNS_DIR / job_id


def batches_dir() -> Path:
    return RUNS_DIR / "_batches"


def batch_dir(batch_id: str) -> Path:
    return batches_dir() / batch_id


def delivery_dir(batch_id: str) -> Path:
    return batch_dir(batch_id) / "delivery"


def _save_json_with_unique_temp(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.stem}-{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for attempt in range(7):
            try:
                temporary.replace(target)
                return
            except PermissionError:
                if attempt == 6:
                    raise
                time.sleep(min(0.5, 0.03 * (2**attempt)))
    finally:
        temporary.unlink(missing_ok=True)


def save_job(job: dict[str, Any]) -> None:
    with _lock:
        job["updated_at"] = now_iso()
        directory = job_dir(job["id"])
        _save_json_with_unique_temp(directory / "job.json", job)
        _jobs[job["id"]] = job


def save_batch(batch: dict[str, Any]) -> None:
    with _lock:
        batch["updated_at"] = now_iso()
        _save_json_with_unique_temp(
            batch_dir(batch["id"]) / "batch.json",
            batch,
        )
        _batches[batch["id"]] = batch


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
            settings = job.setdefault("settings", {})
            if "window_template" not in settings:
                # In-memory compatibility marker only: do not rewrite historical files.
                settings["window_template"] = "legacy"
            _jobs[job["id"]] = job
        except Exception:
            continue


load_jobs()


def load_batches() -> None:
    for path in batches_dir().glob("*/batch.json"):
        try:
            batch = json.loads(path.read_text(encoding="utf-8"))
            if batch.get("status") in {"queued", "running"}:
                batch["status"] = "interrupted"
                batch["error"] = "服务重启中断了上次批量调度，可重试未完成项"
                _save_json_with_unique_temp(path, batch)
            _batches[batch["id"]] = batch
        except Exception:
            continue


load_batches()


def require_job(job_id: str) -> dict[str, Any]:
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


def require_batch(batch_id: str) -> dict[str, Any]:
    with _lock:
        batch = _batches.get(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


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


def _safe_delivery_stem(value: str, fallback: str) -> str:
    stem = re.sub(r"[^\w.-]+", "-", Path(value).stem).strip("-.")
    return stem or fallback


def _ensure_delivery_state(batch: dict[str, Any]) -> dict[str, Any]:
    delivery = batch.setdefault("delivery", {})
    render_pdf = bool(delivery.get("renderPdf", True))
    delivery.setdefault("renderPdf", render_pdf)
    if "pdfStatus" not in delivery:
        completed = [
            _jobs.get(item.get("job_id"))
            for item in batch.get("items", [])
            if _jobs.get(item.get("job_id"), {}).get("status") == "complete"
        ]
        all_have_pdf = bool(completed) and all(
            (
                job_dir(job["id"])
                / "final"
                / "pdf"
                / "print-sheets.pdf"
            ).is_file()
            for job in completed
        )
        delivery["pdfStatus"] = (
            "ready"
            if render_pdf and all_have_pdf
            else "pending"
            if render_pdf
            else "skipped"
        )
    delivery.setdefault("generatedAt", None)
    delivery.setdefault("files", [])
    delivery.setdefault("error", None)
    return delivery


def _selected_candidate(job: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (
            candidate
            for candidate in job.get("candidates", [])
            if candidate.get("id") == job.get("selected_candidate")
        ),
        None,
    )


def _ensure_guide_free_job_outputs(
    job: dict[str, Any],
    *,
    render_pdf: bool,
) -> None:
    """One-time migration for finals created before guide-free delivery rendering."""
    directory = job_dir(job["id"])
    manifest_path = directory / "final" / "layout.json"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    if int(manifest.get("delivery_render_version", 0)) >= 2:
        return
    candidate = _selected_candidate(job)
    if not candidate or not job.get("geometry"):
        raise RuntimeError("历史任务缺少候选或几何数据，无法重建无辅助线交付图")
    outputs = pipeline.render_selected_outputs(
        candidate,
        job["geometry"],
        directory,
        pipeline.merge_settings(job.get("settings")),
        render_pdf=render_pdf,
    )
    selected_names = {"selected-transparent", "selected-white", "selected-pdf"}
    job["artifacts"] = [
        item
        for item in job.get("artifacts", [])
        if item.get("name") not in selected_names
        and not item.get("name", "").startswith("selected-pdf-page-")
    ]
    job["artifacts"].extend(
        pipeline.selected_output_artifacts(
            outputs,
            directory,
            "交付升级方案",
        )
    )
    job["render_pdf"] = render_pdf
    job["pdf_status"] = (
        "ready"
        if outputs.get("combined_pdf")
        else "failed"
        if outputs.get("pdf_error")
        else "skipped"
    )
    job["pdf_error"] = outputs.get("pdf_error")
    job.setdefault("logs", []).append(
        f"[{datetime.now().strftime('%H:%M:%S')}] 已重建无辅助线产品交付图"
    )
    save_job(job)


def assemble_delivery(batch: dict[str, Any]) -> dict[str, Any]:
    """Build the product-facing PNG/PDF folders from completed child jobs."""
    root = delivery_dir(batch["id"])
    resolved_root = root.resolve()
    expected_root = batch_dir(batch["id"]).resolve()
    if expected_root not in resolved_root.parents:
        raise RuntimeError("交付目录超出批次目录")
    if root.exists():
        shutil.rmtree(root)
    png_dir = root / "PNG"
    pdf_dir = root / "PDF"
    png_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    manifest_items: list[dict[str, Any]] = []
    copied_files: list[str] = []
    completed_jobs = 0
    missing_pdfs: list[str] = []
    for index, item in enumerate(batch.get("items", []), start=1):
        job = _jobs.get(item.get("job_id"))
        source_name = item.get("source_name") or f"item-{index}"
        prefix = (
            f"{index:03d}-"
            f"{_safe_delivery_stem(source_name, f'item-{index}')}"
        )
        status = job.get("status") if job else "missing"
        png_names: list[str] = []
        pdf_name: str | None = None
        selected = _selected_candidate(job) if job else None
        error = job.get("error") if job else "子任务不存在"
        if job and status == "complete":
            completed_jobs += 1
            _ensure_guide_free_job_outputs(
                job,
                render_pdf=bool(_ensure_delivery_state(batch)["renderPdf"]),
            )
            final_root = job_dir(job["id"]) / "final"
            for page_index, source in enumerate(
                sorted((final_root / "transparent").glob("sheet-*.png")),
                start=1,
            ):
                name = f"{prefix}-p{page_index:02d}.png"
                shutil.copy2(source, png_dir / name)
                png_names.append(name)
                copied_files.append(f"PNG/{name}")
            source_pdf = final_root / "pdf" / "print-sheets.pdf"
            if source_pdf.is_file():
                pdf_name = f"{prefix}.pdf"
                shutil.copy2(source_pdf, pdf_dir / pdf_name)
                copied_files.append(f"PDF/{pdf_name}")
            elif _ensure_delivery_state(batch)["renderPdf"]:
                missing_pdfs.append(source_name)
        manifest_item = {
            "index": index,
            "sourceName": source_name,
            "jobId": job.get("id") if job else item.get("job_id"),
            "status": status,
            "selectedCandidate": (
                job.get("selected_candidate") if job else None
            ),
            "selectedScore": selected.get("score") if selected else None,
            "pngFiles": png_names,
            "pdfFile": pdf_name,
            "error": error,
        }
        manifest_items.append(manifest_item)
        rows.append(
            {
                "index": index,
                "source_name": source_name,
                "job_id": manifest_item["jobId"],
                "status": status,
                "selected_candidate": manifest_item["selectedCandidate"],
                "selected_score": manifest_item["selectedScore"],
                "png_files": ";".join(png_names),
                "pdf_file": pdf_name or "",
                "error": error or "",
            }
        )

    manifest = {
        "batchId": batch["id"],
        "batchStatus": batch.get("status"),
        "renderPdf": _ensure_delivery_state(batch)["renderPdf"],
        "generatedAt": now_iso(),
        "completedItems": completed_jobs,
        "items": manifest_items,
    }
    manifest_json = root / "manifest.json"
    manifest_json.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    manifest_csv = root / "manifest.csv"
    with manifest_csv.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "index",
                "source_name",
                "job_id",
                "status",
                "selected_candidate",
                "selected_score",
                "png_files",
                "pdf_file",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    copied_files.extend(["manifest.json", "manifest.csv"])

    delivery = _ensure_delivery_state(batch)
    delivery["generatedAt"] = manifest["generatedAt"]
    delivery["files"] = copied_files
    if delivery["renderPdf"]:
        delivery["pdfStatus"] = "failed" if missing_pdfs else "ready"
        delivery["error"] = (
            f"{len(missing_pdfs)} 款缺少PDF：{', '.join(missing_pdfs[:3])}"
            if missing_pdfs
            else None
        )
    else:
        delivery["pdfStatus"] = "skipped"
        delivery["error"] = None
    return manifest


def expose_batch(batch: dict[str, Any]) -> dict[str, Any]:
    _ensure_delivery_state(batch)
    payload = json.loads(json.dumps(batch, ensure_ascii=False))
    exposed_items: list[dict[str, Any]] = []
    for item in batch.get("items", []):
        job = _jobs.get(item.get("job_id"))
        exposed = expose_job(job) if job else None
        child_status = exposed.get("status") if exposed else "missing"
        if batch.get("status") == "queued" and child_status == "ready":
            child_status = "queued"
        source_url = None
        master_url = None
        candidate_url = None
        selected_candidate = None
        selected_score = None
        if exposed:
            source_artifact = next(
                (entry for entry in exposed.get("artifacts", []) if entry.get("name") == "upload"),
                None,
            )
            master_artifact = next(
                (entry for entry in exposed.get("artifacts", []) if entry.get("name") == "master"),
                None,
            )
            selected_candidate = _selected_candidate(exposed)
            source_url = source_artifact.get("url") if source_artifact else None
            master_url = master_artifact.get("url") if master_artifact else None
            candidate_url = (
                selected_candidate.get("contact_sheet_url")
                if selected_candidate
                else None
            )
            selected_score = selected_candidate.get("score") if selected_candidate else None
        exposed_items.append(
            {
                **item,
                "status": child_status,
                "current_stage": exposed.get("current_stage") if exposed else None,
                "error": exposed.get("error") if exposed else "子任务不存在",
                "source_url": source_url,
                "master_url": master_url,
                "candidate_url": candidate_url,
                "selected_candidate": (
                    selected_candidate.get("id") if selected_candidate else None
                ),
                "selected_score": selected_score,
                "final_pdf_url": exposed.get("final_pdf_url") if exposed else None,
                "final_pdf_page_urls": (
                    exposed.get("final_pdf_page_urls", []) if exposed else []
                ),
                "job_download_url": exposed.get("download_url") if exposed else None,
            }
        )
    payload["items"] = exposed_items
    payload["total"] = len(exposed_items)
    payload["completed"] = sum(
        item.get("status") == "complete" for item in exposed_items
    )
    payload["failed"] = sum(
        item.get("status") in {"failed", "interrupted", "missing"}
        for item in exposed_items
    )
    payload["download_url"] = f"/api/batches/{batch['id']}/download"
    payload["delivery_download_url"] = (
        f"/api/batches/{batch['id']}/delivery/download"
    )
    payload["delivery_pdf_url"] = (
        f"/api/batches/{batch['id']}/delivery/pdf"
    )
    return payload


def clear_from(job: dict[str, Any], stage: str) -> None:
    index = pipeline.STAGES.index(stage)
    invalid = set(pipeline.STAGES[index:])
    job["artifacts"] = [item for item in job.get("artifacts", []) if item.get("stage") not in invalid]
    if "generate" in invalid:
        job["generation"] = None
    if "key" in invalid:
        job["key_metrics"] = None
        job["background_removal"] = None
    if "components" in invalid:
        job["primitives"] = []
        job["groups"] = []
        job["semantic_grouping"] = None
    if "geometry" in invalid:
        job["geometry"] = []
    if "layout" in invalid:
        job["candidates"] = []
        job["selected_candidate"] = None
        job["pdf_status"] = None
        job["pdf_error"] = None
    job["current_stage"] = pipeline.STAGES[max(0, index - 1)]


def _artifact_from_external(stage: str, item: dict[str, Any], directory: Path) -> dict[str, Any]:
    return pipeline.artifact(stage, item["name"], item["label"], item["path"], directory, item["kind"])


def _master_path(job: dict[str, Any]) -> Path:
    directory = job_dir(job["id"])
    if job["input_mode"] in {"master", "alpha"}:
        return directory / job["uploads"][job["input_mode"]]
    generated = directory / "generate" / "master.png"
    if not generated.exists():
        raise RuntimeError("尚未生成白底母版")
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
            if settings.get("window_template") == "legacy":
                raise RuntimeError("旧版电商原图任务从生图阶段重跑前，必须先选择“单窗”或“双栏窗”模板")
            append_log(job, "调用内部 gpt-image-2 生成强衍生创新白底窗贴母版")
            stage_started = time.perf_counter()
            clear_from(job, "generate")
            source = directory / job["uploads"]["source"]
            base_prompt = job.get("generation_prompt_base") or DEFAULT_PROMPT
            materialized_prompt = render_generation_prompt(base_prompt, settings)
            job["generation_prompt_base"] = base_prompt
            job["generation_prompt"] = materialized_prompt
            master_path, raw_artifacts, metadata = generate_master(
                source,
                directory,
                materialized_prompt,
                settings,
            )
            job["generation"] = metadata
            pipeline.replace_stage_artifacts(job, "generate", [_artifact_from_external("generate", item, directory) for item in raw_artifacts])
            job["current_stage"] = "generate"
            save_job(job)
            append_log(job, f"生图阶段完成，用时 {time.perf_counter() - stage_started:.2f}s")

        if through_index >= pipeline.STAGES.index("key") and start_index <= pipeline.STAGES.index("key"):
            if job["input_mode"] == "alpha":
                append_log(job, "保留上传文件原始 Alpha，跳过去背景")
            else:
                append_log(job, "上传白底母版到七牛云，并调用 ComfyUI 去背景")
            stage_started = time.perf_counter()
            clear_from(job, "key")
            if job["input_mode"] == "alpha":
                artifacts, metrics = pipeline.run_alpha_passthrough(_master_path(job), directory, settings)
            else:
                transparent_path, raw_artifacts, removal_metadata = remove_background(
                    _master_path(job), directory
                )
                artifacts, metrics = pipeline.run_alpha_passthrough(
                    transparent_path, directory, settings
                )
                artifacts = [
                    _artifact_from_external("key", item, directory)
                    for item in raw_artifacts
                ] + artifacts
                metrics["mode"] = "comfyui_background_removal"
                metrics["comfyui"] = removal_metadata
                job["background_removal"] = removal_metadata
            pipeline.replace_stage_artifacts(job, "key", artifacts)
            job["key_metrics"] = metrics
            job["current_stage"] = "key"
            save_job(job)
            stage_label = {
                "alpha": "Alpha 直通",
                "source": "ComfyUI 去背景",
                "master": "ComfyUI 去背景",
            }[job["input_mode"]]
            append_log(job, f"{stage_label}阶段完成，用时 {time.perf_counter() - stage_started:.2f}s")

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
                    model_note = f"，模型 {semantic_metadata.get('model')}"
                    if semantic_metadata.get("fallback_used"):
                        model_note += "（前序模型失败后已使用备用模型）"
                    job.setdefault("logs", []).append(
                        f"[{datetime.now().strftime('%H:%M:%S')}] 语义分组完成，用时 {time.perf_counter() - semantic_started:.2f}s，应用 {semantic_metadata['applied_count']} 条关系{model_note}"
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
            pdf_requested = bool(job.get("render_pdf", True))
            pdf_path = directory / "final" / "pdf" / "print-sheets.pdf"
            final_manifest_path = directory / "final" / "layout.json"
            pdf_error = None
            if final_manifest_path.is_file():
                final_manifest = json.loads(
                    final_manifest_path.read_text(encoding="utf-8")
                )
                pdf_error = final_manifest.get("pdf_error")
            job["pdf_status"] = (
                "skipped"
                if not pdf_requested
                else "ready"
                if pdf_path.is_file()
                else "failed"
            )
            job["pdf_error"] = pdf_error
            job["current_stage"] = "layout"
            save_job(job)
            if job["pdf_status"] == "failed":
                append_log(
                    job,
                    f"PDF生成失败，不影响PNG交付：{pdf_error or '未生成PDF文件'}",
                )
            append_log(job, f"排版阶段完成，用时 {time.perf_counter() - stage_started:.2f}s")

        job["status"] = "complete"
        append_log(job, f"运行完成，当前阶段：{job.get('current_stage')}")
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        append_log(job, f"失败：{exc}")


def _resume_stage(job: dict[str, Any]) -> str:
    current = job.get("current_stage", "input")
    if current not in pipeline.STAGES:
        return "generate"
    index = pipeline.STAGES.index(current)
    if index >= pipeline.STAGES.index("layout"):
        return "layout"
    return pipeline.STAGES[index + 1]


def execute_batch(batch_id: str) -> None:
    batch = require_batch(batch_id)
    try:
        batch["status"] = "running"
        batch["error"] = None
        save_batch(batch)
        for item in batch.get("items", []):
            job = require_job(item["job_id"])
            if job.get("status") == "complete":
                item["status"] = "complete"
                item["error"] = None
                save_batch(batch)
                continue
            batch["current_job_id"] = job["id"]
            item["status"] = "running"
            item["error"] = None
            save_batch(batch)
            from_stage = (
                None if job.get("status") == "ready" else _resume_stage(job)
            )
            job["status"] = "queued"
            job["error"] = None
            save_job(job)
            execute_job(job["id"], batch.get("through_stage", "all"), from_stage)
            completed_job = require_job(job["id"])
            item["status"] = completed_job.get("status")
            item["error"] = completed_job.get("error")
            save_batch(batch)

        statuses = [
            require_job(item["job_id"]).get("status")
            for item in batch.get("items", [])
        ]
        if statuses and all(status == "complete" for status in statuses):
            batch["status"] = "complete"
        elif any(status == "complete" for status in statuses):
            batch["status"] = "partial_success"
        else:
            batch["status"] = "failed"
        batch["current_job_id"] = None
        if batch.get("through_stage", "all") == "all":
            try:
                assemble_delivery(batch)
            except Exception as delivery_error:
                delivery = _ensure_delivery_state(batch)
                delivery["error"] = str(delivery_error)
                if delivery.get("renderPdf"):
                    delivery["pdfStatus"] = "failed"
        save_batch(batch)
    except Exception as exc:
        batch["status"] = "failed"
        batch["error"] = str(exc)
        batch["current_job_id"] = None
        save_batch(batch)


class RunRequest(BaseModel):
    through_stage: Literal["generate", "key", "components", "geometry", "layout", "all"] = "all"
    from_stage: Literal["generate", "key", "components", "geometry", "layout"] | None = None
    sync: bool = False


class SettingsPatch(BaseModel):
    settings: dict[str, Any]


class PromptPreviewRequest(BaseModel):
    generation_prompt_base: str = DEFAULT_PROMPT
    settings: dict[str, Any] = Field(default_factory=dict)


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


@app.post("/api/restart")
def restart_service() -> dict[str, Any]:
    """先应答再退出自身；由分离的辅助进程等端口释放后拉起新服务。"""
    port = int(os.getenv("LP_APP_PORT", "8790") or 8790)
    helper = (
        "import socket, subprocess, sys, time\n"
        "deadline = time.time() + 30\n"
        "while time.time() < deadline:\n"
        "    probe = socket.socket()\n"
        "    probe.settimeout(1)\n"
        "    try:\n"
        f"        probe.connect(('127.0.0.1', {port}))\n"
        "        probe.close()\n"
        "        time.sleep(0.5)\n"
        "    except OSError:\n"
        "        probe.close()\n"
        "        break\n"
        f"subprocess.run([sys.executable, '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', '{port}'])\n"
    )

    def _relaunch() -> None:
        time.sleep(0.8)  # 等 HTTP 响应发出去再退出进程
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        with open(APP_DIR / "server.out.log", "ab") as log:
            subprocess.Popen(
                [sys.executable, "-c", helper],
                cwd=str(APP_DIR),
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                close_fds=True,
            )
        os._exit(0)

    threading.Thread(target=_relaunch, daemon=True).start()
    return {"ok": True, "message": "服务正在重启，约 5-15 秒后恢复"}


# ---------------------------------------------------------------------------
# 评图（人工批注）：批注是长期资产，全部落盘到 reviews/ 并进 git。
# ---------------------------------------------------------------------------

DEFAULT_DEFECT_TYPES = [
    {"id": "divider_intrusion", "label": "分隔带被侵入/断裂"},
    {"id": "real_window", "label": "出现窗框/玻璃/把手/阴影"},
    {"id": "imbalance", "label": "左右视觉失衡"},
    {"id": "mechanical_copy", "label": "机械镜像/简单复制"},
    {"id": "whitespace", "label": "留白不当（过挤/过空）"},
    {"id": "style_mismatch", "label": "风格不统一"},
    {"id": "edge_overflow", "label": "元素越界/贴边"},
    {"id": "theme_wrong", "label": "主题或元素不符"},
    {"id": "quality", "label": "画质问题（模糊/伪影/乱码）"},
    {"id": "other", "label": "其他"},
]


def _defect_types_path() -> Path:
    return REVIEWS_DIR / "defect-types.json"


def _load_defect_types() -> list[dict[str, Any]]:
    path = _defect_types_path()
    if not path.is_file():
        REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        _save_json_with_unique_temp(path, DEFAULT_DEFECT_TYPES)
        return list(DEFAULT_DEFECT_TYPES)
    return json.loads(path.read_text(encoding="utf-8"))


def _prompt_hash(job: dict[str, Any]) -> str:
    prompt = (job.get("generation_prompt_base") or job.get("generation_prompt") or "").strip()
    if not prompt:
        return ""
    # 浏览器 textarea 提交的是 CRLF，统一换行符避免同一内容产生两个指纹。
    normalized = prompt.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]


def _snapshot_prompt(job: dict[str, Any]) -> None:
    """按哈希去重保存 prompt 全文，任务目录被清理后批注仍可反查 prompt。"""
    digest = _prompt_hash(job)
    if not digest:
        return
    prompt_dir = REVIEWS_DIR / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    target = prompt_dir / f"{digest}.txt"
    if not target.is_file():
        prompt = (job.get("generation_prompt_base") or job.get("generation_prompt") or "").strip()
        target.write_text(prompt, encoding="utf-8")


def _load_annotations() -> list[dict[str, Any]]:
    directory = REVIEWS_DIR / "annotations"
    if not directory.is_dir():
        return []
    records = []
    for path in directory.glob("*.json"):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    records.sort(key=lambda item: item.get("created_at", ""))
    return records


class AnnotationRequest(BaseModel):
    job_id: str
    defect_types: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high"] = "medium"
    note: str = ""


class DefectTypeRequest(BaseModel):
    label: str


@app.get("/review")
def review_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "review.html")


@app.get("/api/review/overview")
def review_overview(limit: int = 100) -> dict[str, Any]:
    annotations = _load_annotations()
    by_job: dict[str, list[dict[str, Any]]] = {}
    for record in annotations:
        by_job.setdefault(record.get("job_id", ""), []).append(record)
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda item: item.get("created_at", ""), reverse=True)
    cards = []
    for job in jobs:
        master = job_dir(job["id"]) / "generate" / "master.png"
        if not master.is_file():
            continue
        source = next(iter(job_dir(job["id"]).glob("upload-source.*")), None)
        template = get_window_template(job.get("settings", {}).get("window_template"), allow_legacy=True)
        cards.append({
            "job_id": job["id"],
            "created_at": job.get("created_at"),
            "source_name": job.get("source_name"),
            "status": job.get("status"),
            "window_template": template["id"],
            "window_template_label": template.get("label", template["id"]),
            "prompt_hash": _prompt_hash(job),
            "prompt": (job.get("generation_prompt_base") or job.get("generation_prompt") or ""),
            "master_url": f"/api/jobs/{job['id']}/files/generate/master.png",
            "source_url": f"/api/jobs/{job['id']}/files/{source.name}" if source else None,
            "annotations": by_job.get(job["id"], []),
        })
        if len(cards) >= max(1, limit):
            break
    constraints = {
        item["id"]: item.get("prompt_constraint", "")
        for item in public_window_templates()
    }
    return {
        "defect_types": _load_defect_types(),
        "jobs": cards,
        "default_prompt": DEFAULT_PROMPT,
        "template_constraints": constraints,
        "annotation_total": len(annotations),
    }


@app.post("/api/review/annotations")
def create_annotation(request: AnnotationRequest) -> dict[str, Any]:
    job = require_job(request.job_id)
    note = request.note.strip()
    if not note and not request.defect_types:
        raise HTTPException(status_code=400, detail="缺陷类别和批注内容至少填一项")
    known_types = {item["id"] for item in _load_defect_types()}
    unknown = [item for item in request.defect_types if item not in known_types]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知缺陷类别: {', '.join(unknown)}")
    _snapshot_prompt(job)
    record = {
        "id": f"rv-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        "created_at": now_iso(),
        "job_id": job["id"],
        "image": "generate/master.png",
        "prompt_hash": _prompt_hash(job),
        "window_template": job.get("settings", {}).get("window_template"),
        "defect_types": list(dict.fromkeys(request.defect_types)),
        "severity": request.severity,
        "note": note[:2000],
        "status": "open",
    }
    directory = REVIEWS_DIR / "annotations"
    directory.mkdir(parents=True, exist_ok=True)
    _save_json_with_unique_temp(directory / f"{record['id']}.json", record)
    return record


@app.delete("/api/review/annotations/{annotation_id}")
def delete_annotation(annotation_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"rv-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", annotation_id):
        raise HTTPException(status_code=400, detail="无效的批注 ID")
    path = REVIEWS_DIR / "annotations" / f"{annotation_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="批注不存在")
    path.unlink()
    return {"ok": True}


@app.post("/api/review/defect-types")
def add_defect_type(request: DefectTypeRequest) -> dict[str, Any]:
    label = request.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="类别名称不能为空")
    types = _load_defect_types()
    if any(item["label"] == label for item in types):
        raise HTTPException(status_code=400, detail="同名类别已存在")
    new_type = {"id": f"custom_{uuid.uuid4().hex[:8]}", "label": label}
    types.append(new_type)
    _save_json_with_unique_temp(_defect_types_path(), types)
    return new_type


@app.get("/api/defaults")
def defaults() -> dict[str, Any]:
    settings = pipeline.default_settings()
    return {
        "settings": settings,
        "generation_prompt": DEFAULT_PROMPT,
        "generation_prompt_preview": render_generation_prompt(DEFAULT_PROMPT, settings),
        "window_templates": public_window_templates(),
        "default_window_template": DEFAULT_WINDOW_TEMPLATE,
        "template_constraint_version": TEMPLATE_CONSTRAINT_VERSION,
        "generation_configured": generation_configured(),
        "comfyui_configured": comfyui_configured(),
        "semantic_grouping_configured": semantic_grouping_configured(),
    }


@app.post("/api/prompts/preview")
def preview_prompt(request: PromptPreviewRequest) -> dict[str, Any]:
    try:
        settings = pipeline.merge_settings(request.settings)
        return {"prompt": render_generation_prompt(request.generation_prompt_base, settings)}
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/batches")
def list_batches() -> list[dict[str, Any]]:
    batches = sorted(
        _batches.values(),
        key=lambda item: item.get("created_at", ""),
        reverse=True,
    )
    return [
        {
            "id": item["id"],
            "status": item.get("status"),
            "total": len(item.get("items", [])),
            "completed": sum(
                _jobs.get(child.get("job_id"), {}).get("status") == "complete"
                for child in item.get("items", [])
            ),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "pdf_status": _ensure_delivery_state(item).get("pdfStatus"),
        }
        for item in batches[:30]
    ]


@app.post("/api/batches")
async def create_batch(
    files: list[UploadFile] = File(...),
    settings_json: str = Form("{}"),
    generation_prompt: str = Form(DEFAULT_PROMPT),
    render_pdf: bool = Form(True),
    through_stage: Literal["generate", "all"] = Form("all"),
) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一张电商原图")
    if not generation_configured():
        raise HTTPException(status_code=409, detail="gpt-image-2 尚未配置")
    if through_stage == "all" and not comfyui_configured():
        raise HTTPException(status_code=409, detail="ComfyUI 去背景工作流尚未配置")

    uploads: list[tuple[str, bytes]] = []
    for file in files:
        file_name = file.filename or "upload.png"
        suffix = Path(file_name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"{file_name} 不是支持的图片格式",
            )
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"{file_name} 超过 20 MB")
        _validate_upload_bytes(content, "source")
        uploads.append((file_name, content))

    settings = _parse_settings(settings_json)
    prompt = generation_prompt
    batch_id = f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    created = now_iso()
    items: list[dict[str, Any]] = []
    for file_name, content in uploads:
        job = _create_job_record(
            input_mode="source",
            file_name=file_name,
            content=content,
            settings=json.loads(json.dumps(settings)),
            generation_prompt=prompt,
            batch_id=batch_id,
            render_pdf=render_pdf,
        )
        items.append(
            {
                "job_id": job["id"],
                "source_name": file_name,
                "status": "queued",
                "error": None,
            }
        )
    batch = {
        "schema_version": 2,
        "id": batch_id,
        "status": "queued",
        "items": items,
        "settings": settings,
        "generation_prompt": prompt,
        "through_stage": through_stage,
        "delivery": {
            "renderPdf": render_pdf and through_stage == "all",
            "pdfStatus": "pending" if render_pdf and through_stage == "all" else "skipped",
            "generatedAt": None,
            "files": [],
            "error": None,
        },
        "current_job_id": None,
        "error": None,
        "created_at": created,
        "updated_at": created,
    }
    save_batch(batch)
    threading.Thread(
        target=execute_batch,
        args=(batch_id,),
        daemon=True,
    ).start()
    return expose_batch(batch)


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, Any]:
    return expose_batch(require_batch(batch_id))


@app.post("/api/batches/{batch_id}/retry")
def retry_batch(batch_id: str) -> dict[str, Any]:
    batch = require_batch(batch_id)
    if batch.get("status") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="批次仍在运行")
    incomplete = 0
    for item in batch.get("items", []):
        job = require_job(item["job_id"])
        if job.get("status") == "complete":
            item["status"] = "complete"
            item["error"] = None
            continue
        incomplete += 1
        item["status"] = "queued"
        item["error"] = None
    if not incomplete:
        batch["status"] = "complete"
        batch["error"] = None
        save_batch(batch)
        return expose_batch(batch)
    batch["status"] = "queued"
    batch["error"] = None
    batch["current_job_id"] = None
    delivery = _ensure_delivery_state(batch)
    delivery["pdfStatus"] = (
        "pending" if delivery.get("renderPdf") else "skipped"
    )
    delivery["generatedAt"] = None
    delivery["files"] = []
    delivery["error"] = None
    save_batch(batch)
    threading.Thread(
        target=execute_batch,
        args=(batch_id,),
        daemon=True,
    ).start()
    return expose_batch(batch)


def generate_batch_pdfs(batch_id: str) -> None:
    batch = require_batch(batch_id)
    delivery = _ensure_delivery_state(batch)
    errors: list[str] = []
    try:
        delivery["renderPdf"] = True
        delivery["pdfStatus"] = "rendering"
        delivery["error"] = None
        save_batch(batch)
        for item in batch.get("items", []):
            job = require_job(item["job_id"])
            if job.get("status") != "complete":
                continue
            try:
                outputs = pipeline.render_existing_pdfs(
                    job_dir(job["id"]),
                    pipeline.merge_settings(job.get("settings")),
                )
                job["render_pdf"] = True
                job["pdf_status"] = "ready"
                job["pdf_error"] = None
                job["artifacts"] = [
                    artifact
                    for artifact in job.get("artifacts", [])
                    if artifact.get("name") != "selected-pdf"
                    and not artifact.get("name", "").startswith(
                        "selected-pdf-page-"
                    )
                ]
                pdf_artifacts = [
                    artifact
                    for artifact in pipeline.selected_output_artifacts(
                        outputs,
                        job_dir(job["id"]),
                        "补生成方案",
                    )
                    if artifact.get("name") == "selected-pdf"
                    or artifact.get("name", "").startswith(
                        "selected-pdf-page-"
                    )
                ]
                job["artifacts"].extend(pdf_artifacts)
                save_job(job)
            except Exception as exc:
                job["render_pdf"] = True
                job["pdf_status"] = "failed"
                job["pdf_error"] = str(exc)
                save_job(job)
                errors.append(f"{item.get('source_name')}: {exc}")
        assemble_delivery(batch)
        delivery = _ensure_delivery_state(batch)
        if errors:
            delivery["pdfStatus"] = "failed"
            delivery["error"] = "；".join(errors[:5])
        save_batch(batch)
    except Exception as exc:
        delivery["pdfStatus"] = "failed"
        delivery["error"] = str(exc)
        save_batch(batch)


@app.post("/api/batches/{batch_id}/delivery/pdf")
def render_batch_delivery_pdfs(batch_id: str) -> dict[str, Any]:
    batch = require_batch(batch_id)
    if batch.get("status") in {"queued", "running"}:
        raise HTTPException(
            status_code=409,
            detail="批次仍在运行，完成后才能补生成PDF",
        )
    if not any(
        require_job(item["job_id"]).get("status") == "complete"
        for item in batch.get("items", [])
    ):
        raise HTTPException(status_code=409, detail="没有可生成PDF的完成项")
    delivery = _ensure_delivery_state(batch)
    if delivery.get("pdfStatus") in {"queued", "rendering"}:
        raise HTTPException(status_code=409, detail="PDF正在生成")
    delivery["renderPdf"] = True
    delivery["pdfStatus"] = "queued"
    delivery["error"] = None
    save_batch(batch)
    threading.Thread(
        target=generate_batch_pdfs,
        args=(batch_id,),
        daemon=True,
    ).start()
    return expose_batch(batch)


@app.get("/api/batches/{batch_id}/delivery/download")
def download_batch_delivery(batch_id: str) -> FileResponse:
    batch = require_batch(batch_id)
    if not any(
        require_job(item["job_id"]).get("status") == "complete"
        for item in batch.get("items", [])
    ):
        raise HTTPException(status_code=409, detail="没有可交付的完成项")
    delivery = _ensure_delivery_state(batch)
    if delivery.get("pdfStatus") in {"queued", "rendering"}:
        raise HTTPException(status_code=409, detail="PDF仍在生成，请稍后下载")
    assemble_delivery(batch)
    save_batch(batch)
    root = delivery_dir(batch_id)
    archive = batch_dir(batch_id) / f"{batch_id}-delivery.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as output:
        output.writestr("PNG/", "")
        output.writestr("PDF/", "")
        for path in root.rglob("*"):
            if path.is_file():
                output.write(path, path.relative_to(root).as_posix())
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=archive.name,
    )


@app.get("/api/batches/{batch_id}/download")
def download_batch(batch_id: str) -> FileResponse:
    batch = require_batch(batch_id)
    root = batch_dir(batch_id)
    archive = root / f"{batch_id}-results.zip"
    summary: dict[str, Any] = {
        "batch_id": batch_id,
        "status": batch.get("status"),
        "items": [],
    }
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for index, item in enumerate(batch.get("items", []), start=1):
            job = require_job(item["job_id"])
            if job.get("status") != "complete":
                continue
            source_stem = re.sub(
                r"[^\w.-]+",
                "-",
                Path(item.get("source_name") or f"item-{index}").stem,
            ).strip("-") or f"item-{index}"
            prefix = f"{index:03d}-{source_stem}"
            directory = job_dir(job["id"])
            master = directory / "generate" / "master.png"
            if master.is_file():
                output.write(master, f"{prefix}/innovation-master.png")
            final_root = directory / "final"
            if final_root.is_dir():
                for path in final_root.rglob("*"):
                    if path.is_file():
                        output.write(
                            path,
                            f"{prefix}/final/{path.relative_to(final_root).as_posix()}",
                        )
            selected = next(
                (
                    candidate
                    for candidate in job.get("candidates", [])
                    if candidate.get("id") == job.get("selected_candidate")
                ),
                None,
            )
            if selected:
                selected_path = (
                    directory
                    / "layout"
                    / selected["id"]
                    / "layout.json"
                )
                if selected_path.is_file():
                    output.write(
                        selected_path,
                        f"{prefix}/selected-layout.json",
                    )
            summary["items"].append(
                {
                    "source_name": item.get("source_name"),
                    "job_id": job["id"],
                    "selected_candidate": job.get("selected_candidate"),
                    "selected_score": selected.get("score") if selected else None,
                }
            )
        output.writestr(
            "batch-summary.json",
            json.dumps(summary, ensure_ascii=False, indent=2),
        )
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=archive.name,
    )


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


def _parse_settings(settings_json: str) -> dict[str, Any]:
    try:
        return pipeline.merge_settings(json.loads(settings_json or "{}"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="settings_json 格式错误") from exc


def _validate_upload_bytes(
    content: bytes,
    input_mode: Literal["source", "master", "alpha"],
) -> None:
    from io import BytesIO
    from PIL import Image

    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
        if input_mode == "alpha":
            with Image.open(BytesIO(content)) as image:
                has_alpha = (
                    "A" in image.getbands()
                    or image.info.get("transparency") is not None
                )
                if not has_alpha:
                    raise ValueError("没有 Alpha 通道")
                alpha = image.convert("RGBA").getchannel("A")
                alpha_min, alpha_max = alpha.getextrema()
                if alpha_max == 0:
                    raise ValueError("图片完全透明")
                if alpha_min == 255:
                    raise ValueError("Alpha 全部不透明")
    except Exception as exc:
        detail = (
            f"透明底图片无效：{exc}"
            if input_mode == "alpha"
            else "上传文件不是有效图片"
        )
        raise HTTPException(status_code=400, detail=detail) from exc


def _create_job_record(
    *,
    input_mode: Literal["source", "master", "alpha"],
    file_name: str,
    content: bytes,
    settings: dict[str, Any],
    generation_prompt: str,
    batch_id: str | None = None,
    render_pdf: bool = True,
) -> dict[str, Any]:
    suffix = Path(file_name or "upload.png").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="仅支持 PNG、JPG、JPEG、WEBP")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"{file_name} 超过 20 MB")
    _validate_upload_bytes(content, input_mode)
    settings = pipeline.merge_settings(settings)
    prompt_base = (generation_prompt or DEFAULT_PROMPT).strip()
    prompt_materialized = (
        render_generation_prompt(prompt_base, settings)
        if settings.get("window_template") != "legacy"
        else prompt_base
    )
    ratio_warning = None
    if input_mode in {"master", "alpha"}:
        from io import BytesIO
        from PIL import Image

        with Image.open(BytesIO(content)) as image:
            ratio_warning = aspect_ratio_warning(
                image.width,
                image.height,
                settings.get("window_template"),
            )

    job_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    directory = job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    upload_name = f"upload-{input_mode}{suffix}"
    upload_path = directory / upload_name
    upload_path.write_bytes(content)
    created = now_iso()
    job = {
        "id": job_id,
        "batch_id": batch_id,
        "source_name": file_name,
        "status": "ready",
        "input_mode": input_mode,
        "selection_mode": (
            "highest_score" if input_mode == "source" else "production_rank"
        ),
        "uploads": {input_mode: upload_name},
        "settings": settings,
        "generation_prompt_base": prompt_base,
        "generation_prompt": prompt_materialized,
        "aspect_ratio_warning": ratio_warning,
        "render_pdf": bool(render_pdf),
        "pdf_status": None,
        "pdf_error": None,
        "generation": None,
        "background_removal": None,
        "key_metrics": None,
        "semantic_grouping": None,
        "primitives": [],
        "groups": [],
        "geometry": [],
        "candidates": [],
        "selected_candidate": None,
        "current_stage": "input",
        "artifacts": [
            pipeline.artifact(
                "input",
                "upload",
                {
                    "source": "上传的电商图",
                    "master": "上传的白底母版",
                    "alpha": "上传的透明底母版",
                }[input_mode],
                upload_path,
                directory,
            )
        ],
        "logs": [f"[{datetime.now().strftime('%H:%M:%S')}] 创建任务，输入模式：{input_mode}"],
        "error": None,
        "created_at": created,
        "updated_at": created,
    }
    if ratio_warning:
        job["logs"].append(
            f"[{datetime.now().strftime('%H:%M:%S')}] 比例警告：{ratio_warning['message']}"
        )
    save_job(job)
    return job


@app.post("/api/jobs")
async def create_job(
    input_mode: Literal["source", "master", "alpha"] = Form(...),
    file: UploadFile = File(...),
    settings_json: str = Form("{}"),
    generation_prompt: str = Form(DEFAULT_PROMPT),
) -> dict[str, Any]:
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    job = _create_job_record(
        input_mode=input_mode,
        file_name=file.filename or "upload.png",
        content=content,
        settings=_parse_settings(settings_json),
        generation_prompt=generation_prompt,
    )
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
    incoming = dict(patch.settings)
    requested_template = incoming.get("window_template", old.get("window_template"))
    template = get_window_template(requested_template, allow_legacy=True)
    if requested_template != old.get("window_template") and template["id"] != "legacy":
        incoming.setdefault("install_width_mm", template["default_mm"][0])
        incoming.setdefault("install_height_mm", template["default_mm"][1])
    elif template["id"] != "legacy":
        ratio = float(template["ratio"][0]) / float(template["ratio"][1])
        if "install_height_mm" in incoming and "install_width_mm" not in incoming:
            incoming["install_width_mm"] = float(incoming["install_height_mm"]) * ratio
        elif "install_width_mm" in incoming and "install_height_mm" not in incoming:
            incoming["install_height_mm"] = float(incoming["install_width_mm"]) / ratio
    new = pipeline.merge_settings({**old, **incoming})
    job["settings"] = new
    key_fields: set[str] = set()
    component_fields = {
        "install_width_mm", "install_height_mm", "group_gap_mm",
        "content_occupancy_ratio",
        "semantic_grouping_enabled", "semantic_min_confidence",
        "min_component_area", "alpha_threshold",
    }
    geometry_fields = {"cut_offset_mm", "spacing_mm", "simplify_mm"}
    changed = {key for key in new if new.get(key) != old.get(key)}
    changed -= {"key_low", "key_high", "morph_kernel"}
    template_changed = "window_template" in changed
    if template_changed:
        base_prompt = job.get("generation_prompt_base") or DEFAULT_PROMPT
        job["generation_prompt_base"] = base_prompt
        job["generation_prompt"] = render_generation_prompt(base_prompt, new)
        clear_from(job, "generate" if job.get("input_mode") == "source" else "components")
    elif changed & key_fields:
        clear_from(job, "key")
    elif changed & component_fields:
        clear_from(job, "components")
    elif changed & geometry_fields:
        clear_from(job, "geometry")
    elif changed:
        clear_from(job, "layout")
    if job.get("input_mode") in {"master", "alpha"}:
        from PIL import Image

        with Image.open(_master_path(job)) as image:
            job["aspect_ratio_warning"] = aspect_ratio_warning(
                image.width,
                image.height,
                new.get("window_template"),
            )
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
    if candidate.get("enabled") is False:
        raise HTTPException(status_code=400, detail="该候选方案已禁用")
    outputs = pipeline.render_selected_outputs(
        candidate,
        job.get("geometry", []),
        job_dir(job_id),
        job["settings"],
        render_pdf=bool(job.get("render_pdf", True)),
    )
    selected_names = {"selected-transparent", "selected-white", "selected-pdf"}
    job["artifacts"] = [
        item for item in job.get("artifacts", [])
        if item.get("name") not in selected_names and not item.get("name", "").startswith("selected-pdf-page-")
    ]
    job["artifacts"].extend(pipeline.selected_output_artifacts(outputs, job_dir(job_id), "手动选中方案"))
    job["selected_candidate"] = candidate_id
    job["pdf_status"] = (
        "ready"
        if outputs.get("combined_pdf")
        else "failed"
        if outputs.get("pdf_error")
        else "skipped"
    )
    job["pdf_error"] = outputs.get("pdf_error")
    pdf_note = (
        f"已生成 {len(outputs['page_pdfs'])} 张单页 PDF 和 1 份多页 PDF"
        if outputs.get("combined_pdf")
        else "未请求PDF"
        if job["pdf_status"] == "skipped"
        else f"PDF生成失败：{job['pdf_error']}"
    )
    append_log(job, f"手动选择候选方案：{candidate_id}；{pdf_note}")
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
