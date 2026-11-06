# encoding: utf-8
# -----------------------------------------------------------------------------
# 橋接器 Python 端進入點。C# 外殼對每一行請求呼叫 handle_line(line, status_json)，
# 回傳一行 JSON 回應字串。
#
# 協定：
#   request : {"id": 1, "method": "ping", "params": {}}
#   response: {"id": 1, "ok": true,  "result": {...}}
#           : {"id": 1, "ok": false, "error": {"message": "...", "class": "...", "backtrace": [...]}}
# -----------------------------------------------------------------------------
import sys
import json
import time
import traceback
import Rhino
import scriptcontext as sc

import rb_common as C
import rb_core    # noqa: F401  註冊 Rhino 指令
import rb_gh      # noqa: F401  註冊 Grasshopper 指令

BRIDGE_VERSION = "1.0.0"


@C.command("ping")
def ping(p):
    return {
        "pong": True,
        "bridge_version": BRIDGE_VERSION,
        "rhino_version": str(Rhino.RhinoApp.Version),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@C.command("list_commands")
def list_commands(p):
    return sorted(C.COMMANDS.keys())


@C.command("reload_scripts")
def reload_scripts(p):
    """開發用：重新載入所有 Python 指令模組（不用重開 Rhino）。"""
    import rb_common, rb_core, rb_gh
    reload(rb_common)
    reload(rb_core)
    reload(rb_gh)
    reload(sys.modules[__name__])
    return {"reloaded": True, "commands": len(C.COMMANDS)}


def _error(req_id, exc_class, message, backtrace=None):
    err = {"message": message, "class": exc_class}
    if backtrace:
        err["backtrace"] = backtrace
    return C.dumps({"id": req_id, "ok": False, "error": err})


def handle_line(line, status_json):
    req_id = None
    try:
        req = json.loads(line)
        if not isinstance(req, dict):
            return _error(None, "ProtocolError", "請求必須是 JSON 物件。")
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        if not isinstance(params, dict):
            return _error(req_id, "ProtocolError", "params 必須是 JSON 物件。")
        try:
            C.STATE["status"] = json.loads(status_json)
        except Exception:
            pass
        # rhinoscriptsyntax 靠 scriptcontext.doc；Rhino 剛啟動時它可能是 None，每次請求都校正
        sc.doc = Rhino.RhinoDoc.ActiveDoc
        C.STATE["undo_depth"] = 0

        fn = C.COMMANDS.get(method)
        if fn is None:
            return _error(req_id, "UnknownMethod",
                          "未知的指令 %r。可用指令：%s" % (method, ", ".join(sorted(C.COMMANDS.keys()))))
        t0 = time.time()
        result = fn(params)
        return C.dumps({"id": req_id, "ok": True, "result": result,
                        "elapsed_ms": int((time.time() - t0) * 1000)})
    except C.BridgeError as e:
        return _error(req_id, "BridgeError", unicode(e))
    except Exception as e:
        tb = []
        try:
            tb = [l.rstrip() for l in traceback.format_exc().splitlines()][-12:]
        except Exception:
            pass
        try:
            msg = unicode(e)
        except Exception:
            msg = repr(e)
        return _error(req_id, e.__class__.__name__, msg, tb)
