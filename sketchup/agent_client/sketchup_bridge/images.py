# encoding: utf-8
"""圖片後處理：縮圖、存檔、轉 PNG bytes。

MCP 回傳給模型的圖片必須控制大小，否則很容易吃光 context。
預設最長邊 1280 px。
"""

from __future__ import annotations

import base64
import io
import os
import tempfile
import time
from typing import Optional, Tuple

DEFAULT_MAX_EDGE = 1280
# 超過這個位元組數就再降一級品質/尺寸
SOFT_BYTE_LIMIT = 900_000


def default_output_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "su_agent_bridge")
    os.makedirs(path, exist_ok=True)
    return path


def timestamped_path(prefix: str, extension: str = ".png", directory: Optional[str] = None) -> str:
    directory = directory or default_output_dir()
    os.makedirs(directory, exist_ok=True)
    name = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}{extension}"
    return os.path.join(directory, name)


def load_image(data: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(data))


def shrink_to_max_edge(image, max_edge: int = DEFAULT_MAX_EDGE):
    """等比縮小到最長邊不超過 max_edge。不放大。"""
    from PIL import Image

    if max_edge is None or max_edge <= 0:
        return image
    width, height = image.size
    longest = max(width, height)
    if longest <= max_edge:
        return image
    scale = max_edge / float(longest)
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return image.resize(new_size, Image.LANCZOS)


def to_png_bytes(image) -> bytes:
    buffer = io.BytesIO()
    if image.mode not in ("RGB", "RGBA", "L"):
        image = image.convert("RGB")
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def to_jpeg_bytes(image, quality: int = 80) -> bytes:
    buffer = io.BytesIO()
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def encode_for_model(image, max_edge: int = DEFAULT_MAX_EDGE) -> Tuple[bytes, str]:
    """把圖片縮到適合塞進模型 context 的大小。

    回傳 (bytes, mime_type)。先試 PNG，太大就改用 JPEG。
    """
    shrunk = shrink_to_max_edge(image, max_edge)
    data = to_png_bytes(shrunk)
    if len(data) <= SOFT_BYTE_LIMIT:
        return data, "image/png"
    data = to_jpeg_bytes(shrunk, quality=82)
    if len(data) <= SOFT_BYTE_LIMIT:
        return data, "image/jpeg"
    smaller = shrink_to_max_edge(shrunk, max(320, int(max_edge * 0.7)))
    return to_jpeg_bytes(smaller, quality=75), "image/jpeg"


def save_image(image, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if image.mode not in ("RGB", "RGBA", "L"):
        image = image.convert("RGB")
    image.save(path)
    return os.path.abspath(path)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
