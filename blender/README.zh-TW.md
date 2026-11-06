# Blender Agent Bridge（Blender 代理操控插件）

[English](README.md) ・ **繁體中文**

讓 Claude Code（或任何 agent／外部程式）操控本機**正在執行的 Blender 3.4**。
使用者層級、跨專案，架構與本 repo 的 SketchUp／Rhino bridge 相同：
**MCP 為主、CLI 備援、外加一份 Claude skill**。

## 架構

```
                     ┌──────────────────────────────────────────────┐
  Claude / agent ──MCP(stdio)──▶ python -m blender_bridge.mcp_server │
                     │                     │                         │
  PowerShell ──CLI──▶ python -m blender_bridge <cmd>                 │  venv
                     │                     │                         │
                     │            blender_bridge/client.py           │
                     └─────────────────────┼─────────────────────────┘
                                           │ TCP，一連線一行 JSON
                                           ▼ 127.0.0.1:9877（BLENDER_BRIDGE_PORT）
                     ┌──────────────────────────────────────────────┐
                     │ Blender 3.4（使用者開著的 GUI）                 │
                     │  scripts/startup/blender_bridge.py           │
                     │  socket thread → queue → bpy.app.timers 主執行緒 │
                     └──────────────────────────────────────────────┘
```

```
blender\
  （venv 建在 %USERPROFILE%\BlenderAgentBridge\venv：mcp[cli]、pillow、本套件 -e，
   所以 clone 下來的這個資料夾要留著，別搬走）
  blender_bridge\             Python 套件（系統/venv 端；MCP 與 CLI 共用同一個 client）
    __init__.py
    client.py                 連線、send()/call()、BlenderNotAvailable / BlenderCommandError
    cli.py / __main__.py      CLI：python -m blender_bridge <cmd> …（輸出 JSON）
    mcp_server.py             MCP：python -m blender_bridge.mcp_server
    images.py                 縮圖／轉 JPEG（Pillow 可選）
  blender_addon\
    blender_bridge.py         Blender 端 startup 腳本（唯一正本）
  skill\blender-control\      Claude skill（install.py 複製到 ~/.claude/skills/）
  install.py                  一鍵安裝（可重複執行）
  test_bg.py                  背景 Blender 自動測試
  pyproject.toml
```

## 安裝

```powershell
cd <本資料夾（blender/）的路徑>
python install.py            # 系統 Python 3.10+（開發時用 3.12）
```

`install.py` 做四件事（每一步都冪等，可重複執行）：

1. 建 `%USERPROFILE%\BlenderAgentBridge\venv`，`pip install "mcp[cli]" pillow`，`pip install -e .`（失敗退回寫 `.pth`）。
   MCP 啟動不依賴網路、不用每次 `uv run` 解析依賴。
2. 把 `blender_addon\blender_bridge.py` 複製到
   `%APPDATA%\Blender Foundation\Blender\3.4\scripts\startup\blender_bridge.py`。
   有變動時提醒**重啟 Blender**（不會自動重啟）。
3. `claude mcp remove blender -s user` → `claude mcp add --scope user blender -- <venv>\Scripts\python.exe -m blender_bridge.mcp_server` → `claude mcp get blender`。
4. 複製 `skill\blender-control` 到 `~\.claude\skills\blender-control`。

旗標：`--skip-venv`、`--skip-deps`、`--skip-addon`、`--skip-mcp`、`--skip-skill`、`--blender-version 3.4`。

安裝後使用者要做的事：
- **重新啟動 Blender**（System Console 會印 `[blender_bridge] v2.0.0 listening on 127.0.0.1:9877`）。
- **新開一個 Claude session** 才看得到 `blender_*` MCP 工具。

不想重啟 Blender 時，可在 Blender 的 Text Editor 開啟 `blender_bridge.py` 按 Run Script，
效果相同（只到這次 Blender 關閉為止）。

## MCP 工具（`python -m blender_bridge.mcp_server`）

| 工具 | 說明 |
|---|---|
| `blender_ping` | 版本、是否 background、目前檔案、`is_dirty`、pid、埠。每次開工先叫 |
| `blender_get_scene_info(limit, offset, types)` | 場景名、frame、單位、引擎、依 type 統計、collection 樹、選取、active、分頁物件清單 |
| `blender_list_objects(name_contains, types, limit, offset)` | 名稱、type、location、rotation(度)、scale、dimensions、parent、collections、visible、頂點/面數、modifiers |
| `blender_eval_python(code, message, undo_push, timeout_seconds)` | 在主執行緒執行 Python；globals 持久含 bpy/bmesh/mathutils/math；`result` 回傳；回 stdout/stderr/traceback |
| `blender_select_objects(names, clear, active)` | 依名稱選取並設 active |
| `blender_delete_objects(names, remove_data)` | 只刪指定名稱，找不到的回報在 `missing` |
| `blender_transform_objects(names, location, rotation_deg, scale, delta, delta_rotation_deg, delta_scale)` | 絕對設定或相對變更 |
| `blender_set_view(view, projection, shading, zoom_extents, view_selected)` | front/back/left/right/top/bottom/iso、ortho/persp、wireframe/solid/material/rendered、框到全部/選取 |
| `blender_viewport_screenshot(max_edge)` | 3D 視埠截圖 → Image（`screen.screenshot_area`，失敗退 `render.opengl`） |
| `blender_window_screenshot(max_edge)` | 整個 Blender 視窗截圖 → Image（`screen.screenshot`） |
| `blender_undo_redo(direction, times)` | `ed.undo` / `ed.redo`；回 before/after 物件清單 |
| `blender_model_file(action, path, confirm, overwrite, copy, empty)` | open / save / new，含安全旗標 |
| `blender_render(path, engine, resolution, camera, samples, percentage, transparent, max_edge)` | 算一張 PNG → Image + 路徑；WORKBENCH/EEVEE/CYCLES；設定算完還原 |

連不上時每個工具回傳以 `BLENDER_NOT_AVAILABLE:` 開頭的訊息（Blender 必須已由使用者開啟，
且 startup 腳本已安裝，重啟 Blender 後生效）。指令失敗回 `BLENDER_COMMAND_FAILED:`，
安全旗標不足回 `BLENDER_REFUSED:`。

環境變數：`BLENDER_BRIDGE_HOST`、`BLENDER_BRIDGE_PORT`（預設 9877）、`BLENDER_BRIDGE_TIMEOUT`
（預設 120 秒）、`BLENDER_BRIDGE_MAX_EDGE`（回傳圖片最長邊，預設 1280）。

### undo 語意

`eval_python`、`transform_objects`、`delete_objects` 在**改動之後** `bpy.ops.ed.undo_push(message=…)`
（與 Blender operator 相同），所以使用者按一次 Ctrl+Z 剛好回到改動前。
（實測「改動前 push」會讓一次 undo 跳兩步，所以不採用。）

## CLI（`venv\Scripts\python.exe -m blender_bridge …`）

| 子命令 | 說明 |
|---|---|
| `ping` / `commands` | 連線測試 / 列出 Blender 端指令 |
| `scene [--limit N] [--offset N] [--types …]` | 場景總覽 |
| `objects [--contains S] [--types …] [--limit N] [--offset N]` | 列物件 |
| `eval [code \| -] [--file F] [--message M] [--no-undo]` | 執行 Python（`exec` 同義；含引號請用 `--file` 或 stdin） |
| `select [names…] [--clear] [--active NAME]` | 選取 |
| `delete names… [--keep-data]` | 刪除 |
| `transform names… [--location x,y,z] [--rotation x,y,z] [--scale s\|x,y,z] [--delta …] [--delta-rotation …] [--delta-scale …]` | 變換 |
| `view [--view iso] [--projection ortho] [--shading solid] [--zoom-extents] [--view-selected]` | 視埠 |
| `screenshot [--out F] [--max-edge N]` / `window-shot …` | 截圖 |
| `undo [--times N]` / `redo [--times N]` | 復原/重做 |
| `model open\|save\|new [--path F] [--confirm] [--overwrite] [--copy] [--empty] [--load-ui]` | 檔案 |
| `render [--out F] [--engine WORKBENCH\|EEVEE\|CYCLES] [--resolution WxH] [--camera NAME] [--samples N] [--percentage N] [--transparent]` | 算圖 |
| `raw <cmd> [--params JSON]` | 直接送任意指令 |

全域選項：`--host`、`--port`（或 `BLENDER_BRIDGE_PORT`）、`--timeout`。
stdout 一律 UTF-8 JSON；離開碼 `0` 成功、`1` 指令失敗（含安全旗標拒絕）、`2` 連不上
（stderr 開頭 `BLENDER_NOT_AVAILABLE:`）、`3` 截圖/算圖失敗。

## 協定

- 每條連線送一行 JSON（`\n` 結尾），回一行 JSON 後關閉連線。
- 請求：`{"id": 1, "cmd": "transform", "params": {...}}`；也接受舊格式
  `{"cmd": "exec", "code": "..."}`（參數放頂層）與 `{"method": ..., "params": ...}`。
- 回應：`{"ok": true, ...}` 或 `{"ok": false, "error": "...", "refused": true?}`，帶 `id` 原樣回傳。
- Blender 端：socket thread 收請求 → `queue` → `bpy.app.timers`（0.05 秒）在主執行緒執行 →
  `threading.Event` 交回結果。主執行緒 600 秒沒處理（Blender 忙／對話框）回 timeout。
- 截圖會先 `tag_redraw` 並延遲 0.15 秒再擷取，避免截到舊畫面；`set_view` 會暫時把
  `smooth_view` 設為 0 讓視角瞬間切換。
- Blender 端指令：`ping list_commands exec scene_info list_objects select delete transform set_view screenshot undo redo open save new render`。

### 安全旗標

| 情況 | 需要 |
|---|---|
| `save` 存回原路徑（不給 path，或 path = 目前檔案） | `confirm=true` |
| `save` 存到已存在的其他檔案 | `overwrite=true` |
| `open` / `new` 而 `bpy.data.is_dirty` 為 true | `confirm=true` |

拒絕時回 `{"ok": false, "refused": true, "error": "..."}`。

## 測試（背景 Blender，不碰使用者開著的 GUI）

```powershell
venv\Scripts\python.exe test_bg.py --port 9899
```

自己啟動 `blender --background --python <pump>`（手動輪詢 queue），依序測
ping、scene_info、list_objects 篩選、exec（建 cube + result / traceback）、select、transform、
delete、undo（刪除後物件回來）、redo、save 安全旗標、open/new 安全旗標、render（Workbench PNG）、
screenshot/set_view（背景模式回清楚錯誤），再把每個 CLI 子命令跑一次檢查 JSON 與離開碼。
測完只結束自己啟動的 Blender。

## 故障排除

| 症狀 | 處理 |
|---|---|
| `BLENDER_NOT_AVAILABLE` | Blender 沒開、或 startup 腳本沒裝／裝了沒重啟。看 Blender System Console 有沒有 `[blender_bridge] … listening` |
| Console 印 `could not bind 127.0.0.1:9877` | 埠被另一個 Blender（含背景測試）佔用。關掉另一個，或用 `BLENDER_BRIDGE_PORT` 改埠（Blender 端與 client 端都要設） |
| 截圖／`set_view` 回「--background 模式」 | 只有 GUI Blender 能截圖；背景實例做不到 |
| `is_dirty` 一直是 true | 只發生在 `--background`（沒有視窗時 Blender 不清 `file_saved`）；GUI 正常 |
| 指令逾時 | Blender 跳了對話框或在 modal operator；用 `blender_window_screenshot` 看畫面 |
| 截圖是舊畫面 | 先 `blender_set_view(zoom_extents=True)` 再截；或加大 `redraw_delay` 參數（raw 指令） |
| `claude mcp get blender` 不是 Connected | 用 venv python 手動跑 `python -m blender_bridge.mcp_server` 看錯誤；`pip install -e .` 是否成功 |
| MCP 工具看不到 | 新開 Claude session |
| 中文路徑亂碼 | CLI 已強制 stdout UTF-8；PowerShell 端可 `[Console]::OutputEncoding = [Text.Encoding]::UTF8` |

## 已知限制

- 不模擬滑鼠鍵盤；截圖只能看。
- 截圖抓第一個（最大的）3D Viewport；Edit Mode／其他 workspace 就是那個畫面。
- `render` 需要場景有相機；EEVEE/CYCLES 期間 Blender UI 會凍結。
- Blender 端 `max_edge` 縮圖用 `bpy.data.images.scale()`（品質普通）；venv 有 Pillow 時 client 端會再做一次高品質縮圖與 JPEG 降級。
- 背景模式 undo 堆疊可用但 `is_dirty` 不更新；GUI 行為以實機為準。
