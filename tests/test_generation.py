import base64
import io

from PIL import Image

from generation import extract_image_bytes


def sample_png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format="PNG")
    return output.getvalue()


def test_extract_data_url():
    raw = sample_png_bytes()
    payload = {"choices": [{"message": {"content": "data:image/png;base64," + base64.b64encode(raw).decode()}}]}
    assert extract_image_bytes(payload) == raw


def test_extract_plain_base64():
    raw = sample_png_bytes()
    payload = {"data": [{"b64_json": base64.b64encode(raw).decode()}]}
    assert extract_image_bytes(payload) == raw
