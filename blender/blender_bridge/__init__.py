# encoding: utf-8
"""Blender Agent Bridge — 共用 Python client 函式庫。

這個套件是「單一來源」：MCP server 與 CLI 都建立在 client.py 之上，
不重複實作任何協定邏輯。Blender 端的 startup 腳本在 ../blender_addon/blender_bridge.py。
"""

from .client import (  # noqa: F401
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    BlenderBridgeError,
    BlenderClient,
    BlenderCommandError,
    BlenderNotAvailable,
    call,
)

__version__ = "2.0.0"

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_TIMEOUT",
    "BlenderBridgeError",
    "BlenderClient",
    "BlenderCommandError",
    "BlenderNotAvailable",
    "call",
    "__version__",
]
