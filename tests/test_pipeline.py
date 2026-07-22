from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import box, mapping, shape

import pipeline


def test_default_installation_size_is_portrait_window():
    defaults = pipeline.default_settings()
    assert defaults["install_width_mm"] == 800.0
    assert defaults["install_height_mm"] == 1200.0
    assert defaults["compactness_weight"] == 0.65
    assert defaults["alignment_weight"] == 0.25
    assert defaults["balance_weight"] == 0.10
    assert defaults["max_shrink_ratio"] == 0.08


def test_layout_scaling_rebuilds_fixed_mm_buffers():
    source = {
        "group_id": "g001",
        "visible": mapping(box(2.5, 2.5, 102.5, 52.5)),
        "visible_offset_mm": [2.5, 2.5],
        "asset_size_mm": [100.0, 50.0],
        "asset_path": "geometry/groups/g001.png",
        "rotatable": False,
        "filler": False,
        "max_copies": 0,
    }
    scaled = pipeline._scaled_layout_source(source, pipeline.default_settings(), 0.92)
    visible = shape(scaled["visible"])
    cutline = shape(scaled["cutline"])
    occupancy = shape(scaled["occupancy"])
    assert abs((visible.bounds[2] - visible.bounds[0]) - 92.0) < 0.01
    assert abs((cutline.bounds[2] - cutline.bounds[0]) - 95.0) < 0.01
    assert abs((occupancy.bounds[2] - occupancy.bounds[0]) - 97.0) < 0.01
    assert scaled["asset_size_mm"] == [92.0, 46.0]


def test_candidate_rank_prefers_pages_then_larger_scale():
    base = {"score": 0.8, "largest_void_ratio": 0.1}
    fewer_pages = {**base, "page_count": 2, "layout_scale": 0.92}
    larger_scale = {**base, "page_count": 3, "layout_scale": 1.0}
    assert min([larger_scale, fewer_pages], key=pipeline._candidate_rank) is fewer_pages
    same_pages_small = {**base, "page_count": 3, "layout_scale": 0.92, "score": 0.95}
    assert min([larger_scale, same_pages_small], key=pipeline._candidate_rank) is larger_scale


def make_master(path: Path):
    scale = 4
    image = Image.new("RGB", (600 * scale, 400 * scale), (0, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((40 * scale, 50 * scale, 180 * scale, 190 * scale), fill=(210, 15, 32))
    draw.rounded_rectangle((250 * scale, 50 * scale, 380 * scale, 180 * scale), radius=18 * scale, fill=(245, 245, 245))
    draw.polygon([(470 * scale, 45 * scale), (520 * scale, 175 * scale), (420 * scale, 175 * scale)], fill=(185, 190, 195))
    image.resize((600, 400), Image.Resampling.LANCZOS).save(path)


def settings():
    value = pipeline.default_settings()
    value.update(
        {
            "install_width_mm": 260,
            "install_height_mm": 175,
            "sheet_width_mm": 180,
            "sheet_height_mm": 140,
            "sheet_margin_mm": 4,
            "cut_offset_mm": 1.5,
            "spacing_mm": 2,
            "group_gap_mm": 0.2,
            "min_component_area": 30,
            "preview_dpi": 30,
            "output_dpi": 30,
        }
    )
    return value


def test_full_algorithm_pipeline(tmp_path):
    master = tmp_path / "master.png"
    make_master(master)
    job = {"artifacts": [], "primitives": [], "groups": [], "geometry": [], "candidates": []}
    config = settings()

    _, metrics = pipeline.run_chroma_key(master, tmp_path, config)
    assert metrics["key_rgb"][1] > 240
    alpha = np.array(Image.open(tmp_path / "key" / "clean-alpha.png"))
    assert alpha[0, 0] == 0
    assert alpha[120, 100] > 240

    _, primitives, groups = pipeline.run_components(job, tmp_path, config)
    assert len(primitives) == 3
    assert len(groups) == 3
    job["primitives"] = primitives
    job["groups"] = groups

    _, geometry = pipeline.run_geometry(job, tmp_path, config)
    assert len(geometry) == 3
    for item in geometry:
        visible = shape(item["visible"])
        cutline = shape(item["cutline"])
        assert cutline.area > visible.area
    job["geometry"] = geometry

    _, candidates, selected = pipeline.run_layout(job, tmp_path, config)
    assert len(candidates) == 4
    assert selected in {item["id"] for item in candidates}
    for candidate in candidates:
        assert candidate["layout_scale"] >= 0.92
        assert 0 <= candidate["compactness"] <= 1
        assert 0 <= candidate["alignment"] <= 1
        assert 0 <= candidate["largest_void_ratio"] <= 1
        pages = {}
        for placement in candidate["placements"]:
            pages.setdefault(placement["page"], []).append(shape(placement["occupancy"]))
        for polygons in pages.values():
            for index, left in enumerate(polygons):
                for right in polygons[index + 1 :]:
                    assert left.intersection(right).area < 1e-5


def test_alpha_passthrough_preserves_soft_alpha(tmp_path):
    source = np.zeros((80, 120, 4), dtype=np.uint8)
    source[10:40, 12:48, :3] = (220, 20, 30)
    source[10:40, 12:48, 3] = 255
    source[50:70, 70:105, :3] = (250, 250, 250)
    source[50:70, 70:105, 3] = 255
    source[9, 20:40, :3] = (170, 60, 40)
    source[9, 20:40, 3] = 93
    source[0, 0, :3] = (0, 255, 0)  # Hidden RGB must not leak from alpha-zero pixels.
    input_path = tmp_path / "transparent.png"
    Image.fromarray(source, "RGBA").save(input_path)

    _, metrics = pipeline.run_alpha_passthrough(input_path, tmp_path, settings())
    result_alpha = np.array(Image.open(tmp_path / "key" / "clean-alpha.png"))
    foreground = np.array(Image.open(tmp_path / "key" / "foreground.png").convert("RGBA"))

    assert metrics["mode"] == "alpha_passthrough"
    assert np.array_equal(result_alpha, source[:, :, 3])
    assert foreground[9, 25, 3] == 93
    assert foreground[0, 0].tolist() == [0, 0, 0, 0]


def test_auto_group_respects_distance():
    primitives = [
        {"id": "p001", "bbox": [0, 0, 10, 10], "contour": [[0, 0], [10, 0], [10, 10], [0, 10]]},
        {"id": "p002", "bbox": [12, 0, 10, 10], "contour": [[12, 0], [22, 0], [22, 10], [12, 10]]},
        {"id": "p003", "bbox": [80, 0, 10, 10], "contour": [[80, 0], [90, 0], [90, 10], [80, 10]]},
    ]
    groups = pipeline.auto_group_primitives(primitives, mm_px=1.0, gap_mm=3.0, master_width=100, master_height=100)
    sizes = sorted(len(item["primitive_ids"]) for item in groups)
    assert sizes == [1, 2]
    assert all(group["rotatable"] is False for group in groups)


def test_geometry_uses_component_bbox_when_contour_bounds_are_smaller(tmp_path):
    (tmp_path / "key").mkdir()
    (tmp_path / "components" / "masks").mkdir(parents=True)
    foreground = np.zeros((60, 50, 4), dtype=np.uint8)
    foreground[5:45, 10:30] = (220, 20, 30, 255)
    Image.fromarray(foreground, "RGBA").save(tmp_path / "key" / "foreground.png")
    Image.fromarray(np.full((40, 20), 255, dtype=np.uint8), "L").save(tmp_path / "components" / "masks" / "p001.png")

    job = {
        "primitives": [
            {
                "id": "p001",
                "bbox": [10, 5, 20, 40],
                # A repaired/self-touching contour can have bounds smaller than
                # the original connected-component mask.
                "contour": [[10, 5], [29, 5], [29, 20], [10, 20]],
                "mask_path": "components/masks/p001.png",
            }
        ],
        "groups": [
            {
                "id": "g001",
                "primitive_ids": ["p001"],
                "bbox": [10, 5, 20, 40],
                "active": True,
                "rotatable": False,
                "filler": False,
                "max_copies": 2,
            }
        ],
    }
    _, geometry = pipeline.run_geometry(job, tmp_path, settings())
    assert geometry[0]["source_bbox_px"] == [10, 5, 20, 40]
    asset = np.array(Image.open(tmp_path / geometry[0]["asset_path"]).convert("RGBA"))
    assert asset.shape[:2] == (40, 20)
    assert asset[-1, -1, 3] == 255
