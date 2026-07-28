import io
import json

from PIL import Image, ImageDraw

import comfyui_client


class FakeResponse:
    def __init__(self, payload=None, content=b"", status_code=200):
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.text = json.dumps(payload or {})

    def json(self):
        return self._payload


def transparent_png() -> bytes:
    image = Image.new("RGBA", (64, 48), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((10, 8, 40, 35), fill=(230, 80, 20, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_comfyui_url_workflow_upload_poll_and_download(tmp_path, monkeypatch):
    source = tmp_path / "white-master.png"
    Image.new("RGB", (64, 48), "white").save(source)
    workflow = {
        "1": {
            "inputs": {"url": "#{image}"},
            "class_type": "LoadImagesFromURL",
        },
        "2": {
            "inputs": {"images": ["1", 0]},
            "class_type": "SaveImage",
        },
    }
    workflow_path = tmp_path / "workflow.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    monkeypatch.setenv("LP_COMFYUI_WORKFLOW", str(workflow_path))
    monkeypatch.setenv("LP_COMFYUI_BASE_URL", "http://comfy.example:6070")
    monkeypatch.setenv("LP_COMFYUI_POLL_SECONDS", "0.001")
    monkeypatch.setattr(
        comfyui_client,
        "upload_image_to_cloud",
        lambda path: ("https://cdn.example/white-master.png", {"data": "https://cdn.example/white-master.png"}),
    )
    calls = []

    def fake_post(url, **kwargs):
        calls.append(("post", url, kwargs))
        assert kwargs["json"]["prompt"]["1"]["inputs"]["url"] == "https://cdn.example/white-master.png"
        return FakeResponse({"prompt_id": "prompt-123", "node_errors": {}})

    def fake_get(url, **kwargs):
        calls.append(("get", url, kwargs))
        if "/history/" in url:
            return FakeResponse({
                "prompt-123": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {
                        "2": {
                            "images": [{
                                "filename": "ComfyUI_00001_.png",
                                "subfolder": "",
                                "type": "output",
                            }]
                        }
                    },
                }
            })
        assert url.endswith("/view")
        assert kwargs["params"]["filename"] == "ComfyUI_00001_.png"
        return FakeResponse(content=transparent_png())

    monkeypatch.setattr(comfyui_client.requests, "post", fake_post)
    monkeypatch.setattr(comfyui_client.requests, "get", fake_get)
    result, artifacts, metadata = comfyui_client.remove_background(source, tmp_path)

    assert result.is_file()
    assert Image.open(result).mode == "RGBA"
    assert metadata["prompt_id"] == "prompt-123"
    assert metadata["alpha_range"] == [0, 255]
    assert any(item["name"] == "qiniu-upload" for item in artifacts)
    assert any(item["name"] == "comfyui-transparent" for item in artifacts)
    assert [item[0] for item in calls] == ["post", "get", "get"]
