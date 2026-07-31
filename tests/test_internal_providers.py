import io
import json

from PIL import Image, ImageDraw

import generation
import semantic_grouping


class FakeResponse:
    def __init__(self, payload=None, content=b"", status_code=200):
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.text = json.dumps(payload or {})

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.text)


def image_bytes(mode="RGB"):
    image = Image.new(mode, (128, 96), (255, 0, 255, 255) if mode == "RGBA" else (255, 0, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 70, 70), fill=(220, 30, 40, 255) if mode == "RGBA" else (220, 30, 40))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_default_prompt_preserves_theme_innovates_and_builds_scene_modules():
    prompt = generation.DEFAULT_PROMPT
    assert "至少四项明显变化" in prompt
    assert "不要一比一复刻" in prompt
    assert "主题表达不变" in prompt
    assert "不要生成彼此无关" in prompt
    assert "模块内部可以合理接触" in prompt
    assert "不同模块之间必须留出" in prompt
    assert "具体画布比例与窗格结构" in prompt
    assert "左右双竖窗" not in prompt
    assert "【元素完整性】" in prompt
    assert "【边缘互动】" in prompt
    assert "探头、半身、局部进入" in prompt
    assert "独立贴纸模块" in prompt
    assert "文字、面部、关键识别特征" in prompt
    assert "画布四周必须保留连续可见的纯白安全边距" in prompt
    assert "#ffffff" in prompt


def test_prompt_styles_compose_base_plus_style():
    styles = generation.list_prompt_styles()
    style_ids = [item["id"] for item in styles]
    assert style_ids[0] == "scene"
    assert {"scene", "large_elements", "small_scatter"} <= set(style_ids)
    assert all(item["label"] for item in styles)

    assert generation.compose_base_prompt() == generation.DEFAULT_PROMPT
    scene = generation.compose_base_prompt("scene")
    large = generation.compose_base_prompt("large_elements")
    scatter = generation.compose_base_prompt("small_scatter")
    # 基础层在每种样式里都存在。
    for prompt in (scene, large, scatter):
        assert "主题表达不变" in prompt
        assert "#ffffff" in prompt
    # 场景式专有条款不得泄漏进其他样式。
    assert "贴好之后的窗户场景" in scene
    assert "贴好之后的窗户场景" not in large
    assert "贴好之后的窗户场景" not in scatter
    # 满铺样式与场景式的图标集合禁令互斥。
    assert "不要生成彼此无关" not in scatter
    assert "满铺" in scatter
    with __import__("pytest").raises(ValueError, match="未知 prompt 版式"):
        generation.compose_base_prompt("nope")


def test_generation_prompt_appends_template_constraint_without_size_block():
    prompt = generation.render_generation_prompt(
        generation.DEFAULT_PROMPT,
        {"window_template": "single", "install_width_mm": 600, "install_height_mm": 800},
    )
    assert generation.SIZE_CONSTRAINT_MARKER not in prompt
    assert prompt.count(generation.TEMPLATE_CONSTRAINT_MARKER) == 1
    assert "mm" not in prompt.split(generation.TEMPLATE_CONSTRAINT_MARKER)[1]


def test_generation_prompt_strips_stale_markers_and_stays_idempotent():
    # 老任务的 base 里可能残留旧版尺寸段，重渲染必须清掉且幂等。
    stale_base = (
        "自定义创新要求\n\n"
        f"{generation.TEMPLATE_CONSTRAINT_MARKER}：单窗】\n旧模板段\n\n"
        f"{generation.SIZE_CONSTRAINT_MARKER}\n统一为 450 × 600 mm 的旧尺寸段"
    )
    rendered = generation.render_generation_prompt(
        stale_base,
        {"window_template": "double"},
    )
    assert rendered.startswith("自定义创新要求")
    assert "450 × 600 mm" not in rendered
    assert generation.SIZE_CONSTRAINT_MARKER not in rendered
    assert rendered.count(generation.TEMPLATE_CONSTRAINT_MARKER) == 1
    again = generation.render_generation_prompt(rendered, {"window_template": "double"})
    assert again == rendered


def test_auto_generation_size_uses_default_window_ratio(tmp_path, monkeypatch):
    source = tmp_path / "square.png"
    source.write_bytes(image_bytes())
    monkeypatch.setenv("LP_IMAGE_SIZE", "auto")

    assert generation._choose_generation_size(source) == "1216x1216"
    assert generation._choose_generation_size(source, {"window_template": "single"}) == "1056x1408"
    assert generation._choose_generation_size(source, {"window_template": "single_portrait"}) == "1168x1392"
    assert generation._choose_generation_size(source, {"window_template": "single_landscape"}) == "1392x1168"


def test_template_prompts_are_mutually_exclusive():
    single = generation.render_generation_prompt(
        "通用创新要求",
        {"window_template": "single", "install_width_mm": 450, "install_height_mm": 600},
    )
    double = generation.render_generation_prompt(
        "通用创新要求",
        {"window_template": "double", "install_width_mm": 600, "install_height_mm": 600},
    )
    portrait = generation.render_generation_prompt(
        "通用创新要求",
        {"window_template": "single_portrait"},
    )
    landscape = generation.render_generation_prompt(
        "通用创新要求",
        {"window_template": "single_landscape"},
    )
    custom = generation.render_generation_prompt(
        "通用创新要求",
        {"window_template": "custom", "frame_id": "pane3", "canvas_id": "1:1"},
    )
    assert "宽3:高4的竖版" in single
    assert "禁止生成贯穿全高的中央纯白分隔带" in single
    assert "左右两个竖向内容区" not in single
    assert "2栏 · 中间6%分隔带 × 1:1 方形" in double
    assert "约占画布总宽度6%" in double
    assert "不得跨越或侵入该分隔带" in double
    assert "宽73:高87" in portrait
    assert "宽87:高73" in landscape
    assert "横向延展" in landscape
    for prompt in (portrait, landscape):
        assert "禁止生成贯穿全高的中央纯白分隔带" in prompt
        assert "约占画布总宽度6%" not in prompt
    # 自定义组合：3栏 × 1:1
    assert "三个竖向内容区" in custom
    assert "5%" in custom
    assert custom.count(generation.TEMPLATE_CONSTRAINT_MARKER) == 1


def test_explicit_generation_size_must_match_template(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(image_bytes())
    monkeypatch.setenv("LP_IMAGE_SIZE", "1056x1408")
    with __import__("pytest").raises(ValueError, match="与窗户模板 double"):
        generation._choose_generation_size(source, {"window_template": "double"})


def test_direct_image_provider_uploads_reference_and_saves_trace(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(image_bytes())
    monkeypatch.setenv("LP_IMAGE_PROVIDER", "direct")
    monkeypatch.setenv("LP_IMAGE_DIRECT_URL", "https://image.example/generate")
    monkeypatch.setenv("LP_IMAGE_UPLOAD_URL", "https://upload.example/image")
    monkeypatch.setenv("LP_IMAGE_SIZE", "1024x1024")
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if "upload" in url:
            assert isinstance(kwargs["json"]["picBytes"], list)
            return FakeResponse({"data": "https://cdn.example/reference.png"})
        assert kwargs["json"]["referenceImages"] == ["https://cdn.example/reference.png"]
        return FakeResponse({
            "success": 1,
            "requestId": "trace-1",
            "data": {
                "requestId": "image-1",
                "model": "gpt-image-2",
                "imageUrl": "https://cdn.example/result.png",
                "durationMs": 1234,
                "tokensUsed": 99,
            },
        })

    monkeypatch.setattr(generation.requests, "post", fake_post)
    monkeypatch.setattr(generation.requests, "get", lambda *args, **kwargs: FakeResponse(content=image_bytes()))
    master, artifacts, metadata = generation.generate_master(source, tmp_path)

    assert master.is_file()
    assert metadata["provider"] == "codex-gpt-image-2-direct"
    assert metadata["request_id"] == "trace-1"
    assert metadata["duration_ms"] == 1234
    assert len(calls) == 2
    summary = json.loads((tmp_path / "generate" / "request-summary.json").read_text(encoding="utf-8"))
    assert summary["referenceImages"][0].startswith("<uploaded:")
    assert any(item["name"] == "generation-upload" for item in artifacts)


def test_semantic_provider_parses_fenced_json_and_merges_groups(tmp_path, monkeypatch):
    (tmp_path / "key").mkdir()
    (tmp_path / "components").mkdir()
    (tmp_path / "key" / "foreground.png").write_bytes(image_bytes("RGBA"))
    (tmp_path / "components" / "components-overlay.png").write_bytes(image_bytes())
    primitives = [
        {"id": "p001", "bbox": [10, 10, 20, 20]},
        {"id": "p002", "bbox": [40, 10, 20, 20]},
        {"id": "p003", "bbox": [80, 10, 20, 20]},
    ]
    groups = [
        {"id": "g001", "primitive_ids": ["p001"], "bbox": [10, 10, 20, 20], "active": True},
        {"id": "g002", "primitive_ids": ["p002"], "bbox": [40, 10, 20, 20], "active": True},
        {"id": "g003", "primitive_ids": ["p003"], "bbox": [80, 10, 20, 20], "active": True},
    ]
    monkeypatch.setenv("LP_VISION_BASE_URL", "https://vision.example/v1/chat/completions")
    monkeypatch.setenv("LP_VISION_TOKEN", "secret-client-key")
    monkeypatch.setattr(semantic_grouping.requests, "post", lambda *args, **kwargs: FakeResponse({
        "id": "chatcmpl-1",
        "choices": [{"message": {"content": "```json\n{\"semantic_groups\":[{\"members\":[\"p001\",\"p002\",\"missing\"],\"mode\":\"rigid\",\"confidence\":0.97,\"reason\":\"同一短语\"}]}\n```"}}],
    }))

    artifacts, updated, metadata = semantic_grouping.infer_and_apply_semantic_groups(
        tmp_path, primitives, groups, {"semantic_min_confidence": 0.9}
    )
    semantic = next(group for group in updated if group.get("origin") == "semantic")
    assert semantic["primitive_ids"] == ["p001", "p002"]
    assert semantic["max_copies"] == 0
    assert metadata["applied_count"] == 1
    assert metadata["response_id"] == "chatcmpl-1"
    assert metadata["fallback_used"] is False
    request_text = (tmp_path / "components" / "semantic" / "request-summary.json").read_text(encoding="utf-8")
    assert "secret-client-key" not in request_text
    assert "<data-url:" in request_text
    assert any(item["name"] == "semantic-relations" for item in artifacts)


def test_semantic_provider_uses_ordered_chain_before_gpt4o(tmp_path, monkeypatch):
    (tmp_path / "key").mkdir()
    (tmp_path / "components").mkdir()
    (tmp_path / "key" / "foreground.png").write_bytes(image_bytes("RGBA"))
    (tmp_path / "components" / "components-overlay.png").write_bytes(image_bytes())
    primitives = [
        {"id": "p001", "bbox": [10, 10, 20, 20]},
        {"id": "p002", "bbox": [40, 10, 20, 20]},
    ]
    groups = [
        {"id": "g001", "primitive_ids": ["p001"], "bbox": [10, 10, 20, 20], "active": True},
        {"id": "g002", "primitive_ids": ["p002"], "bbox": [40, 10, 20, 20], "active": True},
    ]
    monkeypatch.setenv("LP_VISION_BASE_URL", "https://vision.example/v1/chat/completions")
    monkeypatch.setenv("LP_VISION_TOKEN", "secret-client-key")
    monkeypatch.setenv(
        "LP_VISION_MODELS",
        "codex-gpt-5.6-sol,codex-gpt-5.6-luna,codex-gpt-5.6-terra,gpt-4o",
    )
    called_models = []

    def fake_post(*args, **kwargs):
        model = kwargs["json"]["model"]
        called_models.append(model)
        if model.startswith("codex-"):
            return FakeResponse(
                {"error": {"code": "upstream_error", "message": "codex container pool failed"}},
                status_code=502,
            )
        return FakeResponse({
            "id": "chatcmpl-fallback",
            "choices": [{"message": {"content": json.dumps({
                "semantic_groups": [{
                    "members": ["p001", "p002"],
                    "mode": "rigid",
                    "confidence": 0.98,
                    "reason": "同一语义组合",
                }]
            }, ensure_ascii=False)}}],
        })

    monkeypatch.setattr(semantic_grouping.requests, "post", fake_post)
    artifacts, updated, metadata = semantic_grouping.infer_and_apply_semantic_groups(
        tmp_path, primitives, groups, {"semantic_min_confidence": 0.9}
    )
    assert called_models == [
        "codex-gpt-5.6-sol",
        "codex-gpt-5.6-luna",
        "codex-gpt-5.6-terra",
        "gpt-4o",
    ]
    assert metadata["model"] == "gpt-4o"
    assert metadata["fallback_used"] is True
    assert metadata["attempts"][0]["http_status"] == 502
    assert metadata["attempts"][3]["status"] == "success"
    assert metadata["model_chain"] == called_models
    assert any(group.get("origin") == "semantic" and group.get("active") for group in updated)
    assert any(item["name"] == "semantic-attempts" for item in artifacts)


def test_low_confidence_semantic_relation_is_not_applied():
    primitives = [{"id": "p001", "bbox": [0, 0, 10, 10]}, {"id": "p002", "bbox": [20, 0, 10, 10]}]
    groups = [
        {"id": "g001", "primitive_ids": ["p001"], "bbox": [0, 0, 10, 10], "active": True},
        {"id": "g002", "primitive_ids": ["p002"], "bbox": [20, 0, 10, 10], "active": True},
    ]
    updated, applied = semantic_grouping.apply_semantic_relations(
        primitives,
        groups,
        {"semantic_groups": [{"members": ["p001", "p002"], "mode": "rigid", "confidence": 0.6}]},
        0.9,
    )
    assert applied == []
    assert sum(group.get("active", True) for group in updated) == 2
