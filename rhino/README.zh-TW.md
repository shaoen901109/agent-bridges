# Rhino Agent Bridge（Rhino 代理操控插件）

[English](README.md) ・ **繁體中文**

讓本機的 AI agent（Claude Code 或任何支援 MCP 的客戶端）能接管 **Rhino 7** 與 **Grasshopper**：
看到畫面、用 RhinoCommon（IronPython）建模與修改幾何、下 Rhino 指令、在 Grasshopper 內放元件／連線／求解／烘焙。

- **看**：視埠截圖（`ViewCapture`）、Grasshopper 畫布截圖、OS 層全視窗截圖（含命令列、面板、對話框）
- **做**：完整 RhinoCommon + rhinoscriptsyntax（`rhino_eval_python`）、一組建模捷徑、Rhino 指令字串、Grasshopper 全套操作
- **不做**：不模擬滑鼠鍵盤（唯一例外：`rhino_cancel_command` 對「卡在提示的指令」送一次 Esc / Enter）。全視窗截圖「僅供觀看」。

> **安全前提**：伺服器只綁 `127.0.0.1`（本機），不接受外部連線。
> 與 SketchUp 版不同：Rhino 外掛安裝後**預設隨 Rhino 啟動自動開伺服器**（可用 `RhinoAgentBridge AutoStart` 關掉）。
> 沒有「關閉 / 結束 Rhino」的工具；覆蓋檔案、新檔、開檔一律要使用者同意（`confirm` / `overwrite`）。

---

## 1. 架構與為什麼這樣做

```
┌───────────────────────────── Rhino 7.1（.NET Framework 4.8 / IronPython 2.7）─────────────────────────────┐
│  RhinoAgentBridge.rhp  （C#，一個檔，用系統內建 csc 編譯）                                               │
│      PlugIn（AtStartup）＋ 4 個 Rhino 指令                                                                │
│      BridgeServer      TcpListener(127.0.0.1)；accept 執行緒 ＋ 每個連線一條讀取執行緒                    │
│      PythonHost        每行請求 → RhinoApp.InvokeOnUiThread → 呼叫 Python handle_line()                   │
│  py\  （純文字 Python，可熱更新）                                                                         │
│      rhino_bridge_main.py  協定、ping、reload_scripts        rb_common.py  共用工具（單位、GUID、undo…） │
│      rb_core.py            Rhino 指令（建模、檢視、截圖、檔案…）  rb_gh.py   Grasshopper 指令            │
└───────────────────────────────┬──────────────────────────────────────────────────────────────────────────┘
                                │  127.0.0.1:9878（預設；本 repo 內 SketchUp 用 9876、Blender 用 9877）
                                │  換行分隔 JSON：{"id":1,"method":"ping","params":{},"timeout":29}
                                │                 {"id":1,"ok":true,"result":{...}}
┌───────────────────────────────┴──────────────────────────────────────────────────────────────────────────┐
│  Python 套件 rhino_bridge（單一來源，兩層薄殼）                                                           │
│      client.py          ← 唯一實作協定的地方（連線、逾時、錯誤訊息）                                      │
│      launcher.py        ← rhino_launch：啟動 Rhino 並等橋接器就緒                                         │
│      window_capture.py  ← Win32 PrintWindow / ImageGrab 全視窗截圖                                        │
│      images.py          ← 縮圖 / 編碼，控制回傳給模型的圖片大小                                           │
│           ├── mcp_server.py   MCP server（stdio，46 個 tool：rhino_* 與 gh_*）← 主要介面                  │
│           └── cli.py          CLI（除錯 / 備援 / 批次）                                                   │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 為什麼是「C# 外殼 + IronPython 腳本」

| 考量 | 結論 |
|---|---|
| 要能在 Rhino **啟動時自動載入**、不靠使用者手動執行腳本 | 需要真正的 Rhino 外掛（`.rhp`，`LoadMode = AtStartup`）。純 Python 腳本做不到（Rhino 7 的 `StartupScript` 設定要改 settings XML，且 Rhino 開著時改會被覆蓋）。 |
| 不要求使用者裝 Visual Studio / .NET SDK | `.rhp` 其實就是 .NET dll；`install.ps1` 用 VS Build Tools 的 Roslyn `csc.exe`，沒有就退回 Windows 內建的 .NET Framework `csc.exe`（C# 5，原始碼刻意只用 C# 5 語法）。 |
| 要常改、要熱更新，也要讓 agent 能用 RhinoCommon 全部功能 | 指令邏輯放在 IronPython 腳本（`py\*.py`），C# 只負責 socket、UI 執行緒派送、逾時、錯誤包裝。`reload_scripts` 指令可不重開 Rhino 就換新腳本。 |
| Rhino 7 的 Python 是 IronPython 2.7，Python 3 在 7.x 不可用 | 所以 `rhino_eval_python` 是 **Python 2.7 語法**。MCP / CLI 端的 Python（3.10+）是另一個行程，兩邊只透過 JSON 溝通。 |
| 所有 RhinoCommon / Grasshopper 呼叫都不是執行緒安全的 | 每個請求都用 `RhinoApp.InvokeOnUiThread` 派到 UI 執行緒執行；逾時回傳錯誤而不是卡死（見 §8）。 |
| 通訊協定、錯誤碼、CLI 結束碼要與 SketchUp / Blender 版一致 | 沿用同一套：換行分隔 JSON、`RHINO_NOT_AVAILABLE` 前綴、結束碼 0/1/2/3。 |

### 為什麼用登錄機碼註冊外掛（而不是 Rhino 的「套件」或 settings XML）

- Rhino 7 的套件資料夾（`%APPDATA%\McNeel\Rhinoceros\packages\7.0`）實測會被 Yak 清掉沒有 manifest 的外掛，**不可靠**。
- 外掛的「已註冊」資訊本來就存在 `HKCU\SOFTWARE\McNeel\Rhinoceros\7.0\Plug-ins\<GUID>`。
  `install.ps1` 直接寫入該機碼（`LoadMode = 1` 開機載入、`PlugIn\FileName` 指向 `.rhp`、`CommandList` 列出指令）。
  這樣 **Rhino 關著也能安裝**，下次啟動自動載入。
- **完全不修改 Rhino 的 settings XML**。唯一會被修改的系統設定就是上面這個登錄機碼：若它已存在，安裝前先 `reg export` 備份成
  `%LOCALAPPDATA%\RhinoAgentBridge\backup\plugin_key_*.reg`；解除安裝時再匯出一次並只刪這一個機碼。

---

## 2. 安裝

需求：Windows、Rhino 7（預設 `C:\Program Files\Rhino 7`）、Python 3.10+、（編譯外掛用）.NET Framework 4.x（Windows 內建）。

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

`install.ps1` 依序做（每步可重複執行）：

1. 編譯 `rhino_plugin\src\BridgePlugIn.cs` → `%LOCALAPPDATA%\RhinoAgentBridge\plugin\RhinoAgentBridge.rhp`，並複製 `py\*.py`
2. 寫入外掛登錄機碼（見上）
3. 建立 venv `%USERPROFILE%\RhinoAgentBridge\venv`（純 ASCII 路徑）並 `pip install` agent_client（預設官方 PyPI；網路慢可加 `-PipIndex <鏡像網址>`）
4. 把 `skill\rhino-control\`（SKILL.md + references\）複製到 Claude Code 的 skills 資料夾 `~\.claude\skills\`；`-SkipSkills` 可略過
5. 註冊 MCP（名稱 `rhino`）到 Claude Code（`claude mcp add`，只新增 / 更新 `rhino` 這一筆）。其他 MCP 客戶端請參考 `mcp-examples\` 手動設定。

常用參數：`-SkipBuild`、`-SkipVenv`、`-SkipClaudeCodeMcp`、`-PipIndex`、`-HomeDir`（測試用）。

> Rhino 正在執行時，`.rhp` 被鎖住無法覆蓋（Python 腳本可以）。要更新外掛本體請先關閉 Rhino 再重跑 `install.ps1`。
> 只改 `py\*.py` 時，複製後呼叫 `python -m rhino_bridge raw reload_scripts` 即可熱更新。

解除安裝：`powershell -ExecutionPolicy Bypass -File .\uninstall.ps1`（加 `-RemoveVenv` 連 venv 一起刪）。
只移除本插件的東西：登錄機碼、`plugin\` 資料夾、Claude Code 的 `rhino` MCP 項目與 skill。不碰 SketchUp / Blender。

---

## 3. 啟動與使用

- **自動**：Rhino 啟動 → 外掛載入 → 第一個 Idle 事件時開伺服器（預設 `127.0.0.1:9878`）。
- **手動**（Rhino 指令列）：
  - `RhinoBridgeStart` / `RhinoBridgeStop` / `RhinoBridgeStatus`
  - `RhinoAgentBridge`：選項 `Start`、`Stop`、`Status`、`ReloadScripts`、`AutoStart`（是否隨 Rhino 啟動）、`Port`（連接埠；環境變數 `RHINO_BRIDGE_PORT` 優先）
- **Rhino 沒開**：呼叫 `rhino_launch`（或 `python -m rhino_bridge launch`）。它只會啟動 Rhino，等橋接器就緒，**不會關閉或強制結束 Rhino**。
  若等不到就緒會回報 Rhino 視窗標題——通常是授權 / 登入 / 更新對話框擋住，請使用者自行處理。
- 記錄檔：`%LOCALAPPDATA%\RhinoAgentBridge\bridge.log`

---

## 4. MCP 客戶端設定

`install.ps1` 只會自動註冊 Claude Code；其他 MCP 客戶端請參考 `mcp-examples\`（`mcp-config.json` 是通用的 `mcpServers` 範例）：

| 客戶端 | 位置 | 做法 |
|---|---|---|
| Claude Code | 使用者層級 MCP | `claude mcp add rhino --scope user -- "<venv>\Scripts\python.exe" -m rhino_bridge.mcp_server`（名稱要放在 `--scope` 前面；在 PowerShell 裡 `--` 要加引號） |
| 其他（Claude Desktop 等） | 該客戶端的 MCP 設定檔 | 合併 `mcpServers.rhino`（`command` 指向 venv 的 python，`args` 為 `-m rhino_bridge.mcp_server`），其他項目不動 |

環境變數（放進 `env`，選填）：

| 變數 | 預設 | 說明 |
|---|---|---|
| `RHINO_BRIDGE_HOST` | `127.0.0.1` | 只應維持本機 |
| `RHINO_BRIDGE_PORT` | `9878` | 要和 Rhino 內伺服器連接埠一致 |
| `RHINO_BRIDGE_TIMEOUT` | `30` | 單一指令逾時秒數（慢指令工具自帶更長逾時） |
| `RHINO_BRIDGE_MAX_EDGE` | `1280` | 回傳給模型的截圖最長邊像素 |
| `RHINO_EXE` | （預設安裝路徑） | `rhino_launch` 用的 Rhino.exe |

---

## 5. MCP 工具清單（46 個）

### Rhino（27）

| 工具 | 說明 |
|---|---|
| `rhino_get_info` | 連線檢查（兼 ping）＋ Rhino 版本、檔案、模型單位 / 公差、是否有指令執行中 |
| `rhino_get_scene_info` | 模型全貌：物件統計、圖層、邊界、視埠 |
| `rhino_list_objects` | 列物件（GUID、型別、圖層、顏色、bbox），分頁、可依型別 / 圖層 / 名稱過濾 |
| `rhino_select_objects` | 以 GUID 設定 / 清除選取 |
| `rhino_delete_objects` | 以 GUID 刪除（一個 undo record） |
| `rhino_viewport_screenshot` | 視埠截圖（ViewCapture）→ MCP 圖片 |
| `rhino_window_screenshot` | 全視窗截圖（PrintWindow），`target` = `rhino` 或 `grasshopper` |
| `rhino_set_view` | 標準視圖 / 顯示模式 / 相機 / zoom extents |
| `rhino_create_box` / `_sphere` / `_cylinder` / `_circle` | 基本實體與圓 |
| `rhino_create_polyline` / `_curve` | 多段線、NURBS 曲線（控制點或內插） |
| `rhino_extrude` | 擠出曲線 / 曲面（可封口） |
| `rhino_boolean` | 聯集 / 差集 / 交集 |
| `rhino_loft` | 放樣 |
| `rhino_transform_objects` | 移動 / 旋轉 / 縮放 / 鏡射（可複製） |
| `rhino_set_layer` | 移到圖層（`Parent::Child`）或編輯圖層 |
| `rhino_set_material` | 顏色 / 簡單材質 / 透明度 |
| `rhino_eval_python` | 在 UI 執行緒跑 IronPython 2.7，回傳 `result` 與 stdout，整段是一個 undo record |
| `rhino_run_command` | 執行 Rhino 指令字串（建議用 `-_` 開頭的無對話框形式），回傳新增物件與命令列輸出 |
| `rhino_cancel_command` | 結束卡在提示的指令（Esc / Enter） |
| `rhino_undo_redo` | 復原 / 重做 |
| `rhino_model_file` | `new` / `open` / `save` / `save_as` / `export`（需 `confirm` / `overwrite`） |
| `rhino_launch` | 沒開就啟動 Rhino（可帶 .3dm）並等橋接器就緒 |

### Grasshopper（19）

| 工具 | 說明 |
|---|---|
| `gh_status` | GH 版本、編輯器是否載入 / 顯示、作用中文件摘要 |
| `gh_open_editor` | 載入並顯示（或隱藏）編輯器 |
| `gh_new_document` / `gh_open_document` / `gh_save_document` | 新增 / 開啟 `.gh` `.ghx` / 儲存（需 `confirm`） |
| `gh_search_components` | 依名稱搜尋元件，回傳 guid、分類與輸入輸出埠 |
| `gh_add_component` | 放置元件（名稱或 guid；有歧義會列出候選） |
| `gh_add_slider` / `gh_add_panel` / `gh_add_toggle` | Number Slider / Panel / Boolean Toggle |
| `gh_set_input` | 設 slider / panel / toggle 的值，或元件輸入埠的 persistent data（數字、字串、點、向量、區間、顏色、Rhino 物件） |
| `gh_connect` / `gh_disconnect` | 連線 / 斷線 |
| `gh_list_document` | 列出物件、埠、連線與執行期錯誤 / 警告 |
| `gh_solve` | 同步求解，回傳每個物件的訊息 |
| `gh_get_output` | 讀輸出（或輸入）埠資料的摘要 |
| `gh_bake` | 烘焙輸出到 Rhino（可指定圖層 / 名稱 / 顏色，一個 undo record） |
| `gh_delete_objects` | 刪除 GH 物件 |
| `gh_canvas_screenshot` | 畫布截圖 |
| `gh_eval_python` | 帶 Grasshopper SDK 的 IronPython（`G`、`ghdoc`、`canvas`、`gh`、`find`） |

---

## 6. 單位、GUID 與命名（重要的坑）

- **單位 = 文件的模型單位**（例如模型單位是公分、公差 0.01）。所有長度參數省略 `units` 時就是模型單位，**不是公釐**。要明確指定可傳 `units: "mm"/"cm"/"m"/"km"/"in"/"ft"`。`rhino_get_info` 會告訴你目前單位。
- 物件用 **GUID**（Rhino 物件 id）；Grasshopper 物件用 **instance GUID**（`gh_add_*` / `gh_list_document` 回傳的 `id`）。不要拿元件的 component GUID 當 id。
- 圖層用完整路徑 `Parent::Child`，不存在就建立。
- Grasshopper 元件 / 埠用 **Name 或 NickName（不分大小寫）或索引**指定。例：Circle 元件輸入 `Plane`(`P`)、`Radius`(`R`)；Extrude 輸入 `Base`(`B`)、`Direction`(`D`)。
  Number Slider / Panel / Boolean Toggle 本身就是「參數」，輸出就是自己（可省略 `source_output`）。
- 同名元件常有多個（例如 `Circle` 有 Curve 分類的元件，也有 Params 分類的參數；`Construct Point` 有過時版本）。
  `gh_add_component` 遇到無法判斷時會列出候選並要求加 `category` / `subcategory` 或直接給 `guid`。
- Extrude 的 `Direction` 是必填輸入，沒連線時要用 `gh_set_input` 設 `[0,0,10]` 之類的向量。
- 輸出若是 `Box` / `Circle` / `Line` 這類 GH 值型別，`gh_bake` 會自動轉成 Brep / 曲線再放進文件。

### IronPython 2.7 規則（寫 `rhino_eval_python` / `gh_eval_python` 時）

- 是 **Python 2.7**：沒有 f-string、沒有型別註記、`print` 可無括號、整數除法會截斷（用 `1.0 * a / b`）。
- 含中文的檔案開頭要 `# encoding: utf-8`；字串用 `u'中文'`。`json.dumps` 預設 ASCII 轉義，傳回前端沒問題。
- 預設名稱：`Rhino`、`rg`（`Rhino.Geometry`）、`rdo`（`Rhino.DocObjects`）、`rs`（rhinoscriptsyntax）、`sc`（scriptcontext）、`System`、`math`、`json`、`doc`（目前文件）。
  `gh_eval_python` 另有 `G`、`ghdoc`、`canvas`、`gh`、`find`。
- 結果放進變數 `result`；回傳值必須能被 JSON 序列化（Point3d、BoundingBox 等會自動轉成陣列 / 字典）。
- 整段程式是一個 undo record。要新增物件請用 `doc.Objects.Add...`；改完不需要手動 `doc.Views.Redraw()`（橋接器會做）。
- 用到 `rs.*` 函式時，`sc.doc` 已在每次請求前設成目前文件。

---

## 7. CLI 用法

`python -m rhino_bridge <子指令>`（venv 的 python）。輸出一律是 JSON。結束碼：`0` 成功、`1` 指令失敗、`2` 連不上 Rhino、`3` 截圖失敗。

```powershell
$py = "$env:USERPROFILE\RhinoAgentBridge\venv\Scripts\python.exe"
& $py -m rhino_bridge launch                 # 沒開就啟動 Rhino
& $py -m rhino_bridge info
& $py -m rhino_bridge box --width 20 --depth 20 --height 10 --layer Test::A
& $py -m rhino_bridge view --view iso --zoom-extents
& $py -m rhino_bridge screenshot --out C:\temp\view.png
& $py -m rhino_bridge gh-slider --min 1 --max 20 --value 6 --nickname R
```

完整子指令見 `python -m rhino_bridge --help`；給 agent 的版本見 `AGENT-GUIDE.zh-TW.md`。

---

## 8. 安全性與限制

- 只監聽 `127.0.0.1`；單行訊息上限 16 MB；最多同時 8 個連線。
- **UI 執行緒**：所有 RhinoCommon / GH 呼叫都在 UI 執行緒執行，請求逾時（預設 120 秒；client 會把自己的逾時減 1 秒放在請求的 `timeout` 欄位）會回傳 `TimeoutError`，之後的請求若遇到仍在跑的舊請求回傳 `BusyError`（`ping` / `get_info` / `cancel_command` / `gh_status` 例外）。
- **模態對話框無法由橋接器處理**：Rhino 或 GH 跳出對話框（授權、存檔詢問、錯誤框）時，UI 執行緒被擋住，請求會逾時。請使用者手動關閉；用 `rhino_window_screenshot` 可看到對話框內容。
- 檔案：`save` 要 `confirm`；`save_as` / `export` 覆寫要 `overwrite`；`new` / `open` 要 `confirm`（內部會把 `Modified` 設成 false 以免跳存檔詢問，所以**未存的變更會被丟掉**，請先問使用者）。
- `rhino_run_command` 若指令停在提示等輸入，後續請求會顯示 `in_command`；用 `rhino_cancel_command` 解除。需要檔案對話框的指令（例如 `_Open`）請改用 `-_Open "路徑"` 形式或 `rhino_model_file`。
- 沒有關閉 Rhino 的工具。Rhino 關閉時若文件有未存變更，會跳出 Yes/No/Cancel 對話框，需使用者處理。
- 未測試的成功路徑：`rhino_model_file` 的 `new` / `open` / `save` / `save_as` 成功流程（測試時為避免動到使用者檔案，只驗證了「沒給 confirm 會被拒絕」與 `export`）。

---

## 9. 疑難排解

| 症狀 | 處理 |
|---|---|
| `RHINO_NOT_AVAILABLE` | Rhino 沒開 → `rhino_launch`；外掛沒裝 → 跑 `install.ps1`；伺服器被停了 → Rhino 指令列 `RhinoBridgeStart` |
| 工具逾時 | Rhino 在跑長運算或卡在對話框 → `rhino_window_screenshot` 看狀況；`rhino_cancel_command` |
| Rhino 開了但連不上 | 指令列輸入 `RhinoBridgeStatus` 看連接埠；看 `%LOCALAPPDATA%\RhinoAgentBridge\bridge.log` |
| 改了 `py\*.py` 沒生效 | `python -m rhino_bridge raw reload_scripts` |
| 安裝時 `.rhp` 無法覆蓋 | Rhino 正在使用，關閉後重跑 |
| 升級後想回復登錄機碼 | 雙擊 `%LOCALAPPDATA%\RhinoAgentBridge\backup\plugin_key_*.reg` |

---

## 10. 開發與測試

```powershell
cd agent_client
& "$env:USERPROFILE\RhinoAgentBridge\venv\Scripts\python.exe" -m pip install -q pytest
& "$env:USERPROFILE\RhinoAgentBridge\venv\Scripts\python.exe" -m pytest -q --tb=line
```

測試用假 TCP server 模擬 Rhino 端協定（分段、大訊息、錯誤、逾時、斷線），並檢查 CLI 結束碼、MCP 工具名稱與 `RHINO_NOT_AVAILABLE` 前綴。
Rhino 端的實機測試需要開著的 Rhino（見上方各工具的行為說明）。

專案結構：

```
rhino\
  rhino_plugin\src\BridgePlugIn.cs     C# 外掛本體
  rhino_plugin\build.ps1               編譯腳本（csc）
  rhino_plugin\py\*.py                 Rhino 端指令腳本（IronPython 2.7）
  agent_client\rhino_bridge\           Python client / MCP server / CLI
  agent_client\tests\                  單元測試（假伺服器）
  install.ps1 / uninstall.ps1
  AGENT-GUIDE.zh-TW.md  mcp-examples\
```
