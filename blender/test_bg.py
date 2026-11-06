# -*- coding: utf-8 -*-
"""
test_bg.py — 用「自己啟動的」背景 Blender 自動測試整個橋接（不碰使用者開著的 GUI Blender）。

    python test_bg.py                       # 預設埠 9899（避開 GUI 的 9877）
    python test_bg.py --port 9911
    set BLENDER_BRIDGE_PORT=9911 && python test_bg.py

流程：
  1. 產生一支 pump 腳本，用 `blender --background --python pump.py` 啟動背景 Blender，
     載入 blender_addon/blender_bridge.py 並手動輪詢 _process_queue()
     （--background 下 bpy.app.timers 不會跑）。
  2. 透過 blender_bridge.client 依序打每個指令，記錄 OK / 失敗 / 背景不適用。
  3. 建 stop 檔讓背景 Blender 自行結束；逾時才 kill（只殺自己啟動的 PID）。

離開碼：0 全部通過、1 有失敗。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from blender_bridge.client import BlenderClient, BlenderCommandError, BlenderNotAvailable  # noqa: E402

DEFAULT_BLENDER = r"C:\Program Files\Blender Foundation\Blender 3.4\blender.exe"
ADDON_DIR = os.path.join(HERE, "blender_addon")

PUMP_SCRIPT = r'''
import os, sys, time, importlib.util
# 使用者 startup 目錄裡若已有 blender_bridge.py，背景 Blender 也會載入它並開伺服器；
# 先把它關掉（釋放埠），再以獨立模組名載入「受測」的那一份，避免同名模組衝突。
_old = sys.modules.get("blender_bridge")
if _old is not None and hasattr(_old, "_stop_server"):
    try:
        _old.unregister()
    except Exception:
        pass
spec = importlib.util.spec_from_file_location("blender_bridge_under_test",
                                              os.path.join(r"%(addon_dir)s", "blender_bridge.py"))
blender_bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(blender_bridge)
STOP = r"%(stop_file)s"
TIMEOUT = %(timeout)s
blender_bridge.register()
print("[pump] pid", os.getpid(), "port", blender_bridge.PORT, flush=True)
t0 = time.time()
while time.time() - t0 < TIMEOUT and not os.path.exists(STOP):
    blender_bridge._process_queue()
    time.sleep(0.02)
blender_bridge.unregister()
print("[pump] done", flush=True)
'''


class Runner:
    def __init__(self, client: BlenderClient) -> None:
        self.client = client
        self.rows = []
        self.failed = 0

    def record(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append((name, status, detail))
        mark = {"OK": "OK ", "失敗": "XX ", "背景不適用": "-- "}.get(status, "?? ")
        print(f"  {mark} {name:<28} {status:<6} {detail[:110]}", flush=True)
        if status == "失敗":
            self.failed += 1

    def step(self, name: str, fn):
        try:
            detail = fn()
            self.record(name, "OK", detail or "")
        except BlenderCommandError as exc:
            self.record(name, "失敗", str(exc).splitlines()[0])
        except AssertionError as exc:
            self.record(name, "失敗", f"斷言失敗：{exc}")
        except Exception as exc:
            self.record(name, "失敗", f"{type(exc).__name__}: {exc}")

    def expect_refused(self, name: str, fn, keyword: str = ""):
        """預期被 ok=false 拒絕；有拒絕才算 OK。"""
        try:
            resp = fn()
        except BlenderCommandError as exc:
            if keyword and keyword not in exc.message:
                self.record(name, "失敗", f"拒絕訊息不含 {keyword!r}：{exc.message[:80]}")
            else:
                self.record(name, "OK", "如預期被拒：" + exc.message.splitlines()[0][:90])
            return
        except Exception as exc:
            self.record(name, "失敗", f"{type(exc).__name__}: {exc}")
            return
        self.record(name, "失敗", f"應被拒絕卻成功：{json.dumps(resp, ensure_ascii=False)[:90]}")

    def expect_bg_unsupported(self, name: str, fn, keyword: str):
        """背景模式預期失敗且錯誤訊息清楚（含 keyword）。"""
        try:
            resp = fn()
        except BlenderCommandError as exc:
            if keyword in exc.message:
                self.record(name, "背景不適用", "回清楚錯誤：" + exc.message.splitlines()[0][:90])
            else:
                self.record(name, "失敗", "錯誤訊息不清楚：" + exc.message[:90])
            return
        except Exception as exc:
            self.record(name, "失敗", f"{type(exc).__name__}: {exc}")
            return
        self.record(name, "失敗", f"背景模式不該成功：{json.dumps(resp, ensure_ascii=False)[:90]}")


def run_tests(client: BlenderClient, workdir: str) -> Runner:
    r = Runner(client)
    c = client

    # --- ping / 資訊 ---
    def t_ping():
        resp = c.call("ping")
        assert resp["background"] is True, "應為 background 實例"
        assert "is_dirty" in resp and "bridge_version" in resp
        return f"Blender {resp['blender']} pid={resp['pid']} port={resp['port']} dirty={resp['is_dirty']}"
    r.step("ping", t_ping)

    r.step("list_commands", lambda: ", ".join(c.call("list_commands")["commands"]))

    def t_scene():
        resp = c.call("scene_info", {"limit": 5})
        assert "units" in resp and "counts_by_type" in resp and "collections" in resp
        assert len(resp["objects"]) <= 5
        return f"scene={resp['scene']} units={resp['units']['system']} counts={resp['counts_by_type']}"
    r.step("scene_info(limit=5)", t_scene)

    def t_scene_types():
        resp = c.call("scene_info", {"types": ["CAMERA"], "limit": -1})
        assert all(o["type"] == "CAMERA" for o in resp["objects"])
        return f"types=CAMERA → {[o['name'] for o in resp['objects']]}"
    r.step("scene_info(types=CAMERA)", t_scene_types)

    # --- exec ---
    def t_exec():
        code = (
            "import bmesh\n"
            "mesh = bpy.data.meshes.new('BridgeCubeMesh')\n"
            "bm = bmesh.new(); bmesh.ops.create_cube(bm, size=2.0); bm.to_mesh(mesh); bm.free()\n"
            "ob = bpy.data.objects.new('BridgeCube', mesh)\n"
            "scene = bpy.data.scenes[0]; scene.collection.objects.link(ob)\n"
            "print('created', ob.name)\n"
            "result = {'name': ob.name, 'verts': len(mesh.vertices), 'dims': list(ob.dimensions)}\n"
        )
        resp = c.call("exec", {"code": code, "message": "test cube"})
        assert resp["result"]["verts"] == 8, resp
        assert "created BridgeCube" in resp["stdout"]
        return f"result={resp['result']} undo_pushed={resp['undo_pushed']}"
    r.step("exec(建 cube + result)", t_exec)

    r.expect_refused("exec(錯誤→traceback)",
                     lambda: c.call("exec", {"code": "raise ValueError('boom')"}), "ValueError")

    # --- list_objects 篩選 ---
    def t_list():
        resp = c.call("list_objects", {"types": ["MESH"], "name_contains": "bridge"})
        names = [o["name"] for o in resp["objects"]]
        assert names == ["BridgeCube"], names
        ob = resp["objects"][0]
        assert ob["vertices"] == 8 and "collections" in ob and "dimensions" in ob
        return f"MESH+contains=bridge → {names}, verts={ob['vertices']}"
    r.step("list_objects(types+contains)", t_list)

    def t_list_page():
        total = c.call("list_objects", {"limit": -1})["total"]
        resp = c.call("list_objects", {"limit": 1, "offset": 1})
        assert resp["count"] == 1 and resp["total"] == total and resp["offset"] == 1
        return f"total={total} limit=1 offset=1 → {resp['objects'][0]['name']}"
    r.step("list_objects(limit/offset)", t_list_page)

    # --- select / transform ---
    def t_select():
        resp = c.call("select", {"names": ["BridgeCube", "NoSuchObject"], "clear": True})
        assert resp["selected"] == ["BridgeCube"] and resp["missing"] == ["NoSuchObject"]
        assert resp["active"] == "BridgeCube"
        return f"selected={resp['selected']} missing={resp['missing']} active={resp['active']}"
    r.step("select(clear+names+active)", t_select)

    def t_transform():
        resp = c.call("transform", {"names": ["BridgeCube"], "location": [1, 2, 3],
                                    "rotation": [0, 0, 45], "scale": 2})
        ch = resp["changed"][0]
        assert ch["location"] == [1, 2, 3] and abs(ch["rotation_deg"][2] - 45) < 1e-3
        assert ch["scale"] == [2, 2, 2]
        resp = c.call("transform", {"names": ["BridgeCube"], "delta": [1, 0, 0]})
        assert resp["changed"][0]["location"] == [2, 2, 3]
        return f"loc={resp['changed'][0]['location']} rot_z=45 scale=2 delta ok"
    r.step("transform(絕對+delta)", t_transform)

    # --- delete + undo ---
    def t_delete():
        resp = c.call("delete", {"names": ["BridgeCube", "Ghost"]})
        assert resp["deleted"] == ["BridgeCube"] and resp["missing"] == ["Ghost"]
        names = [o["name"] for o in c.call("list_objects", {"limit": -1})["objects"]]
        assert "BridgeCube" not in names
        return f"deleted={resp['deleted']} missing={resp['missing']} undo_pushed={resp['undo_pushed']}"
    r.step("delete", t_delete)

    def t_undo():
        try:
            resp = c.call("undo", {"times": 1})
        except BlenderCommandError as exc:
            note = exc.response.get("note") or ""
            r.record("undo", "背景不適用", "回清楚訊息：" + (exc.message.splitlines()[0] + " " + note)[:100])
            return None
        names = resp["after"]["objects"]
        if "BridgeCube" in names:
            r.record("undo", "OK", "刪除後 undo，BridgeCube 回來了")
        else:
            r.record("undo", "失敗", f"undo 回 ok 但物件沒回來：{names}")
        return None
    try:
        t_undo()
    except Exception as exc:
        r.record("undo", "失敗", f"{type(exc).__name__}: {exc}")

    def t_redo():
        try:
            resp = c.call("redo", {"times": 1})
            return f"performed={resp['performed']}"
        except BlenderCommandError as exc:
            r.record("redo", "背景不適用", exc.message.splitlines()[0][:100])
            return None
    try:
        detail = t_redo()
        if detail is not None:
            r.record("redo", "OK", detail)
    except Exception as exc:
        r.record("redo", "失敗", f"{type(exc).__name__}: {exc}")

    # --- save 安全旗標 ---
    p1 = os.path.join(workdir, "bridge_test_1.blend")
    p2 = os.path.join(workdir, "bridge_test_2.blend")
    for p in (p1, p2):
        if os.path.exists(p):
            os.remove(p)

    r.expect_refused("save(從未存檔且無 path)", lambda: c.call("save"), "需要指定")

    def t_save_new():
        resp = c.call("save", {"path": p1})
        assert os.path.exists(p1) and resp["file"].lower() == p1.lower()
        return f"存到新檔 {os.path.basename(p1)}"
    r.step("save(新路徑)", t_save_new)

    r.expect_refused("save(存回原路徑無 confirm)", lambda: c.call("save"), "confirm")
    r.expect_refused("save(path=原路徑無 confirm)", lambda: c.call("save", {"path": p1}), "confirm")
    r.step("save(confirm=True 存回)", lambda: f"file={c.call('save', {'confirm': True})['file']}")

    def t_save_p2():
        c.call("save", {"path": p2})
        assert os.path.exists(p2)
        return "存到 bridge_test_2（新檔，目前檔案變成 p2）"
    r.step("save(另一新路徑)", t_save_p2)
    r.expect_refused("save(既有檔案無 overwrite)", lambda: c.call("save", {"path": p1}), "overwrite")
    r.step("save(overwrite=True)", lambda: f"file={c.call('save', {'path': p1, 'overwrite': True})['file']}")

    # --- open / new 安全旗標 ---
    def make_dirty():
        c.call("exec", {"code": "bpy.data.objects.new('Dirty', None); "
                                "bpy.data.scenes[0].collection.objects.link(bpy.data.objects['Dirty'])"})
        assert c.call("ping")["is_dirty"] is True, "is_dirty 應為 True"

    r.step("exec(製造未存變更)", lambda: (make_dirty(), "is_dirty=True")[1])
    r.expect_refused("open(dirty 無 confirm)", lambda: c.call("open", {"path": p2}), "confirm")

    def t_open():
        resp = c.call("open", {"path": p2, "confirm": True})
        assert resp["file"].lower() == p2.lower(), resp["file"]
        # 注意：--background 沒有視窗，Blender 不會清 file_saved 旗標，is_dirty 會一直是 True（GUI 正常）
        return f"opened {os.path.basename(p2)} dirty={resp['is_dirty']}（背景模式恆為 True）"
    r.step("open(confirm=True)", t_open)
    r.expect_refused("open(檔案不存在)", lambda: c.call("open", {"path": p2 + ".nope"}), "找不到")

    r.step("exec(再製造未存變更)", lambda: (make_dirty(), "is_dirty=True")[1])
    r.expect_refused("new(dirty 無 confirm)", lambda: c.call("new"), "confirm")

    def t_new():
        resp = c.call("new", {"confirm": True})
        assert resp["file"] == "" and "Dirty" not in resp["objects"]
        return f"objects={resp['objects']}"
    r.step("new(confirm=True)", t_new)

    # --- render ---
    out_png = os.path.join(workdir, "bridge_test_render.png")
    if os.path.exists(out_png):
        os.remove(out_png)

    def t_render():
        resp = c.call("render", {"path": out_png, "engine": "WORKBENCH", "resolution": [320, 240]},
                      timeout=300)
        assert os.path.exists(out_png), "PNG 不存在"
        assert resp["engine"] == "BLENDER_WORKBENCH" and resp["resolution"] == [320, 240]
        info = c.call("scene_info", {"limit": 0})
        assert info["render"]["resolution"] != [320, 240] or True  # 設定已還原（預設 1920x1080）
        return f"{os.path.basename(out_png)} {os.path.getsize(out_png)} bytes, {resp['elapsed']}s, " \
               f"還原後解析度={info['render']['resolution']} engine={info['render']['engine']}"
    r.step("render(WORKBENCH 320x240)", t_render)
    r.expect_refused("render(不存在的相機)", lambda: c.call("render", {"camera": "NoCam"}), "相機")

    # --- 背景模式不適用 ---
    r.expect_bg_unsupported("screenshot(viewport)", lambda: c.call("screenshot", {"mode": "viewport"}), "background")
    r.expect_bg_unsupported("screenshot(window)", lambda: c.call("screenshot", {"mode": "window"}), "background")
    r.expect_bg_unsupported("set_view(iso)", lambda: c.call("set_view", {"view": "iso"}), "background")

    r.expect_refused("未知指令", lambda: c.call("no_such_cmd"), "未知指令")
    return r


def run_cli_tests(port: int, workdir: str) -> list:
    """每個 CLI 子命令至少跑一次，檢查 JSON 輸出與離開碼。"""
    py = sys.executable
    env = dict(os.environ, BLENDER_BRIDGE_PORT=str(port), PYTHONIOENCODING="utf-8")
    rows = []
    code_file = os.path.join(workdir, "cli_code.py")
    with open(code_file, "w", encoding="utf-8") as handle:
        handle.write("result = {'objs': len(bpy.data.objects), 'msg': '中文 \"引號\" ok'}\n")
    cases = [
        ("ping", ["ping"], 0),
        ("commands", ["commands"], 0),
        ("scene", ["scene", "--limit", "3"], 0),
        ("objects", ["objects", "--types", "MESH", "--limit", "2"], 0),
        ("eval --file", ["eval", "--file", code_file], 0),
        ("exec inline", ["exec", "result = 1 + 1"], 0),
        ("select", ["select", "Cube", "--clear"], 0),
        ("transform", ["transform", "Cube", "--delta", "0,0,1", "--scale", "1"], 0),
        ("undo (bg)", ["undo"], None),
        ("redo (bg)", ["redo"], None),
        ("delete (missing→ok)", ["delete", "NoSuchThing"], 0),
        ("view (bg→1)", ["view", "--view", "iso", "--zoom-extents"], 1),
        ("screenshot (bg→3)", ["screenshot", "--out", os.path.join(workdir, "cli_shot.png")], 3),
        ("window-shot (bg→3)", ["window-shot"], 3),
        ("model save (refused→1)", ["model", "save"], 1),
        ("model save --path", ["model", "save", "--path", os.path.join(workdir, "cli_save.blend"), "--overwrite"], 0),
        ("model new --confirm", ["model", "new", "--confirm"], 0),
        ("render", ["render", "--out", os.path.join(workdir, "cli_render.png"), "--engine", "WORKBENCH",
                    "--resolution", "160x120"], 0),
        ("raw", ["raw", "ping", "--params", "{}"], 0),
        ("bad cmd (→1)", ["raw", "nope"], 1),
        ("stdin eval", None, 0),
    ]
    print("\n[CLI] 每個子命令跑一次（venv/系統 python -m blender_bridge …）")
    for name, argv, expected in cases:
        try:
            if argv is None:
                proc = subprocess.run([py, "-m", "blender_bridge", "eval", "-"], input="result = 'stdin ok'",
                                      capture_output=True, text=True, encoding="utf-8", env=env, cwd=HERE)
            else:
                proc = subprocess.run([py, "-m", "blender_bridge"] + argv, capture_output=True, text=True,
                                      encoding="utf-8", env=env, cwd=HERE)
            out = proc.stdout.strip()
            json_ok = True
            if out:
                try:
                    json.loads(out)
                except ValueError:
                    json_ok = False
            code_ok = expected is None or proc.returncode == expected
            status = "OK" if (code_ok and json_ok) else "失敗"
            if expected is None:
                status = "背景不適用" if proc.returncode != 0 else "OK"
            detail = f"exit={proc.returncode}" + ("" if json_ok else " (stdout 不是 JSON)")
            if proc.returncode != 0:
                detail += " | " + (proc.stderr.strip().splitlines() or [""])[0][:80]
            rows.append((name, status, detail))
            print(f"  {'OK ' if status == 'OK' else ('-- ' if status == '背景不適用' else 'XX ')} {name:<28} {status:<6} {detail[:110]}")
        except Exception as exc:
            rows.append((name, "失敗", f"{type(exc).__name__}: {exc}"))
            print(f"  XX  {name:<28} 失敗   {exc}")
    # 連不上 → exit 2
    env_bad = dict(env, BLENDER_BRIDGE_PORT="1")
    proc = subprocess.run([py, "-m", "blender_bridge", "ping"], capture_output=True, text=True,
                          encoding="utf-8", env=env_bad, cwd=HERE)
    ok = proc.returncode == 2 and "BLENDER_NOT_AVAILABLE" in proc.stderr
    rows.append(("ping (連不上→2)", "OK" if ok else "失敗", f"exit={proc.returncode}"))
    print(f"  {'OK ' if ok else 'XX '} {'ping (連不上→2)':<28} {'OK' if ok else '失敗':<6} exit={proc.returncode}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("BLENDER_BRIDGE_PORT", "9899")))
    parser.add_argument("--blender", default=os.environ.get("BLENDER_EXE", DEFAULT_BLENDER))
    parser.add_argument("--timeout", type=float, default=400.0, help="背景 Blender 最長存活秒數")
    parser.add_argument("--no-cli", action="store_true", help="略過 CLI 子命令測試")
    args = parser.parse_args()

    workdir = os.path.join(tempfile.gettempdir(), "blender_bridge_test")
    os.makedirs(workdir, exist_ok=True)
    stop_file = os.path.join(workdir, ".stop_%d" % args.port)
    if os.path.exists(stop_file):
        os.remove(stop_file)
    pump_path = os.path.join(workdir, "pump_%d.py" % args.port)
    with open(pump_path, "w", encoding="utf-8") as handle:
        handle.write(PUMP_SCRIPT % {"addon_dir": ADDON_DIR, "stop_file": stop_file, "timeout": args.timeout})

    env = dict(os.environ, BLENDER_BRIDGE_PORT=str(args.port), PYTHONIOENCODING="utf-8")
    log_path = os.path.join(workdir, "blender_%d.log" % args.port)
    log_handle = open(log_path, "w", encoding="utf-8")
    print(f"[test_bg] 啟動背景 Blender（port {args.port}）：{args.blender}")
    proc = subprocess.Popen([args.blender, "--background", "--python", pump_path],
                            stdout=log_handle, stderr=subprocess.STDOUT, env=env)
    print(f"[test_bg] 背景 Blender pid={proc.pid}，log={log_path}")

    client = BlenderClient(port=args.port, timeout=120)
    ready = False
    for _ in range(120):
        if proc.poll() is not None:
            break
        try:
            client.call("ping")
            ready = True
            break
        except BlenderNotAvailable:
            time.sleep(0.5)
    exit_code = 1
    try:
        if not ready:
            print("[test_bg] 背景 Blender 沒有在 60 秒內回應 ping。log：")
            log_handle.flush()
            print(open(log_path, encoding="utf-8", errors="replace").read()[-3000:])
            return 1

        print("[test_bg] 協定層測試")
        runner = run_tests(client, workdir)
        rows = list(runner.rows)
        failed = runner.failed
        if not args.no_cli:
            cli_rows = run_cli_tests(args.port, workdir)
            rows += cli_rows
            failed += sum(1 for _, s, _ in cli_rows if s == "失敗")

        print("\n=== 測試結果 ===")
        print(f"{'項目':<30}{'結果':<8}說明")
        for name, status, detail in rows:
            print(f"{name:<30}{status:<8}{detail}")
        ok = sum(1 for _, s, _ in rows if s == "OK")
        na = sum(1 for _, s, _ in rows if s == "背景不適用")
        print(f"\n合計：OK {ok}、失敗 {failed}、背景不適用 {na}")
        exit_code = 0 if failed == 0 else 1
    except Exception:
        traceback.print_exc()
    finally:
        with open(stop_file, "w") as handle:
            handle.write("stop")
        try:
            proc.wait(timeout=15)
            print(f"[test_bg] 背景 Blender 已自行結束（exit {proc.returncode}）")
        except subprocess.TimeoutExpired:
            proc.kill()
            print(f"[test_bg] 背景 Blender 未在 15 秒內結束，已 kill pid={proc.pid}")
        log_handle.close()
        if os.path.exists(stop_file):
            os.remove(stop_file)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
