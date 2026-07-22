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


def test_direct_image_provider_uploads_reference_and_saves_trace(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(image_bytes())
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
    request_text = (tmp_path / "components" / "semantic" / "request-summary.json").read_text(encoding="utf-8")
    assert "secret-client-key" not in request_text
    assert "<data-url:" in request_text
    assert any(item["name"] == "semantic-relations" for item in artifacts)


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
