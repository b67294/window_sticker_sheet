"""Run an opt-in end-to-end smoke test against a running workbench server.

Usage:
    python tests/real_smoke.py path/to/master.png
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8790")
    args = parser.parse_args()

    for attempt in range(30):
        try:
            health = requests.get(f"{args.base_url}/api/health", timeout=2)
            health.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 29:
                raise
            time.sleep(0.5)

    settings = {
        "install_width_mm": 500,
        "install_height_mm": 400,
        "preview_dpi": 50,
        "output_dpi": 72,
        "auto_group_gap_mm": 0.5,
        "min_area_px": 45,
    }
    with args.image.open("rb") as handle:
        response = requests.post(
            f"{args.base_url}/api/jobs",
            files={"file": (args.image.name, handle, "image/png")},
            data={"input_mode": "master", "settings_json": json.dumps(settings)},
            timeout=30,
        )
    response.raise_for_status()
    job = response.json()

    response = requests.post(
        f"{args.base_url}/api/jobs/{job['id']}/run",
        json={"through_stage": "layout", "sync": True},
        timeout=120,
    )
    response.raise_for_status()
    job = response.json()
    summary = {
        "job_id": job["id"],
        "stage": job["current_stage"],
        "primitive_count": len(job.get("primitives", [])),
        "group_count": len(job.get("groups", [])),
        "selected_candidate_id": job.get("selected_candidate"),
        "candidates": [
            {
                "id": candidate["id"],
                "pages": candidate["page_count"],
                "utilization": candidate["utilization"],
                "balance": candidate["balance"],
                "score": candidate["score"],
            }
            for candidate in job.get("candidates", [])
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
