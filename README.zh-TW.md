# agent-bridges

[English](README.md) ・ **繁體中文**

> 📖 **圖文教學：** https://shao-en.com/blog/agent-bridges-guide.html ——如果你不知道該如何操作，請把這篇文章交給你的 Claude 或 ChatGPT。

讓 AI agent（Claude Code 或任何支援 MCP 的客戶端）操控你自己電腦上桌面軟體的本機橋接工具：
截圖、查詢文件、在軟體內執行程式、建模，並用截圖驗證結果。

| 橋接器 | 軟體 | 本機埠 | MCP 名稱 | 備註 |
|---|---|---|---|---|
| [`blender/`](blender/README.zh-TW.md) | Blender 3.4 | `127.0.0.1:9877` | `blender` | Blender 內的 startup 腳本；在主執行緒執行 `bpy` |
| [`rhino/`](rhino/README.zh-TW.md) | Rhino 7 + Grasshopper | `127.0.0.1:9878` | `rhino` | C# `.rhp` 外掛 + IronPython 腳本；46 個工具（`rhino_*`、`gh_*`） |
| [`sketchup/`](sketchup/README.zh-TW.md) | SketchUp 2024 | `127.0.0.1:9876` | `sketchup` | Ruby 擴充功能；伺服器**預設關閉**，須從選單手動啟動 |
| [`power-automate/`](power-automate/README.zh-TW.md) | Power Automate Desktop（PAD） | 無（UI Automation＋本機執行紀錄） | `pad` | 執行桌面流程、讀寫 Robin 腳本 |

## 架構

```
 AI agent（MCP 客戶端）                      其他工具／腳本
        │  stdio (MCP)                               │  CLI
        ▼                                            ▼
 ┌──────────────────────────────────────────────────────────┐
 │  Python 套件 <name>_bridge（venv 在 %USERPROFILE% 底下） │
 │   mcp_server.py   cli.py   ──►   client.py（協定）       │
 └───────────────────────────────┬──────────────────────────┘
                                 │ TCP、換行分隔 JSON，只限 127.0.0.1
                                 ▼
 ┌──────────────────────────────────────────────────────────┐
 │  軟體內部的伺服器（外掛／腳本）                          │
 │  請求先排隊，再於 UI／主執行緒執行                       │
 └──────────────────────────────────────────────────────────┘
```

Power Automate Desktop 沒有可用的腳本 socket，所以 `pad-bridge` 改用 Windows UI Automation
操作 PAD，並讀取 PAD 的本機執行紀錄。

每個橋接器包含三樣東西：**Python MCP 伺服器＋CLI**、**軟體內的伺服器**、
以及一份 **Claude skill**（`skill/<name>-control/`，放工作流程守則與程式片段）。

## 快速開始

需求：Windows 10/11、Python 3.10+、對應的軟體（見上表）。

```powershell
# Rhino / SketchUp / Power Automate（PowerShell）
cd rhino            # 或 sketchup、power-automate
powershell -ExecutionPolicy Bypass -File .\install.ps1
# PyPI 太慢？加上：-PipIndex https://你的鏡像網址/simple

# Blender
cd blender
python install.py
```

安裝程式會把 MCP 註冊到 **Claude Code**（`claude mcp add --scope user ...`），並把 skill 複製到
`~\.claude\skills\`。其他 MCP 客戶端請看各橋接器的 `mcp-examples\` 資料夾
（`mcp-config.json` 是通用的 `mcpServers` 範例）。安裝後請重啟軟體與 MCP 客戶端。
詳細說明、工具清單與疑難排解請看各橋接器的 README。

## 安全設計

- 伺服器**只綁 `127.0.0.1`**，不對區網或網際網路開放。沒有驗證機制，任何能在你電腦上執行程式的人都能連線，
  所以**不要**改成對外監聽，也不要做 port forwarding。
- 能執行任意程式的工具（`*_eval_python`、`sketchup_eval_ruby`、`gh_eval_python`）等於把軟體
  （以及語言執行環境所能碰到的使用者帳號權限）交給 agent，請只對你信任的 agent 啟用。
- SketchUp 的伺服器預設關閉，必須由你從選單啟動。
- 覆蓋檔案、在有未存變更時開新檔／開檔，都需要明確的 `confirm`／`overwrite` 旗標。
  任何橋接器都沒有「關閉／強制結束軟體」的工具。
- 軟體支援時，模型修改都包在 undo 記錄裡。

## 專案結構

```
blender/  rhino/  sketchup/  power-automate/
  README.md、README.zh-TW.md      各橋接器文件
  install.(ps1|py)、uninstall.ps1
  skill/                          Claude skill（SKILL.md＋references）
  mcp-examples/                   MCP 客戶端設定範例
  agent_client/ 或 <name>_bridge/    Python 套件（MCP 伺服器、CLI、client）
  tests/                          單元測試（假 TCP 伺服器）
```

## 技術來源與致謝

| 用途 | 元件 | 授權 |
|---|---|---|
| MCP 伺服器框架 | [Model Context Protocol](https://modelcontextprotocol.io/) Python SDK (`mcp`) | MIT |
| 截圖處理（選用） | [Pillow](https://python-pillow.org/) | HPND |
| Windows UI 自動化（power-automate 橋接） | [uiautomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) | Apache-2.0 |
| 測試 | [pytest](https://pytest.org/) | MIT |
| 被操控的軟體（未附帶） | Blender, Rhino / Grasshopper (McNeel), SketchUp (Trimble), Power Automate Desktop (Microsoft) | 見各家原廠 |

開發過程使用 [Claude Code](https://claude.com/claude-code)（Anthropic）協助。

## 作者

- 網站：https://shaoenstudio.com
- 聯絡信箱：shaoenstudio@gmail.com
- GitHub：[@shaoen901109](https://github.com/shaoen901109)

## 授權

[MIT](LICENSE)
