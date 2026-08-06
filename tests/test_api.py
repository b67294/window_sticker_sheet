import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from pypdf import PdfReader

import app as webapp


def upload_bytes():
    image = Image.new("RGB", (500, 350), (0, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((30, 40, 140, 150), fill=(210, 20, 30))
    draw.rectangle((240, 60, 350, 165), fill=(248, 248, 248))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def transparent_upload_bytes():
    image = Image.new("RGBA", (500, 350), (13, 240, 17, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((30, 40, 140, 150), fill=(210, 20, 30, 255))
    draw.rectangle((240, 60, 350, 165), fill=(248, 248, 248, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_defaults_publish_window_templates_and_upload_warns_on_ratio(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
    client = TestClient(webapp.app)
    defaults = client.get("/api/defaults").json()
    assert defaults["default_window_template"] == "double"
    assert {item["id"]: item["generation_size"] for item in defaults["window_templates"]} == {
        "double": "1216x1216",
        "single_portrait": "1168x1392",
        "single_landscape": "1392x1168",
        "big_single": "1216x1216",
        "single": "1056x1408",
    }
    assert {item["id"] for item in defaults["window_frames"]} == {"pane1", "pane2", "pane3"}
    assert {item["id"] for item in defaults["canvas_ratios"]} == {"1:1", "73:87", "87:73", "3:4"}
    assert defaults["window_templates"][0]["id"] == "double"
    assert [item["id"] for item in defaults["prompt_styles"]][:3] == [
        "scene", "large_elements", "small_scatter",
    ]
    assert defaults["default_prompt_style"] == "scene"
    response = client.post(
        "/api/jobs",
        data={
            "input_mode": "master",
            "settings_json": json.dumps({"window_template": "single"}),
            "generation_prompt": "",
        },
        files={"file": ("wide-master.png", upload_bytes(), "image/png")},
    )
    assert response.status_code == 200
    warning = response.json()["aspect_ratio_warning"]
    assert warning["code"] == "aspect_ratio_mismatch"
    assert warning["expected_aspect_ratio"] == 0.75


def test_direct_master_job(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
    def fake_remove_background(source_path, directory):
        output_dir = directory / "comfyui"
        output_dir.mkdir(parents=True, exist_ok=True)
        result = output_dir / "transparent.png"
        result.write_bytes(transparent_upload_bytes())
        return result, [], {"prompt_id": "mock-comfyui", "alpha_range": [0, 255]}

    monkeypatch.setattr(webapp, "remove_background", fake_remove_background)
    client = TestClient(webapp.app)
    settings = webapp.pipeline.default_settings()
    settings.update({"install_width_mm": 250, "install_height_mm": 175, "preview_dpi": 24, "output_dpi": 24, "group_gap_mm": 0.2})
    response = client.post(
        "/api/jobs",
        data={"input_mode": "master", "settings_json": json.dumps(settings), "generation_prompt": ""},
        files={"file": ("master.png", upload_bytes(), "image/png")},
    )
    assert response.status_code == 200
    job_id = response.json()["id"]
    response = client.post(f"/api/jobs/{job_id}/run", json={"through_stage": "all", "sync": True})
    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "complete", job.get("error")
    assert len(job["candidates"]) == 4
    assert job["selected_candidate"]
    selected = next(item for item in job["candidates"] if item["id"] == job["selected_candidate"])
    assert job["final_pdf_url"].endswith("final/pdf/print-sheets.pdf")
    assert len(job["final_pdf_page_urls"]) == selected["page_count"]
    combined_pdf = tmp_path / job_id / "final" / "pdf" / "print-sheets.pdf"
    reader = PdfReader(combined_pdf)
    assert len(reader.pages) == selected["page_count"]
    width_pt = float(reader.pages[0].mediabox.width)
    height_pt = float(reader.pages[0].mediabox.height)
    assert abs(width_pt - settings["sheet_width_mm"] * 72 / 25.4) < 0.1
    assert abs(height_pt - settings["sheet_height_mm"] * 72 / 25.4) < 0.1

    disabled = next(item for item in job["candidates"] if item.get("enabled") is False)
    assert disabled["id"] != job["selected_candidate"]
    response = client.post(f"/api/jobs/{job_id}/candidates/{disabled['id']}/select")
    assert response.status_code == 400

    other = next(
        item for item in job["candidates"]
        if item["id"] != job["selected_candidate"] and item.get("enabled", True)
    )
    response = client.post(f"/api/jobs/{job_id}/candidates/{other['id']}/select")
    assert response.status_code == 200
    selected_job = response.json()
    assert selected_job["selected_candidate"] == other["id"]
    assert len(selected_job["final_pdf_page_urls"]) == other["page_count"]
    assert "单页 PDF" in selected_job["logs"][-1]
    response = client.get(f"/api/jobs/{job_id}/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "final/pdf/print-sheets.pdf" in names
        assert {f"final/pdf/sheet-{index:02d}.pdf" for index in range(1, other["page_count"] + 1)} <= names


def test_transparent_alpha_job_skips_chroma_key(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
    client = TestClient(webapp.app)
    settings = webapp.pipeline.default_settings()
    settings.update({"install_width_mm": 250, "install_height_mm": 175, "preview_dpi": 24, "output_dpi": 24, "group_gap_mm": 0.2})
    response = client.post(
        "/api/jobs",
        data={"input_mode": "alpha", "settings_json": json.dumps(settings), "generation_prompt": ""},
        files={"file": ("transparent.png", transparent_upload_bytes(), "image/png")},
    )
    assert response.status_code == 200
    job_id = response.json()["id"]

    response = client.post(f"/api/jobs/{job_id}/run", json={"through_stage": "all", "sync": True})
    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "complete", job.get("error")
    assert job["key_metrics"]["mode"] == "alpha_passthrough"
    assert job["key_metrics"]["partial_alpha_pixels"] == 0
    assert len(job["primitives"]) == 2
    assert all(group["rotatable"] is False for group in job["groups"])
    assert len(job["candidates"]) == 4


def test_transparent_alpha_job_rejects_opaque_image(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
    client = TestClient(webapp.app)
    response = client.post(
        "/api/jobs",
        data={"input_mode": "alpha", "settings_json": "{}", "generation_prompt": ""},
        files={"file": ("opaque.png", upload_bytes(), "image/png")},
    )
    assert response.status_code == 400
    assert "Alpha" in response.json()["detail"]


def test_concurrent_job_saves_use_atomic_unique_temp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
    job = {
        "id": "concurrent-save",
        "status": "complete",
        "current_stage": "components",
        "artifacts": [],
        "primitives": [],
        "groups": [],
        "geometry": [],
        "candidates": [],
        "logs": [],
    }
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: webapp.save_job(job), range(24)))

    saved = json.loads((tmp_path / job["id"] / "job.json").read_text(encoding="utf-8"))
    assert saved["id"] == job["id"]
    assert list((tmp_path / job["id"]).glob("*.tmp")) == []


def test_running_job_rejects_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
    job = {
        "id": "busy-job",
        "status": "running",
        "settings": webapp.pipeline.default_settings(),
        "current_stage": "components",
        "artifacts": [],
        "primitives": [],
        "groups": [],
        "geometry": [],
        "candidates": [],
        "logs": [],
    }
    webapp.save_job(job)
    client = TestClient(webapp.app)
    response = client.patch(f"/api/jobs/{job['id']}/settings", json={"settings": {"spacing_mm": 3}})
    assert response.status_code == 409


def test_ecommerce_batch_creates_independent_serial_children(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
    webapp._batches.clear()
    monkeypatch.setattr(webapp, "generation_configured", lambda: True)
    monkeypatch.setattr(webapp, "comfyui_configured", lambda: True)

    started = []

    class DeferredThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
        def start(self):
            started.append((self.target, self.args))

    monkeypatch.setattr(webapp.threading, "Thread", DeferredThread)
    client = TestClient(webapp.app)
    prompt = "共享的强衍生测试 Prompt"
    files = [
        ("files", (f"source-{index}.png", upload_bytes(), "image/png"))
        for index in range(3)
    ]
    response = client.post(
        "/api/batches",
        data={"settings_json": "{}", "generation_prompt": prompt},
        files=files,
    )
    assert response.status_code == 200, response.text
    batch = response.json()
    assert batch["status"] == "queued"
    assert batch["total"] == 3
    assert batch["delivery"]["renderPdf"] is True
    assert batch["delivery"]["pdfStatus"] == "pending"
    assert len(started) == 1
    assert (tmp_path / "_batches" / batch["id"] / "batch.json").is_file()
    child_jobs = [webapp._jobs[item["job_id"]] for item in batch["items"]]
    assert all(job["input_mode"] == "source" for job in child_jobs)
    assert all(job["generation_prompt"].startswith(prompt) for job in child_jobs)
    assert all("【本次任务唯一尺寸约束】" not in job["generation_prompt"] for job in child_jobs)
    assert all(job["settings"]["window_template"] == "double" for job in child_jobs)
    assert all(job["generation_prompt"].count("【当前窗户模板硬约束") == 1 for job in child_jobs)
    assert all(job["render_pdf"] is True for job in child_jobs)
    assert len({webapp.job_dir(job["id"]) for job in child_jobs}) == 3


def test_batch_can_skip_pdf_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
    webapp._batches.clear()
    monkeypatch.setattr(webapp, "generation_configured", lambda: True)
    monkeypatch.setattr(webapp, "comfyui_configured", lambda: True)

    class DeferredThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
        def start(self):
            pass

    monkeypatch.setattr(webapp.threading, "Thread", DeferredThread)
    client = TestClient(webapp.app)
    response = client.post(
        "/api/batches",
        data={
            "settings_json": "{}",
            "generation_prompt": "prompt",
            "render_pdf": "false",
        },
        files=[
            ("files", ("a.png", upload_bytes(), "image/png")),
            ("files", ("b.png", upload_bytes(), "image/png")),
        ],
    )
    assert response.status_code == 200
    batch = response.json()
    assert batch["delivery"]["renderPdf"] is False
    assert batch["delivery"]["pdfStatus"] == "skipped"
    assert all(
        webapp._jobs[item["job_id"]]["render_pdf"] is False
        for item in batch["items"]
    )


def test_delivery_package_groups_selected_png_and_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
    webapp._batches.clear()
    job = webapp._create_job_record(
        input_mode="source",
        file_name="商品 A.webp",
        content=upload_bytes(),
        settings=webapp.pipeline.default_settings(),
        generation_prompt="prompt",
        batch_id="batch-delivery",
    )
    final_root = webapp.job_dir(job["id"]) / "final"
    (final_root / "transparent").mkdir(parents=True)
    (final_root / "pdf").mkdir(parents=True)
    for page in (1, 2):
        image = Image.new("RGBA", (40, 30), (0, 0, 0, 0))
        ImageDraw.Draw(image).rectangle((8, 8, 20, 20), fill=(220, 30, 40, 255))
        image.save(
            final_root / "transparent" / f"sheet-{page:02d}.png",
            dpi=(300, 300),
        )
    (final_root / "pdf" / "print-sheets.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (final_root / "layout.json").write_text(
        json.dumps({"delivery_render_version": 2, "files": {}}),
        encoding="utf-8",
    )
    job["status"] = "complete"
    job["current_stage"] = "layout"
    job["selected_candidate"] = "candidate-4"
    job["candidates"] = [{"id": "candidate-4", "score": 0.93, "page_count": 2}]
    webapp.save_job(job)
    batch = {
        "id": "batch-delivery",
        "status": "complete",
        "items": [
            {
                "job_id": job["id"],
                "source_name": "商品 A.webp",
                "status": "complete",
                "error": None,
            }
        ],
        "delivery": {
            "renderPdf": True,
            "pdfStatus": "ready",
            "generatedAt": None,
            "files": [],
            "error": None,
        },
        "created_at": webapp.now_iso(),
    }
    webapp.save_batch(batch)
    webapp.assemble_delivery(batch)
    root = tmp_path / "_batches" / "batch-delivery" / "delivery"
    assert (root / "PNG" / "001-商品-A-p01.png").is_file()
    assert (root / "PNG" / "001-商品-A-p02.png").is_file()
    assert (root / "PDF" / "001-商品-A.pdf").is_file()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["items"][0]["selectedCandidate"] == "candidate-4"
    assert manifest["items"][0]["selectedScore"] == 0.93

    client = TestClient(webapp.app)
    response = client.get("/api/batches/batch-delivery/delivery/download")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "PNG/001-商品-A-p01.png" in names
        assert "PDF/001-商品-A.pdf" in names
        assert "manifest.csv" in names
        assert "manifest.json" in names
        assert all(not name.startswith("final/") for name in names)


def test_pdf_backfill_reuses_existing_sheets_without_pipeline_rerun(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
    webapp._batches.clear()
    job = webapp._create_job_record(
        input_mode="source",
        file_name="source.png",
        content=upload_bytes(),
        settings=webapp.pipeline.default_settings(),
        generation_prompt="prompt",
        batch_id="batch-backfill",
        render_pdf=False,
    )
    final_root = webapp.job_dir(job["id"]) / "final"
    (final_root / "white").mkdir(parents=True)
    (final_root / "transparent").mkdir(parents=True)
    white = Image.new("RGB", (120, 90), "white")
    white.save(final_root / "white" / "sheet-01.jpg", dpi=(300, 300))
    transparent = Image.new("RGBA", (120, 90), (0, 0, 0, 0))
    transparent.save(
        final_root / "transparent" / "sheet-01.png",
        dpi=(300, 300),
    )
    (final_root / "layout.json").write_text(
        json.dumps(
            {
                "delivery_render_version": 2,
                "files": {},
                "page_count": 1,
            }
        ),
        encoding="utf-8",
    )
    job["status"] = "complete"
    job["current_stage"] = "layout"
    job["selected_candidate"] = "candidate-1"
    job["candidates"] = [{"id": "candidate-1", "score": 0.9, "page_count": 1}]
    webapp.save_job(job)
    batch = {
        "id": "batch-backfill",
        "status": "complete",
        "items": [
            {
                "job_id": job["id"],
                "source_name": "source.png",
                "status": "complete",
                "error": None,
            }
        ],
        "delivery": {
            "renderPdf": False,
            "pdfStatus": "skipped",
            "generatedAt": None,
            "files": [],
            "error": None,
        },
        "created_at": webapp.now_iso(),
    }
    webapp.save_batch(batch)
    monkeypatch.setattr(
        webapp,
        "execute_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("不应重新运行生图或排版")
        ),
    )
    webapp.generate_batch_pdfs(batch["id"])
    assert (
        final_root / "pdf" / "print-sheets.pdf"
    ).is_file()
    assert webapp._jobs[job["id"]]["status"] == "complete"
    assert webapp._jobs[job["id"]]["pdf_status"] == "ready"
    assert webapp._batches[batch["id"]]["delivery"]["pdfStatus"] == "ready"


def test_batch_continues_after_failure_and_retry_skips_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
    webapp._batches.clear()
    settings = webapp.pipeline.default_settings()
    jobs = [
        webapp._create_job_record(
            input_mode="source",
            file_name=f"{index}.png",
            content=upload_bytes(),
            settings=settings,
            generation_prompt="prompt",
            batch_id="batch-test",
        )
        for index in range(3)
    ]
    batch = {
        "id": "batch-test",
        "status": "queued",
        "items": [
            {"job_id": job["id"], "source_name": f"{index}.png", "status": "queued", "error": None}
            for index, job in enumerate(jobs)
        ],
        "current_job_id": None,
        "error": None,
        "created_at": webapp.now_iso(),
    }
    webapp.save_batch(batch)
    calls = []

    def first_run(job_id, through_stage, from_stage=None):
        calls.append(job_id)
        job = webapp.require_job(job_id)
        job["status"] = "failed" if job_id == jobs[1]["id"] else "complete"
        job["error"] = "mock failure" if job["status"] == "failed" else None
        job["current_stage"] = "layout" if job["status"] == "complete" else "generate"
        webapp.save_job(job)

    monkeypatch.setattr(webapp, "execute_job", first_run)
    webapp.execute_batch(batch["id"])
    assert calls == [job["id"] for job in jobs]
    assert webapp._batches[batch["id"]]["status"] == "partial_success"

    retry_calls = []

    def retry_run(job_id, through_stage, from_stage=None):
        retry_calls.append(job_id)
        job = webapp.require_job(job_id)
        job["status"] = "complete"
        job["error"] = None
        job["current_stage"] = "layout"
        webapp.save_job(job)

    monkeypatch.setattr(webapp, "execute_job", retry_run)
    webapp.execute_batch(batch["id"])
    assert retry_calls == [jobs[1]["id"]]
    assert webapp._batches[batch["id"]]["status"] == "complete"


def test_review_annotation_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(webapp, "REVIEWS_DIR", tmp_path / "reviews")
    webapp._jobs.clear()
    client = TestClient(webapp.app)

    job_id = "20260730-000000-testjob1"
    generate_dir = tmp_path / job_id / "generate"
    generate_dir.mkdir(parents=True)
    (generate_dir / "master.png").write_bytes(upload_bytes())
    webapp._jobs[job_id] = {
        "id": job_id,
        "status": "complete",
        "created_at": "2026-07-30T00:00:00",
        "source_name": "demo.png",
        "settings": {"window_template": "double"},
        "generation_prompt_base": "测试 prompt 母本",
    }

    overview = client.get("/api/review/overview").json()
    assert overview["jobs"][0]["job_id"] == job_id
    assert overview["jobs"][0]["prompt_hash"]
    assert any(item["id"] == "divider_intrusion" for item in overview["defect_types"])
    assert "double" in overview["template_constraints"]

    response = client.post("/api/review/annotations", json={
        "job_id": job_id,
        "defect_types": ["divider_intrusion"],
        "severity": "high",
        "note": "分隔带右下被侵入",
    })
    assert response.status_code == 200
    record = response.json()
    assert record["prompt_hash"] == overview["jobs"][0]["prompt_hash"]
    snapshot = tmp_path / "reviews" / "prompts" / f"{record['prompt_hash']}.txt"
    assert snapshot.read_text(encoding="utf-8") == "测试 prompt 母本"

    refreshed = client.get("/api/review/overview").json()
    assert refreshed["annotation_total"] == 1
    assert refreshed["jobs"][0]["annotations"][0]["id"] == record["id"]

    assert client.post("/api/review/annotations", json={
        "job_id": job_id, "defect_types": ["nope"],
    }).status_code == 400
    assert client.post("/api/review/annotations", json={
        "job_id": job_id, "defect_types": [], "note": "",
    }).status_code == 400

    added = client.post("/api/review/defect-types", json={"label": "自定义类别"})
    assert added.status_code == 200
    assert client.post("/api/review/defect-types", json={"label": "自定义类别"}).status_code == 400

    assert client.delete(f"/api/review/annotations/{record['id']}").status_code == 200
    assert client.get("/api/review/overview").json()["annotation_total"] == 0


def test_batch_generate_only_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
    webapp._batches.clear()
    monkeypatch.setattr(webapp, "generation_configured", lambda: True)
    # 提示词迭代模式不应要求 ComfyUI 配置。
    monkeypatch.setattr(webapp, "comfyui_configured", lambda: False)

    class DeferredThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
        def start(self):
            pass

    monkeypatch.setattr(webapp.threading, "Thread", DeferredThread)
    client = TestClient(webapp.app)
    response = client.post(
        "/api/batches",
        data={
            "settings_json": "{}",
            "generation_prompt": "prompt",
            "through_stage": "generate",
        },
        files=[
            ("files", ("a.png", upload_bytes(), "image/png")),
            ("files", ("b.png", upload_bytes(), "image/png")),
        ],
    )
    assert response.status_code == 200
    batch = response.json()
    assert batch["through_stage"] == "generate"
    assert batch["delivery"]["renderPdf"] is False
    assert batch["delivery"]["pdfStatus"] == "skipped"

    executed = []
    monkeypatch.setattr(
        webapp, "execute_job",
        lambda job_id, through, from_stage=None: executed.append((job_id, through)) or webapp._jobs[job_id].update({"status": "complete"}),
    )
    delivery_calls = []
    monkeypatch.setattr(webapp, "assemble_delivery", lambda b: delivery_calls.append(b["id"]))
    webapp.execute_batch(batch["id"])
    assert all(through == "generate" for _, through in executed)
    assert len(executed) == 2
    assert delivery_calls == []
    assert webapp._batches[batch["id"]]["status"] == "complete"


def test_batch_full_mode_requires_comfyui(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
    webapp._batches.clear()
    monkeypatch.setattr(webapp, "generation_configured", lambda: True)
    monkeypatch.setattr(webapp, "comfyui_configured", lambda: False)
    client = TestClient(webapp.app)
    response = client.post(
        "/api/batches",
        data={"settings_json": "{}", "generation_prompt": "prompt"},
        files=[("files", ("a.png", upload_bytes(), "image/png"))],
    )
    assert response.status_code == 409


def test_brief_lab_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "BRIEF_DIR", tmp_path / "brief_runs")

    class InlineThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(webapp.threading, "Thread", InlineThread)
    received_prompts = []

    def fake_vlm(path, log=print, prompt=None):
        received_prompts.append(prompt)
        return ("codex-gpt-5.6-luna",
            "【反推重点】\n主角为测试雪人\n"
            "【衍生方向 1】\n方向一\n【生图提示词 1】\n提示词一\n"
            "【衍生方向 2】\n方向二\n【生图提示词 2】\n提示词二\n"
            "【衍生方向 3】\n方向三\n【生图提示词 3】\n提示词三")

    monkeypatch.setattr(webapp.brief_lab, "call_vlm", fake_vlm)

    def fake_generate(prompt, size, out_path, log=print, reference_url=None):
        # 创新简报五段组装：通用底线 / 空产品约束 / 版式 / 内容简报 / 规格（分栏×画布）。
        assert "主题表达不变" in prompt
        assert "【3 版式 · 大元素】" in prompt
        assert "【4 内容简报】" in prompt
        assert "【5 规格 · 1栏 · 无分隔带 × 1:1 方形】" in prompt
        assert "当前窗户模板硬约束" not in prompt
        assert "中间6%分隔带" not in prompt
        assert size == "1216x1216"
        out_path.write_bytes(b"fake-png")
        return {"ok": True, "image_url": "https://example/x.png"}

    monkeypatch.setattr(webapp.brief_lab, "generate_image", fake_generate)
    client = TestClient(webapp.app)

    response = client.post(
        "/api/brief",
        data={"innovation_prompt": "用户编辑后的创新反推提示词"},
        files={"file": ("s.png", upload_bytes(), "image/png")},
    )
    assert response.status_code == 200
    assert received_prompts == ["用户编辑后的创新反推提示词"]
    run_id = response.json()["id"]

    state = client.get(f"/api/brief/{run_id}").json()
    assert state["status"] == "ready"
    assert state["vlm_model"] == "codex-gpt-5.6-luna"
    assert state["summary"] == "主角为测试雪人"
    assert [item["index"] for item in state["directions"]] == [1, 2, 3]

    generated = client.post(
        f"/api/brief/{run_id}/generate",
        json={"index": 2, "prompt": "人工改过的提示词二"},
    )
    assert generated.status_code == 200
    state = client.get(f"/api/brief/{run_id}").json()
    direction = next(item for item in state["directions"] if item["index"] == 2)
    assert direction["status"] == "done"
    assert direction["prompt"] == "人工改过的提示词二"
    assert len(direction["image_urls"]) == 1
    assert client.get(direction["image_urls"][0]).status_code == 200
    assert client.get("/api/brief").json()[0]["id"] == run_id


def test_brief_batch_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "BRIEF_DIR", tmp_path / "brief_runs")

    class InlineThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(webapp.threading, "Thread", InlineThread)
    monkeypatch.setattr(
        webapp.brief_lab, "call_vlm",
        lambda path, log=print, prompt=None: ("codex-gpt-5.6-luna",
            "【反推重点】\n要点\n"
            "【衍生方向 1】\n甲\n【生图提示词 1】\n提示甲\n"
            "【衍生方向 2】\n乙\n【生图提示词 2】\n提示乙\n"
            "【衍生方向 3】\n丙\n【生图提示词 3】\n提示丙"),
    )
    sizes = []

    def fake_generate(prompt, size, out_path, log=print, reference_url=None):
        sizes.append(size)
        assert "【4 内容简报】" in prompt
        out_path.write_bytes(b"fake")
        return {"ok": True}

    monkeypatch.setattr(webapp.brief_lab, "generate_image", fake_generate)
    client = TestClient(webapp.app)
    response = client.post(
        "/api/brief/batch",
        data={
            "auto_generate": "true",
            "prompt_style": "large_elements",
            "canvas_id": "87:73",
        },
        files=[
            ("files", ("a.png", upload_bytes(), "image/png")),
            ("files", ("b.png", upload_bytes(), "image/png")),
        ],
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["id"].startswith("brief-batch-")
    assert payload["status"] == "completed"
    assert len(payload["items"]) == 2

    for run_id in payload["ids"]:
        state = client.get(f"/api/brief/{run_id}").json()
        assert state["status"] == "ready"
        assert all(item["status"] == "done" for item in state["directions"])
        assert all(len(item["image_urls"]) == 1 for item in state["directions"])
        assert "大元素" in state["directions"][0]["config_label"]
        assert state["directions"][0]["last_prompt_url"].endswith("-prompt.txt")
    assert sizes and all(size == "1392x1168" for size in sizes)
    batch_id = payload["id"]
    batch = client.get(f"/api/brief/batches/{batch_id}").json()
    assert len(batch["output_urls"]) == 6
    assert all(client.get(url).status_code == 200 for url in batch["output_urls"])
    assert client.get(f"/api/brief/batches/{batch_id}/download").status_code == 200
    history_zip = client.get("/api/brief/history/download")
    assert history_zip.status_code == 200
    assert len(history_zip.content) > 0
    assert client.get("/api/brief/batches").json()[0]["id"] == batch_id


def test_brief_batch_pause_cancel_and_retry_controls(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "BRIEF_DIR", tmp_path / "brief_runs")
    run_id = webapp._create_brief_run_record("a.png", upload_bytes(), "queued")
    batch_id = "brief-batch-20260803-120000-abcdef"
    (webapp._brief_batch_dir(batch_id) / "outputs").mkdir(parents=True)
    webapp._save_brief_batch(batch_id, {
        "created_at": webapp.now_iso(),
        "status": "queued",
        "auto_generate": True,
        "compose": {},
        "run_ids": [run_id],
        "total": 1,
        "attempt": 1,
        "pause_requested": False,
        "cancel_requested": False,
        "output_files": {},
    })
    monkeypatch.setattr(webapp, "_start_brief_batch_worker", lambda batch_id: True)
    client = TestClient(webapp.app)

    paused = client.post(f"/api/brief/batches/{batch_id}/pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    cancelled = client.post(f"/api/brief/batches/{batch_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    retried = client.post(f"/api/brief/batches/{batch_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "queued"
    assert retried.json()["attempt"] == 2


def test_brief_prompt_preview_is_visible_five_part_composition(tmp_path, monkeypatch):
    # 隔离 prompt-defaults.json 覆盖层，避免读到真实使用中的已保存配置。
    monkeypatch.setattr(webapp, "BRIEF_DIR", tmp_path / "brief_runs")
    client = TestClient(webapp.app)
    defaults = client.get("/api/defaults").json()
    assert [item["id"] for item in defaults["brief_prompt_styles"]] == [
        "large_elements", "scene", "white_silhouette_scene", "small_scatter",
    ]
    assert defaults["brief_prompt_defaults"]["product_prompt"] == ""
    assert defaults["brief_prompt_defaults"]["core_prompt"]
    assert defaults["brief_prompt_defaults"]["layout_prompt"]
    assert "【反推重点】" in defaults["brief_innovation_prompt"]

    response = client.post(
        "/api/brief/prompts/preview",
        json={
            "prompt": "测试内容简报",
            "prompt_style": "small_scatter",
            "canvas_id": "3:4",
            "core_prompt": "测试通用底线",
            "product_prompt": "",
            "layout_prompt": "测试满铺版式",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["sections"]] == [
        "core", "product", "layout", "content", "spec",
    ]
    assert "测试通用底线" in payload["prompt"]
    assert "测试满铺版式" in payload["prompt"]
    assert "测试内容简报" in payload["prompt"]
    assert "【2 产品约束】\n（无）" in payload["prompt"]
    assert "宽3:高4" in payload["prompt"]
    # 默认分栏骨架为 1栏：规格段带分栏标签，但不出现多栏分隔带要求。
    assert "【5 规格 · 1栏 · 无分隔带 × 竖 3:4】" in payload["prompt"]
    assert "约占画布总宽度6%" not in payload["prompt"]

    pane2_preview = client.post(
        "/api/brief/prompts/preview",
        json={
            "prompt": "测试内容简报",
            "prompt_style": "small_scatter",
            "canvas_id": "1:1",
            "frame_id": "pane2",
        },
    ).json()
    assert "【5 规格 · 2栏 · 中间6%分隔带 × 1:1 方形】" in pane2_preview["prompt"]
    assert "约占画布总宽度6%" in pane2_preview["prompt"]
    assert "约占画布宽度2%的纯白安全边" in pane2_preview["prompt"]
    assert "每一侧单独看必须是构图完整、可独立成品" in pane2_preview["prompt"]
    silhouette = next(
        item for item in defaults["brief_prompt_styles"] if item["id"] == "white_silhouette_scene"
    )["text"]
    assert "纯白色" in silhouette
    assert "#1f2937" in silhouette
    assert "临时绘制为均匀纯黑" not in silhouette
    scene = next(
        item for item in defaults["brief_prompt_styles"] if item["id"] == "scene"
    )["text"]
    assert "场景空间骨架" in scene
    assert "不是让母版画布把普通完整主体意外截断" in scene
    scatter = next(
        item for item in defaults["brief_prompt_styles"] if item["id"] == "small_scatter"
    )["text"]
    assert "母题族" in scatter
    assert "弱层级而非单一尺寸" in scatter


def test_brief_prompt_defaults_and_direction_can_be_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "BRIEF_DIR", tmp_path / "brief_runs")
    client = TestClient(webapp.app)

    saved = client.patch(
        "/api/brief/prompts/defaults",
        json={"field": "core_prompt", "value": "永久通用底线"},
    )
    assert saved.status_code == 200
    saved = client.patch(
        "/api/brief/prompts/defaults",
        json={
            "field": "layout_prompt",
            "value": "永久满铺版式",
            "prompt_style": "small_scatter",
        },
    )
    assert saved.status_code == 200
    defaults = client.get("/api/defaults").json()
    assert defaults["brief_prompt_defaults"]["core_prompt"] == "永久通用底线"
    assert next(
        item for item in defaults["brief_prompt_styles"] if item["id"] == "small_scatter"
    )["text"] == "永久满铺版式"

    run_id = webapp._create_brief_run_record("a.png", upload_bytes(), "ready")
    meta = webapp._load_brief_meta(run_id)
    meta["directions"] = [{"index": 1, "prompt": "旧简报", "status": "idle", "images": []}]
    webapp._save_brief_meta(run_id, meta)
    response = client.patch(
        f"/api/brief/{run_id}/directions/1",
        json={"prompt": "永久保存后的内容简报"},
    )
    assert response.status_code == 200
    assert response.json()["directions"][0]["prompt"] == "永久保存后的内容简报"


def test_brief_fallback_still_produces_three_directions(tmp_path, monkeypatch):
    """VLM 全链失败时必须仍给出三个差异化方向，并把失败原因暴露到页面。"""
    monkeypatch.setattr(webapp, "BRIEF_DIR", tmp_path / "brief_runs")

    class InlineThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(webapp.threading, "Thread", InlineThread)

    def dead_vlm(path, log=print, prompt=None):
        raise webapp.brief_lab.VLMChainError(
            "VLM 全部模型失败：codex-gpt-5.5: HTTP 502 upstream_error；gpt-4o: HTTP 502"
        )

    monkeypatch.setattr(webapp.brief_lab, "call_vlm", dead_vlm)
    client = TestClient(webapp.app)
    response = client.post("/api/brief", files={"file": ("s.png", upload_bytes(), "image/png")})
    assert response.status_code == 200
    state = client.get(f"/api/brief/{response.json()['id']}").json()

    assert state["status"] == "ready"
    assert state["fallback"] is True
    assert state["vlm_model"] is None
    assert [item["index"] for item in state["directions"]] == [1, 2, 3]
    assert len({item["prompt"] for item in state["directions"]}) == 3
    # 失败原因必须能在页面上看到，而不是只留在服务日志里。
    assert "HTTP 502" in state["summary"]
    assert "兜底" in state["summary"] or "降级" in state["summary"]


def test_brief_vlm_chain_prefers_verified_models(monkeypatch):
    monkeypatch.delenv("LP_BRIEF_VLM_MODELS", raising=False)
    chain = webapp.brief_lab.vlm_chain()
    # 2026-08-06 实测 codex-gpt-5.6-* 网关侧 502，不应再占据链首。
    assert chain[0] != "codex-gpt-5.6-luna"
    assert "codex-gpt-5.5" in chain
    assert "gpt-4o" in chain
    monkeypatch.setenv("LP_BRIEF_VLM_MODELS", "model-a, model-b")
    assert webapp.brief_lab.vlm_chain() == ["model-a", "model-b"]


def test_brief_restart_recovers_interrupted_state(tmp_path, monkeypatch):
    """重启后残留的“进行中”状态必须被标记为中断，否则前端永远显示队列未完成。"""
    monkeypatch.setattr(webapp, "BRIEF_DIR", tmp_path / "brief_runs")
    run_dir = tmp_path / "brief_runs" / "brief-20260806-120000-aaaaaa"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "created_at": "2026-08-06T12:00:00",
        "source_file": "source.png",
        "status": "briefing",
        "directions": [
            {"index": 1, "status": "generating", "prompt": "p1", "images": []},
            {"index": 2, "status": "done", "prompt": "p2", "images": ["a.png"]},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    batch_dir = tmp_path / "brief_runs" / "_batches" / "brief-batch-20260806-120000-bbbbbb"
    batch_dir.mkdir(parents=True)
    (batch_dir / "batch.json").write_text(json.dumps({
        "created_at": "2026-08-06T12:00:00",
        "status": "running",
        "run_ids": [],
        "current_run_id": "brief-20260806-120000-aaaaaa",
    }, ensure_ascii=False), encoding="utf-8")

    webapp.recover_interrupted_brief_state()

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "interrupted"
    assert meta["directions"][0]["status"] == "failed"
    assert "重启" in meta["directions"][0]["error"]
    # 已完成的方向不能被动。
    assert meta["directions"][1]["status"] == "done"

    batch = json.loads((batch_dir / "batch.json").read_text(encoding="utf-8"))
    assert batch["status"] == "interrupted"
    assert batch["current_run_id"] is None
