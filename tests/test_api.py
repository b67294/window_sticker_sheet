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


def test_direct_master_job(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RUNS_DIR", tmp_path)
    webapp._jobs.clear()
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

    other = next(item for item in job["candidates"] if item["id"] != job["selected_candidate"])
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
