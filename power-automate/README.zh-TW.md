# Power Automate Agent Bridge（pad-bridge，Power Automate 代理操控）

[English](README.md) ・ **繁體中文**

讓 AI agent（Claude Code 或任何支援 MCP 的客戶端）操作本機的 Power Automate Desktop（PAD）：
執行桌面流程並取回結果、用 Robin 腳本建立或修改流程、查動作語法。

## 能做什麼

| MCP 工具 | 作用 | 怎麼做到的 |
|---|---|---|
| `pad_list_flows` | 列出流程、執行狀態、帳號 | UI Automation 讀 console 清單 |
| `pad_run_flow` | 執行流程（帶輸入），等結果，回傳輸出變數與失敗動作 | 官方 run URL + 讀本機執行紀錄 |
| `pad_get_run` / `pad_list_runs` | 查執行結果、最近紀錄 | 讀 `RunDefinition.json`、`Actions.log` |
| `pad_stop_flow` | 停止執行中的流程 | console 停止鈕 |
| `pad_get_dialogs` / `pad_dismiss_dialogs` | 看／關 console 的錯誤與輸出對話框 | UI Automation |
| `pad_get_flow_script` | 讀出流程的 Robin 腳本與變數 | 設計工具全選 → 複製 |
| `pad_create_flow` / `pad_update_flow` | 用 Robin 建立／改寫流程、建輸入輸出變數 | 設計工具貼上 + 變數對話框 |
| `pad_search_actions` / `pad_action_template` | 查動作目錄、取得 PAD 產生的正確語法 | 動作面板搜尋 → 加入暫存流程 → 複製 |

同一套功能也有 CLI：`python -m pad_bridge --help`。

## 已實測（2026-10-14，PAD 2.72 Store 版、個人 Microsoft 帳號）

- 免費個人帳號可以用 run URL 觸發流程（官方文件寫需要 Premium，實測不用）。
- 文字／數字／布林輸入、輸出變數、`&` `#` 引號 反斜線等特殊字元都正確傳遞。
- 執行失敗會回報出錯動作、錯誤代碼與訊息。
- 腳本語法錯誤會在儲存前從設計工具的錯誤清單抓出來（含行號），不會存進壞掉的流程。

## 限制與注意事項

- **確認對話框**：PAD 預設每次外部觸發都跳「外部來源正在嘗試執行以下流程」。
  可在 PAD console 設定關閉「從外部叫用流程時顯示確認對話方塊」，
  或安裝時加 `-AutoConfirm`（bridge 只替自己剛觸發、名稱相符的流程按繼續）。
- **會搶畫面**：建立／修改／讀腳本／查語法用真實鍵盤滑鼠操作設計工具，約 20–60 秒，期間別動電腦。
  執行流程本身不搶畫面，但流程內容若有滑鼠鍵盤動作就會。
- 同時只能跑一個流程（PAD attended 模式限制）。
- 讀寫腳本只涵蓋 **Main 子流程**。
- 輸入只支援文字、數字、布林；run URL 長度上限約 2000 字元，大量資料請傳檔案路徑。
- 前一次 URL 請求失敗時，PAD 有時會沿用舊的 runId 資料夾；bridge 會改用「觸發後最新一筆紀錄」對應。
- UI 元件以 AutomationId 為主，少數按鈕用繁中名稱（附英文備援）；PAD 改版可能需要調整。
- 必須在使用者自己的桌面工作階段執行；跑在別的 Windows 帳號或遠端工作階段的 agent 無法操作該使用者的 PAD。
- 第一次用 `pad_search_actions` / `pad_action_template` 會在帳號裡建立空流程 `_pad_bridge_scratch` 當畫布。

## 安裝

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
# 網路慢時可指定 PyPI 鏡像：-PipIndex <鏡像網址>
```

venv 在 `%USERPROFILE%\PADAgentBridge\venv`；skill 裝到 `~/.claude/skills/pad-control`；
MCP 以 `pad` 為名註冊到 Claude Code 使用者層級。其他 MCP 客戶端的設定範例見 `mcp-examples\`。

## 結構

```
agent_client/pad_bridge/
  uia.py         UI Automation 共用輔助（找元件、點擊、送鍵、剪貼簿、行程列舉）
  runs.py        讀本機執行紀錄（純檔案）
  console.py     console：列流程、run URL、對話框處理、停止
  designer.py    設計工具：讀寫腳本、變數、錯誤清單、動作目錄與範本
  mcp_server.py  MCP 薄殼
  cli.py         CLI
skill/pad-control/  給 agent 的工作流程與 Robin 片段（片段皆經 PAD 驗證）
```

測試（不需要 PAD 的純邏輯部分）：`python -m pytest agent_client/tests`
