# Revit Agent Bridge（Revit 代理操控插件）

[English](README.md) ・ **繁體中文**

讓本機的 AI agent（Claude Code 或任何支援 MCP 的客戶端）能接管 **Revit 2024**：
看到畫面、查詢元件與參數、建牆／樓板／樓層、放門窗與族群、改參數，並可用 C#（Revit API）處理其他一切。

- **看**：視圖截圖（`ExportImage`，作用中視圖）、OS 層全視窗截圖（含功能區、專案瀏覽器、對話框）
- **做**：一組建模捷徑、參數讀寫、C# 片段（`revit_eval_csharp`，完整 Revit API），共 18 個 MCP 工具：`revit_*`
- **不做**：不模擬滑鼠鍵盤、不自動關閉對話框、沒有關閉 / 結束 Revit 的工具。全視窗截圖「僅供觀看」。

> **安全前提**：伺服器只綁 `127.0.0.1`（本機），不接受外部連線；連線送來的內容不是 JSON（例如瀏覽器網頁對本機埠發的 HTTP 請求）就立刻斷線。
> 外掛安裝後**預設隨 Revit 啟動自動開伺服器**（環境變數 `REVIT_BRIDGE_AUTOSTART=0` 可關掉，功能區「Agent Bridge」頁籤可手動啟停）。
> 覆蓋檔案、新檔、開檔一律要使用者同意（`confirm` / `overwrite`）。

> **實測狀態**：於 Revit 2024.0.4（繁體中文預設樣板）實機驗證，18 個工具全部通過
> （建牆、樓板、樓層、門窗、參數讀寫、選取、刪除、復原、視圖匯出、全視窗截圖、C# 片段、存檔，以及 MCP stdio）。

---

## 1. 架構與為什麼這樣做

```
┌──────────────────────────── Revit 2024（.NET Framework 4.8）────────────────────────────┐
│  RevitAgentBridge.dll   （C#，IExternalApplication，用 csc 編譯，C# 5 語法）            │
│      App            功能區「Agent Bridge」頁籤、啟動 / 停止 / 狀態、DialogBoxShowing 記錄  │
│      BridgeServer   TcpListener(127.0.0.1)；背景執行緒 accept ＋ 每個連線一條讀取執行緒   │
│      ApiBridge      請求佇列 + ExternalEvent：所有 Revit API 呼叫都在 Revit API 執行緒    │
│      Handlers       查詢 / 建模 / 視圖 / 截圖 / 檔案；Evaluator 負責 eval_csharp          │
└──────────────────────────────┬────────────────────────────────────────────────────────────┘
                               │  127.0.0.1:9879（預設；本 repo 內 SketchUp 用 9876、Blender 用 9877、Rhino 用 9878）
                               │  換行分隔 JSON：{"id":1,"method":"ping","params":{},"timeout":29}
                               │                 {"id":1,"ok":true,"result":{...}}
┌──────────────────────────────┴────────────────────────────────────────────────────────────┐
│  Python 套件 revit_bridge（單一來源，兩層薄殼）                                           │
│      client.py          ← 唯一實作協定的地方（連線、逾時、錯誤訊息）                      │
│      launcher.py        ← revit_launch：啟動 Revit 並等橋接器就緒                         │
│      window_capture.py  ← Win32 PrintWindow / ImageGrab 全視窗截圖                        │
│      images.py          ← 縮圖 / 編碼，控制回傳給模型的圖片大小                           │
│           ├── mcp_server.py   MCP server（stdio，18 個 tool：revit_*）← 主要介面         │
│           └── cli.py          CLI（除錯 / 備援 / 批次）                                   │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

### 關鍵設計

| 考量 | 做法 |
|---|---|
| Revit API **只能在 Revit 的 API 執行緒**呼叫 | 背景執行緒收請求 → 放進佇列 → `ExternalEvent.Raise()`，由 `IExternalEventHandler.Execute` 在 API 執行緒清空佇列處理。Revit 忙碌時事件不會立刻觸發，所以每 500 ms 重新 Raise 直到請求開始執行。 |
| 請求逾時不能卡死、也不能事後偷偷執行 | 每個請求有 Pending / Started / Abandoned 狀態：逾時而尚未開始的請求會被標成放棄，之後輪到它時直接略過。 |
| Revit 停在模態對話框時，API 執行緒不會處理事件 | `ping`、`get_status`、`list_commands` 不進 API 執行緒，永遠可回；`get_info` 逾時 4 秒後回快取（`stale=true`）。錯誤訊息一律提示「Revit 可能停在對話框，請使用者處理」。對話框只訂閱 `DialogBoxShowing` 記錄日誌，**絕不自動關閉**。 |
| 要能不裝 Visual Studio 就編譯 | `build.ps1` 優先用 VS Build Tools 的 Roslyn `csc`，沒有就退回 .NET Framework 內建 `csc`（C# 5，原始碼刻意只用 C# 5 語法，編譯時加 `/langversion:5`）。 |
| JSON 序列化不想多帶第三方 DLL | 用 .NET 內建 `JavaScriptSerializer`，不用 Newtonsoft。 |
| 修改要可退回、要避免警告對話框 | 每個修改指令是一個 Transaction，附 `WarningSwallower`：警告自動刪除並列在回應的 `warnings`，有錯誤則整筆 RollBack。 |
| `eval_csharp` | 片段包進 `AgentEval.Snippet.Run(UIApplication uiapp, UIDocument uidoc, Document doc, Action<object> print)`，用 CodeDom 在記憶體內編譯（C# 5）；包裝固定 3 行前綴，編譯錯誤的行號會換算成「片段內的行號」；開頭的 `using` 會被提到最上面。 |
| 單位 | 外部工具一律**公釐**（協定、MCP、CLI）；`eval_csharp` 內是 Revit 內部單位**英呎**（1 ft = 304.8 mm），工具說明與 skill 都明講。 |

### 為什麼用 .addin 清單檔註冊

Revit 啟動時會掃描 `%APPDATA%\Autodesk\Revit\Addins\2024\*.addin`（使用者層級，不需系統管理員）。
`install.ps1` 在那裡寫入 `RevitAgentBridge.addin`（固定 GUID），
DLL 放在 `%APPDATA%\RevitAgentBridge\addin\`。第一次載入 Revit 會問「是否載入此外掛」，選**永遠載入**。
解除安裝只刪這一個 `.addin` 檔與自己的資料夾。

---

## 2. 安裝

需求：Windows、Revit 2024（預設 `C:\Program Files\Autodesk\Revit 2024`）、Python 3.10+、.NET Framework 4.x（Windows 內建）。

```powershell
# 先關閉 Revit（DLL 會被 Revit 鎖住）
cd revit
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

腳本依序做（每一步可重複執行）：

1. 編譯 `RevitAgentBridge.dll` → `%APPDATA%\RevitAgentBridge\addin\`
2. 寫入 `%APPDATA%\Autodesk\Revit\Addins\2024\RevitAgentBridge.addin`（已存在先備份）
3. 在 `%USERPROFILE%\RevitAgentBridge\venv` 建立 venv 並安裝 `revit_bridge`（預設官方 PyPI；網路慢可加 `-PipIndex <鏡像網址>`）
4. 複製 `skill\revit-control` 到 Claude Code 的 `~\.claude\skills\`
5. 註冊 MCP「revit」到 Claude Code（`claude mcp add`，只新增 / 更新 `revit` 這一筆）。其他 MCP 客戶端請參考 `mcp-examples\` 手動設定。

常用參數：`-SkipBuild`、`-SkipVenv`、`-SkipSkills`、`-SkipClaudeCodeMcp`、`-PipIndex`、
`-RevitDir`（Revit 安裝路徑，預設 `C:\Program Files\Autodesk\Revit 2024`）、`-Python`。

解除安裝：`powershell -ExecutionPolicy Bypass -File .\uninstall.ps1`（加 `-RemoveVenv` 連 venv 一起刪）。

安裝後：啟動 Revit，第一次選「永遠載入」；功能區出現「Agent Bridge」頁籤即成功。
日誌在 `%LOCALAPPDATA%\RevitAgentBridge\bridge.log`。

---

## 3. 專案結構

```
revit\
├── install.ps1 / uninstall.ps1
├── revit_plugin\
│   ├── build.ps1                編譯（-RunTests 另跑不依賴 Revit 的包裝邏輯測試）
│   ├── src\                     App / Server / Handlers / Eval / EvalWrapper / Util（.cs）
│   └── tests\EvalWrapperTests.cs
├── agent_client\
│   ├── pyproject.toml / requirements.txt
│   ├── revit_bridge\            client / mcp_server / cli / launcher / window_capture / images
│   └── tests\test_client.py     假 TCP server 測試
├── skill\revit-control\         SKILL.md + references\csharp-snippets.md
├── mcp-examples\                Claude Code / 通用 MCP 設定範例
└── AGENT-GUIDE.zh-TW.md         CLI 版（給沒有 MCP 的 agent）
```

---

## 4. 通訊協定

換行分隔 JSON，一行一則：

```json
{"id": 1, "method": "create_wall", "params": {"start": [0,0], "end": [5000,0], "height": 3000}, "timeout": 29}
{"id": 1, "ok": true, "result": {"id": 312456, "length_mm": 5000.0}}
{"id": 1, "ok": false, "error": {"message": "...", "class": "BridgeError", "backtrace": ["..."]}}
```

- 埠預設 9879，環境變數 `REVIT_BRIDGE_PORT` 可改（Revit 端與 client 端都吃這個變數）。
- `timeout`（秒）是 client 告訴 Revit 端「我最多等幾秒」，通常比 client 自己的逾時少 1 秒。
- 協定指令：`ping`、`get_status`、`list_commands`（不進 API 執行緒）；`get_info`、`get_scene_info`、`list_elements`、
  `get_element`、`set_parameter`、`create_wall`、`create_floor`、`create_level`、`place_family_instance`、
  `delete_elements`、`select_elements`、`set_view`、`screenshot`、`eval_csharp`、`new_model`、`open_model`、
  `save_model`、`undo`、`redo`。

---

## 5. MCP 工具對照（18 個）

| 類別 | 工具 |
|---|---|
| 狀態 | `revit_get_info`、`revit_get_scene_info`、`revit_launch` |
| 查詢 | `revit_list_elements`、`revit_get_element` |
| 建模 | `revit_create_wall`、`revit_create_floor`、`revit_create_level`、`revit_place_family_instance` |
| 修改 | `revit_set_parameter`、`revit_delete_elements`、`revit_select_elements` |
| 視圖與截圖 | `revit_set_view`、`revit_view_screenshot`、`revit_window_screenshot` |
| 任意 C# | `revit_eval_csharp` |
| 復原與檔案 | `revit_undo`、`revit_model_file` |

錯誤字串前綴：`REVIT_NOT_AVAILABLE`（連不上）、`REVIT_COMMAND_FAILED`（Revit 端回報失敗）、`REVIT_WINDOW_NOT_FOUND`（全視窗截圖找不到視窗）。

### 單位與截圖的坑

- 外部工具的長度一律**公釐**；`revit_eval_csharp` 內是 Revit 內部單位**英呎**（1 ft = 304.8 mm），換算要自己做。
- 視圖截圖用 `ExportImage` 匯出**作用中視圖**。視圖若以 DirectX / 硬體加速圖形顯示，匯出結果可能與螢幕上看到的略有差異；
  要確認實際畫面請用 `revit_window_screenshot`。

---

## 6. CLI

`python -m revit_bridge <子指令>`，離開碼 `0` 成功 / `1` 指令失敗 / `2` 連不上 / `3` 截圖失敗。
完整子指令見 `python -m revit_bridge --help`；給 agent 的版本見 `AGENT-GUIDE.zh-TW.md`。

---

## 7. 環境變數與 MCP 客戶端設定

`install.ps1` 只會自動註冊 Claude Code；其他 MCP 客戶端請參考 `mcp-examples\`（`mcp-config.json` 是通用的 `mcpServers` 範例）。

| 變數 | 預設 | 說明 |
|---|---|---|
| `REVIT_BRIDGE_PORT` | `9879` | Revit 端與 client 端共用 |
| `REVIT_BRIDGE_HOST` | `127.0.0.1` | client 端；伺服器永遠只綁本機 |
| `REVIT_BRIDGE_TIMEOUT` | `30` | 單一指令逾時秒數 |
| `REVIT_BRIDGE_MAX_EDGE` | `1280` | 回傳給模型的截圖最長邊像素 |
| `REVIT_BRIDGE_AUTOSTART` | `1` | Revit 端：設 `0` 則啟動時不自動開伺服器 |
| `REVIT_EXE` | 預設安裝路徑 | `revit_launch` 使用的 Revit.exe |

---

## 8. 安全性與限制

- 只監聽 `127.0.0.1`；沒有驗證機制。連線送來的資料不是 JSON（例如瀏覽器的 HTTP 請求）會立刻斷線，不會執行任何東西。
- `revit_eval_csharp` 可執行任意程式並使用完整 Revit API，請只搭配你信任的 agent。
- 逾時而尚未開始執行的請求會被標成放棄，之後不會偷偷執行。
- **模態對話框無法由橋接器處理**：Revit 跳出對話框時 API 請求會逾時。請使用者手動關閉；用 `revit_window_screenshot` 可看到對話框內容。
- 覆蓋檔案、`new`、`open` 都要明確的確認旗標；沒有關閉 Revit 的工具。

---

## 9. 疑難排解

| 症狀 | 做法 |
|---|---|
| `install.ps1` 說 Revit 正在執行 | 存檔後關閉 Revit，重跑 |
| Revit 啟動後沒有「Agent Bridge」頁籤 | 看 `%LOCALAPPDATA%\RevitAgentBridge\bridge.log`；確認 `.addin` 的 `<Assembly>` 路徑存在；第一次的安全性對話框若選了「不載入」，刪除 `.addin` 後重跑 `install.ps1`、再開 Revit 重選 |
| `REVIT_NOT_AVAILABLE` | Revit 沒開 → `revit_launch`；或在功能區按「啟動伺服器」 |
| 請求逾時 | Revit 停在對話框或在做長時間運算；`revit_window_screenshot` 看一下，請使用者處理 |
| 編譯錯誤（build.ps1） | 確認 `-RevitDir` 指到有 `RevitAPI.dll` 的資料夾；原始碼只能 C# 5 |

---

## 10. 驗證狀態與測試

無 Revit 環境即可驗證：
- `build.ps1` 編譯通過（Roslyn csc 與 Framework C# 5 csc 皆可）
- 不依賴 Revit 的 `eval_csharp` 包裝與行號換算：`build.ps1 -RunTests`，23 項檢查
- Python 端：`python -m pytest -q --tb=line agent_client\tests`（假 TCP server，涵蓋 client、CLI 離開碼、MCP 工具名稱與錯誤前綴）

**已實機驗證**（Revit 2024.0.4）：
- `revit_launch` 啟動到伺服器就緒約 40 秒；`model_file new` 自動用繁中預設樣板
- 建 4 面牆、樓板、樓層（含平面視圖）、在牆上放門與窗；`get_element`／`set_parameter`（窗台高度 900→1000 mm）
- `select`、`delete`、`undo`（PostCommand，非同步，下一次呼叫時已生效）、`save`
- `set_view`（`3d`、同名平面圖會選樓板平面圖）、`ExportImage` 匯出、全視窗截圖
- `eval_csharp`：回傳值序列化、print、編譯錯誤行號、執行期例外堆疊、相同程式碼重跑走編譯快取
- MCP stdio：列出 18 個工具、圖片以 image content 回傳
- `model_file open` 開既有檔；瀏覽器式 HTTP 請求會被直接斷線、不執行

**尚未實機驗證**：
- `save_as` 覆寫
- Revit 停在對話框時的逾時訊息與 `DialogBoxShowing` 記錄
