# SketchUp Agent Bridge（SketchUp 代理操控插件）

[English](README.md) ・ **繁體中文**

讓本機的 AI agent（Claude Code 或任何支援 MCP 的客戶端）能接管 SketchUp 2024：
看到畫面、用 Ruby API 建模與修改幾何、切換工具。

- **看**：SketchUp 視埠截圖（`view.write_image`）＋ OS 層全視窗截圖（含工具列、面板、對話框）
- **做**：完整 SketchUp Ruby API（`eval_ruby`）＋ 一組常用建模捷徑指令
- **不做**：不模擬滑鼠鍵盤。全視窗截圖「僅供觀看」。

> **安全前提**：SketchUp 內的伺服器**預設關閉**，只綁 `127.0.0.1`，
> 必須由你本人從「延伸程式(Extensions) → Agent Bridge → 啟動伺服器」手動開啟。
> AI agent 沒有辦法自己打開它。

---

## 1. 架構

```
┌──────────────────────────── SketchUp 2024（Ruby 3.2）────────────────────────────┐
│  Plugins\su_agent_bridge.rb          ← SketchupExtension 載入器                  │
│  Plugins\su_agent_bridge\                                                         │
│      main.rb        選單（啟動/停止/狀態/設定連接埠）＋ 公開 API                 │
│      server.rb      非阻塞 TCP server：UI.start_timer 輪詢 accept/read/write      │
│      commands.rb    27 個指令的實作（全部在主執行緒執行）                        │
│      units.rb       mm/cm/m/in/ft ↔ SketchUp 內部英吋                            │
│      serialize.rb   SketchUp 物件 → 可 JSON 化的資料                              │
└──────────────────────────────────┬────────────────────────────────────────────────┘
                                   │  127.0.0.1:9876
                                   │  換行分隔 JSON（newline-delimited JSON）
                                   │  {"id":1,"method":"ping","params":{}}
                                   │  {"id":1,"ok":true,"result":{...}}
┌──────────────────────────────────┴────────────────────────────────────────────────┐
│  Python 套件 sketchup_bridge（單一來源，兩層薄殼）                                │
│                                                                                    │
│      client.py          ← 唯一實作協定的地方（連線、逾時、錯誤訊息）              │
│      window_capture.py  ← Win32 PrintWindow / ImageGrab 全視窗截圖                │
│      images.py          ← 縮圖 / 編碼，控制回傳給模型的圖片大小                   │
│           ├── mcp_server.py   MCP server（stdio，18 個 tool）← 主要介面           │
│           └── cli.py          CLI（除錯 / 備援 / 批次）                           │
└──────────────────────────────────┬────────────────────────────────────────────────┘
                                   │ stdio (MCP) / 直接執行 (CLI)
                     ┌─────────────┼─────────────┐
              Claude Code / 任何 MCP 客戶端
```

MCP server 與 CLI 都只呼叫 `client.py`，**協定邏輯不重複實作**。

---

## 2. 安裝

```powershell
cd "<本資料夾（sketchup/）的路徑>"
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

`install.ps1` 會做四件事（可重複執行，只碰自己的檔案）：

1. 自動偵測已安裝的 SketchUp 版本，複製 Ruby 擴充功能到各版本的
   `%APPDATA%\SketchUp\SketchUp 20xx\SketchUp\Plugins\`
   （只碰 `su_agent_bridge.rb` 與 `su_agent_bridge\`，不動其他插件）
2. 自動尋找 Python 3.10+，在 `%USERPROFILE%\SketchUpAgentBridge\venv` 建立獨立 venv，
   安裝 `mcp`、`pillow` 與本專案的 `sketchup_bridge` 套件
3. 把 `sketchup-control` skill 複製到 Claude Code 的 `~\.claude\skills\`
4. 註冊 MCP：Claude Code（有 `claude` 指令時用 `claude mcp add --scope user`）、
   Claude Desktop（偵測到時合併寫入 `claude_desktop_config.json`，改前會備份）

常用參數：`-PipIndex <鏡像網址>`（PyPI 很慢時）、`-Python <python.exe>`、
`-PluginsDir <路徑>`、`-SkipVenv`、`-SkipSkills`、`-SkipClaudeCodeMcp`、`-SkipDesktopMcp`。

> **為什麼 venv 放在 `%USERPROFILE%\SketchUpAgentBridge\` 而不是專案裡？**
> 專案資料夾的路徑可能含中文或空白，也可能位於同步資料夾。
> 把 venv 放在純 ASCII 路徑後，MCP 啟動指令完全不含中文字元，
> 避免任何非 UTF-8 命令列編碼的風險，也不會被同步工具搬動。
> 套件是用 `pip install` **複製**進 venv 的，所以**改了程式碼要重跑 `install.ps1`** 才會生效。

解除安裝：

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1          # 保留 venv
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -RemoveVenv
```

---

## 3. 在 SketchUp 啟動伺服器

1. 開啟 SketchUp 2024
2. 選單 **延伸程式(Extensions) → Agent Bridge → 啟動伺服器**
3. 出現「Agent Bridge 伺服器已啟動 127.0.0.1:9876」即可

其他選單項目：

| 項目 | 說明 |
|---|---|
| 啟動伺服器 | 開始監聽 127.0.0.1（伺服器執行中時會變灰） |
| 停止伺服器 | 關閉監聽並斷開所有連線 |
| 狀態… | 顯示執行狀態、連接埠、目前連線數 |
| 設定連接埠… | 改用其他連接埠（預設 9876，改完要重新啟動伺服器） |

狀態與錯誤訊息會寫到 SketchUp 狀態列與 Ruby 主控台（`視窗 → Ruby 主控台`）。

### 自動化／無人值守（選用）

正式使用請用選單。若要讓 SketchUp 一開啟就自動啟動伺服器（例如自動化測試），
可用 SketchUp 的命令列參數：

```powershell
& "C:\Program Files\SketchUp\SketchUp 2024\SketchUp.exe" -RubyStartup "C:\路徑\autostart.rb" "C:\路徑\某個可寫入的模型.skp"
```

`autostart.rb` 內容：

```ruby
# encoding: UTF-8
UI.start_timer(4.0, false) { SUAgentBridge.start_server(9876) }
```

> 注意：若傳入的 .skp 位於唯讀位置（例如 SketchUp 安裝目錄的範本），
> SketchUp 會跳出「檔案是唯讀的」對話框並卡住啟動流程。請先複製到可寫入的位置。

---

## 4. MCP 客戶端設定

MCP 啟動指令（絕對路徑、純 ASCII）：

```
%USERPROFILE%\SketchUpAgentBridge\venv\Scripts\python.exe -m sketchup_bridge.mcp_server
```

### 4.1 Claude Code

```powershell
claude mcp add sketchup --scope user -- "$env:USERPROFILE\SketchUpAgentBridge\venv\Scripts\python.exe" -m sketchup_bridge.mcp_server
claude mcp get sketchup      # 應顯示 Status: ✔ Connected
```

**參數順序很重要**：伺服器名稱要放在 `--scope` 之前，否則 CLI 會把 `-m` 當成自己的選項
（實測 Claude Code 2.1.268 會回報 `error: unknown option '-m'`；
`claude mcp add-json` 在該版本則會回報 `Invalid configuration: : Invalid input`）。

設定檔：`%USERPROFILE%\.claude.json` 的頂層 `mcpServers`。

### 4.2 其他 MCP 客戶端（Claude Desktop 等）

在該客戶端的 MCP 設定檔（`mcpServers`）加入下列項目（`install.ps1` 偵測到 Claude Desktop 時會自動合併，其他客戶端請手動）：

```json
{
  "mcpServers": {
    "sketchup": {
      "command": "C:\\Users\\<USER>\\SketchUpAgentBridge\\venv\\Scripts\\python.exe",
      "args": ["-m", "sketchup_bridge.mcp_server"],
      "env": {
        "SKETCHUP_BRIDGE_PORT": "9876",
        "SKETCHUP_BRIDGE_MAX_EDGE": "1280"
      }
    }
  }
}
```

完整範例在 `mcp-examples\`。

### 4.3 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `SKETCHUP_BRIDGE_HOST` | `127.0.0.1` | 應維持本機 |
| `SKETCHUP_BRIDGE_PORT` | `9876` | 要和 SketchUp 內的設定一致 |
| `SKETCHUP_BRIDGE_TIMEOUT` | `30` | 單一指令逾時秒數 |
| `SKETCHUP_BRIDGE_MAX_EDGE` | `1280` | 回傳給模型的截圖最長邊像素 |

---

## 5. Skill

`skill\sketchup-control\` 是**單一來源**的 Agent Skill，內容是 MCP tool 描述講不到的
工作流程知識：前置條件、`get_scene_info → 操作 → 截圖驗證 → undo` 的循環、
何時用捷徑 tool 何時用 `eval_ruby`、Ruby API 守則（單位、面方向、群組、persistent_id）、
安全守則、CLI 備援，以及 `references/ruby-snippets.md` 的常用程式碼片段。

`install.ps1` 會把它複製到 Claude Code 的使用者層級 skills 資料夾 `%USERPROFILE%\.claude\skills\sketchup-control\`。

其他支援 `SKILL.md` + YAML frontmatter 格式的 agent，把 `skill\sketchup-control\` 整個資料夾複製到它的 skills 資料夾即可。

> Skill 內引用的 CLI 路徑是
> `%USERPROFILE%\SketchUpAgentBridge\venv\Scripts\python.exe`。
> 若你改用不同的 venv 位置（`install.ps1 -VenvDir`），請一併修改 `SKILL.md`。

---

## 6. MCP 工具清單（18 個）

| Tool | 用途 |
|---|---|
| `sketchup_get_info` | 連線測試 + SketchUp 版本、模型路徑、單位、選取數、伺服器狀態 |
| `sketchup_get_scene_info` | 模型全貌：實體統計、圖層/標籤、材質、元件、場景、選取、bounding box |
| `sketchup_list_entities` | 列出實體（persistent_id / 型別 / 名稱 / bounds / 圖層 / 材質），可分頁與篩選 |
| `sketchup_select_entities` | 設定或清空選取集 |
| `sketchup_delete_entities` | 依 persistent_id 刪除 |
| `sketchup_viewport_screenshot` | 3D 視埠截圖（回傳 image content） |
| `sketchup_window_screenshot` | 全視窗截圖，含工具列與對話框（回傳 image content） |
| `sketchup_get_camera` | 取得相機 |
| `sketchup_set_camera` | 設定相機 / 標準視角 / zoom extents |
| `sketchup_send_action` | `Sketchup.send_action`（切換工具、改變檢視），可列出常用 action |
| `sketchup_create_box` | 建立軸對齊方塊 |
| `sketchup_create_face` | 用點陣列建面，可同時擠出 |
| `sketchup_push_pull` | 推拉既有的面 |
| `sketchup_transform_entities` | 移動 / 旋轉 / 縮放（可複製） |
| `sketchup_set_material` | 上材質 / 顏色 / 透明度 |
| `sketchup_eval_ruby` | 執行任意 Ruby（回傳 value / inspect / stdout / backtrace） |
| `sketchup_undo_redo` | 復原 / 重做 |
| `sketchup_model_file` | 開新檔 / 開啟 / 儲存（有安全旗標保護） |

CLI 另外還有 `create_group`、`get_selection`、`zoom_extents`、`select_tool`、
`list_commands`、`list_actions`，共 27 個協定指令。

**單位慣例**：對外一律預設**公釐(mm)**，可用 `units` 參數指定 `mm`/`cm`/`m`/`in`/`ft`。
內部自動換算成 SketchUp 的英吋 `Length`。回傳的長度也是同一單位。

**Undo**：每個會改模型的指令都包在一個 `model.start_operation` / `commit_operation` 內，
所以使用者按一次 Ctrl+Z 就能回復一步。指令中途失敗會 `abort_operation`。

---

## 7. CLI 用法

```powershell
$SK = "$env:USERPROFILE\SketchUpAgentBridge\venv\Scripts\python.exe"

& $SK -m sketchup_bridge ping
& $SK -m sketchup_bridge info
& $SK -m sketchup_bridge commands                 # 列出所有協定指令
& $SK -m sketchup_bridge actions                  # 列出常用 send_action
& $SK -m sketchup_bridge scene --limit 50
& $SK -m sketchup_bridge entities --scope all --types Face Group --limit 20
& $SK -m sketchup_bridge selection
& $SK -m sketchup_bridge select 12345 12346
& $SK -m sketchup_bridge delete 12345

& $SK -m sketchup_bridge box --width 3000 --depth 2000 --height 2800 --name "量體"
& $SK -m sketchup_bridge face --points "[[0,0,0],[2000,0,0],[2000,1500,0]]" --push-pull 500
& $SK -m sketchup_bridge pushpull 12345 300
& $SK -m sketchup_bridge group 12345 12346 --name "群組"
& $SK -m sketchup_bridge transform 12345 --translate "0,3000,0"
& $SK -m sketchup_bridge transform 12345 --rotate '{"origin":[0,0,0],"axis":[0,0,1],"angle_deg":45}'
& $SK -m sketchup_bridge material 12345 --material "紅磚" --color "#AA4433"

& $SK -m sketchup_bridge camera
& $SK -m sketchup_bridge set-camera --view iso --zoom-extents
& $SK -m sketchup_bridge tool line                # 短名稱：select/line/pushpull/move/…
& $SK -m sketchup_bridge action "viewTop:"

& $SK -m sketchup_bridge screenshot --out C:\temp\view.png --max-edge 1280
& $SK -m sketchup_bridge window-shot --out C:\temp\window.png
& $SK -m sketchup_bridge window-shot --list       # 列出找到的 SketchUp 視窗

& $SK -m sketchup_bridge undo
& $SK -m sketchup_bridge redo
& $SK -m sketchup_bridge model save --confirm     # 安全旗標，見下方
& $SK -m sketchup_bridge raw get_info --params '{"units":"m"}'
```

`eval` 三種輸入方式（Ruby 含引號時請用檔案或 stdin，避開 Windows 引號地獄）：

```powershell
& $SK -m sketchup_bridge eval "Sketchup.active_model.entities.size"
& $SK -m sketchup_bridge eval --file C:\temp\code.rb
Get-Content C:\temp\code.rb -Raw | & $SK -m sketchup_bridge eval -
```

離開碼：`0` 成功、`1` 指令執行失敗、`2` 連不上伺服器、`3` 截圖失敗。
所有輸出都是 JSON（截圖類會印出存檔路徑）。

其他全域選項：`--host`、`--port`、`--timeout`、`--units`。

---

## 8. 安全性

| 項目 | 設計 |
|---|---|
| **預設關閉** | 伺服器不會自動啟動，必須由使用者手動從選單開啟 |
| **只綁 localhost** | `TCPServer.new('127.0.0.1', port)`，區網與外網都連不進來 |
| **沒有驗證機制** | 任何能在這台電腦上執行程式的使用者都能連線——這是刻意的取捨（單機工具）。**不要**把它改成綁 `0.0.0.0` 或做 port forwarding |
| **`eval_ruby` 風險** | 它能執行**任何** Ruby：讀寫檔案、開行程、連網路。等於把這台電腦交給連線端。請只在你信任的 agent 上啟用，用完就從選單停止伺服器 |
| **連線數上限** | 同時最多 8 條連線；單一請求上限 8 MB |
| **存檔保護** | `save_model` 存回原路徑需 `confirm=true`、覆寫既有檔案需 `overwrite=true`；`new_model` / `open_model` 需 `confirm=true` |
| **不模擬輸入** | 不送出任何滑鼠或鍵盤事件；全視窗截圖只讀畫面 |
| **可復原** | 所有模型修改都包在具名的 undo operation 內 |

**建議習慣**：要用的時候才從選單啟動，用完就停止。

---

## 9. 疑難排解

| 症狀 | 處理 |
|---|---|
| CLI 回報「無法連線到 SketchUp Agent Bridge」 | SketchUp 沒開，或沒有從 延伸程式 → Agent Bridge → 啟動伺服器 |
| 選單裡沒有「Agent Bridge」 | 重跑 `install.ps1` 後**重新啟動 SketchUp**；或檢查 `視窗 → 擴充功能管理員` 內是否被停用 |
| 「連接埠 9876 已被其他程式佔用」 | 用 `netstat -ano \| findstr :9876` 找出佔用者，或從 設定連接埠… 改用別的埠（記得同步改 agent 設定的 `SKETCHUP_BRIDGE_PORT`） |
| 指令一直逾時 | SketchUp 可能跳出對話框卡住了——用 `window-shot` 看一眼；或加長 `--timeout` |
| `send_action` / `undo` 回應的狀態看起來沒更新 | `Sketchup.send_action` 是**非同步**的（排進 SketchUp 自己的訊息佇列）。回應中的 `active_tool_before` 是送出前的狀態；要確認結果請稍後重新查詢 |
| 截圖一片黑 | `PrintWindow` 對部分 GPU 合成的視窗會失敗，程式會自動退回 `ImageGrab`，並在 `notes` 註明；此時視窗需要實際顯示在螢幕上 |
| 全視窗截圖抓到錯的視窗 | 用 `window-shot --list` 看有哪些視窗；程式預設選面積最大的那個 |
| 改了 Python 程式碼但沒生效 | 套件是複製安裝的，請重跑 `install.ps1`（或只跑 `-SkipSkills`） |
| 改了 Ruby 程式碼但沒生效 | 重跑 `install.ps1` 後重新啟動 SketchUp；或在執行中用 `eval_ruby` 下 `load '…/su_agent_bridge/commands.rb'` 熱重載 |
| `install.ps1` 跳出一堆語法錯誤 | `.ps1` 必須是 **UTF-8 with BOM**，PowerShell 5.1 才不會把中文當成 ANSI |
| PowerShell 中文顯示成亂碼 | 設定 `$env:PYTHONIOENCODING="utf-8"`，或改用 Windows Terminal |

---

## 10. 開發與測試

```powershell
$VPY = "$env:USERPROFILE\SketchUpAgentBridge\venv\Scripts\python.exe"
$env:PYTHONPATH = "$PWD\agent_client"      # 在專案根目錄執行
& $VPY -m pytest "$PWD\agent_client\tests" -v
```

`tests\test_client.py` 用一個假的 TCP server 模擬協定，涵蓋往返、分段訊息、
多請求共用連線、大訊息（3 MB）、逾時、連線被拒、壞掉的 JSON、錯誤回應與 CLI 參數解析。

另見 `AGENT-GUIDE.zh-TW.md`（給沒有 MCP、只能跑腳本的 agent）。
