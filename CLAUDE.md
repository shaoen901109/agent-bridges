# Project Overview
- 用途：agent-bridges — 讓 AI agent（MCP）操控本機 Blender／Rhino＋Grasshopper／SketchUp／Power Automate Desktop 的四個橋接工具。
- Structure: `blender/`、`rhino/`、`sketchup/`、`power-automate/`；各含程式、`README.md`（英）＋`README.zh-TW.md`（繁中）、`skill/`、`mcp-examples/`、安裝腳本。
- 通訊：各軟體內建 TCP 伺服器只綁 `127.0.0.1`（Blender 9877、Rhino 9878、SketchUp 9876）；Power Automate 走 UI Automation＋本機執行紀錄，無 TCP 埠。

# Commands (quiet)
- Test（Rhino／SketchUp／PAD 有 `agent_client/tests`）：`python -m pytest -q --tb=line <資料夾>/agent_client/tests`
- Blender 背景測試：`python blender/test_bg.py --port 9899`

# Constraints
- 不可放密鑰、`.env`、個人路徑（`C:\Users\<名字>`）、內網 IP、個人網域或 Email；路徑一律用 `%USERPROFILE%`／相對路徑。
- 伺服器只能綁 127.0.0.1，不加對外監聽。
- 改文字檔用 Edit／Write 工具；`.ps1` 需 UTF-8 with BOM。
