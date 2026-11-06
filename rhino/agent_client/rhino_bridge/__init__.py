# encoding: utf-8
"""Rhino Agent Bridge — 共用 Python client 函式庫。

這個套件是「單一來源」：MCP server 與 CLI 都建立在 client.py 之上，
不重複實作任何協定邏輯。
"""

from .client import (  # noqa: F401
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    RhinoBridgeError,
    RhinoClient,
    RhinoCommandError,
    RhinoConnectionError,
    call,
)

__version__ = "1.0.0"

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_TIMEOUT",
    "RhinoBridgeError",
    "RhinoClient",
    "RhinoCommandError",
    "RhinoConnectionError",
    "call",
    "__version__",
]
