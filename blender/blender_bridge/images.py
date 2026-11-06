# encoding: utf-8
"""圖片後處理（client 端）：縮圖與轉成適合回傳給模型的 bytes。

縮圖主要在 Blender 端完成（screenshot 的 max_edge 參數，用 bpy.data.images.scale），
所以這個模組**不強制依賴 Pillow**：
  - 有 Pillow：可再縮一次、PNG 太大時改 JPEG。
  - 沒 Pillow：原樣回傳 Blender 產生的 PNG bytes。
"""

from __future__ import annotations

import io
import os
import tempfile
import time
from typing import Optional, Tuple

DEFAULT_MAX_EDGE = 1280
SOFT_BYTE_LIMIT = 900_000


def default_output_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "blender_bridge")
    os.makedirs(path, exist_ok=True)
    return path


def timestamped_path(prefix: str, extension: str = ".png", directory: Optional[str] = None) -> str:
    directory = directory or default_output_dir()
    os.makedirs(directory, exist_ok=True)
    name = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}{extension}"
    return os.path.join(directory, name)


def has_pillow() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def encode_for_model(png_bytes: bytes, max_edge: int = DEFAULT_MAX_EDGE) -> Tuple[bytes, str]:
    """把 PNG bytes 整理成 (bytes, mime)。沒有 Pillow 時原樣回傳。"""
    if not has_pillow():
        return png_bytes, "image/png"
    from PIL import Image

    image = Image.open(io.BytesIO(png_bytes))
    width, height = image.size
    longest = max(width, height)
    if max_edge and longest > max_edge:
        scale = max_edge / float(longest)
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)

    buffer = io.BytesIO()
    if image.mode not in ("RGB", "RGBA", "L"):
        image = image.convert("RGB")
    image.save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()
    if len(data) <= SOFT_BYTE_LIMIT:
        return data, "image/png"

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=82, optimize=True)
    data = buffer.getvalue()
    if len(data) <= SOFT_BYTE_LIMIT:
        return data, "image/jpeg"

    smaller_edge = max(320, int((max_edge or longest) * 0.7))
    scale = smaller_edge / float(max(image.size))
    image = image.resize((max(1, int(image.size[0] * scale)), max(1, int(image.size[1] * scale))), Image.LANCZOS)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=75, optimize=True)
    return buffer.getvalue(), "image/jpeg"
