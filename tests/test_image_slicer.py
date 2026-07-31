import io

from PIL import Image

import image_slicer


def _png_bytes(width, height, color=(120, 180, 200)):
    image = Image.new("RGB", (width, height), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_plan_matches_reference_layout():
    # 参考图场景：整图约 87:73，小块 29:36.5 → 应选 3 列 × 2 行。
    plan, _ = image_slicer.choose_plan(870, 730, 29, 36.5)
    assert (plan.columns, plan.rows) == (3, 2)
    assert plan.crop_loss < 0.01
    assert plan.tile_width == 290
    assert plan.tile_height == 365


def test_plan_prefers_fewer_tiles_on_tie():
    # 2:1 的图配 1:1 小块：2×1 与 4×2 都零损失，应选块数更少的 2×1。
    plan, _ = image_slicer.choose_plan(1000, 500, 1, 1)
    assert (plan.columns, plan.rows) == (2, 1)


def test_forced_grid_and_invalid_ratio(tmp_path):
    plan, _ = image_slicer.choose_plan(870, 730, 29, 36.5, columns=6, rows=4)
    assert (plan.columns, plan.rows) == (6, 4)
    try:
        image_slicer.choose_plan(870, 730, 0, 36.5)
        raise AssertionError("应当拒绝非正比例")
    except ValueError:
        pass


def test_slice_image_writes_tiles_and_preview(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(_png_bytes(870, 730))
    result = image_slicer.slice_image(source, tmp_path / "out", 29, 36.5)
    assert result["plan"]["columns"] == 3
    assert result["plan"]["rows"] == 2
    tiles = sorted((tmp_path / "out" / "tiles").glob("*.png"))
    assert len(tiles) == 6
    with Image.open(tiles[0]) as tile:
        assert tile.size == (290, 365)
    assert (tmp_path / "out" / "preview.jpg").is_file()
    assert len(result["alternatives"]) >= 1


def test_resize_stretch_defaults_to_longer_side(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(_png_bytes(1216, 1216))
    # 竖版模板：高87 宽73 → 输出较长边(高)=1216，宽按比例推导。
    result = image_slicer.resize_image(source, tmp_path / "out", 73, 87)
    assert result["mode"] == "stretch"
    assert result["output_size"] == [1020, 1216]
    with Image.open(tmp_path / "out" / "resized.png") as image:
        assert image.size == (1020, 1216)

    # 横版模板：高73 宽87 → 输出较长边(宽)=1216。
    result = image_slicer.resize_image(source, tmp_path / "out2", 87, 73)
    assert result["output_size"] == [1216, 1020]


def test_resize_crop_mode_and_explicit_size(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(_png_bytes(1000, 500))
    result = image_slicer.resize_image(source, tmp_path / "out", 1, 1, mode="crop", target_width=400)
    assert result["output_size"] == [400, 400]
    with Image.open(tmp_path / "out" / "resized.png") as image:
        assert image.size == (400, 400)
    try:
        image_slicer.resize_image(source, tmp_path / "out3", 1, 1, mode="nope")
        raise AssertionError("应当拒绝未知模式")
    except ValueError:
        pass


def test_slice_image_writes_combined_pdf(tmp_path):
    from pypdf import PdfReader

    source = tmp_path / "source.png"
    source.write_bytes(_png_bytes(870, 730))
    result = image_slicer.slice_image(source, tmp_path / "out", 29, 36.5)
    assert result["pdf"] == "tiles.pdf"
    reader = PdfReader(tmp_path / "out" / "tiles.pdf")
    # 第一页是切分示意图，之后每块一页。
    assert len(reader.pages) == result["plan"]["tile_count"] + 1 == 7
    overview_width = float(reader.pages[0].mediabox.width)
    overview_height = float(reader.pages[0].mediabox.height)
    assert abs(overview_width / overview_height - 870 / 730) < 0.01
    width_pt = float(reader.pages[1].mediabox.width)
    height_pt = float(reader.pages[1].mediabox.height)
    # 300 DPI 折算：290px → 69.6pt，365px → 87.6pt，比例应等于 29:36.5。
    assert abs(width_pt / height_pt - 29 / 36.5) < 0.01
