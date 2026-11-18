# Claude Code — 註冊 Revit MCP server

## 建議做法：用 CLI（會自動寫進使用者層級設定）

```powershell
claude mcp add revit --scope user -- "$env:USERPROFILE\RevitAgentBridge\venv\Scripts\python.exe" -m revit_bridge.mcp_server
```

> **參數順序很重要**（實測於 Claude Code 2.1.x）：
> 伺服器名稱要放在 `--scope` **之前**。若寫成
> `claude mcp add --scope user revit -- ...`，CLI 會把後面的 `-m` 當成自己的選項，
> 回報 `error: unknown option '-m'`。
>
> **在 PowerShell 腳本裡**，`claude` 若是 npm 的 `claude.ps1`，裸的 `--` 會被 PowerShell 吃掉，
> 同樣出現 `unknown option '-m'`。腳本內要寫成 `& claude mcp add revit --scope user '--' $python -m revit_bridge.mcp_server`
> （把 `--` 加引號）。直接在 Git Bash / cmd 打則不需要。

確認：

```powershell
claude mcp list
claude mcp get revit
```

移除：

```powershell
claude mcp remove --scope user revit
```

## 手動做法（CLI 不可用時）

使用者層級的 MCP 設定存在 `%USERPROFILE%\.claude.json` 的頂層 `mcpServers` 物件內。
**請先備份再手動編輯**，並且只新增 `revit` 這一個鍵、不要動其他既有內容：

```json
{
  "mcpServers": {
    "revit": {
      "type": "stdio",
      "command": "C:\\Users\\<USER>\\RevitAgentBridge\\venv\\Scripts\\python.exe",
      "args": ["-m", "revit_bridge.mcp_server"],
      "env": {}
    }
  }
}
```

（JSON 不會展開 `%USERPROFILE%`，請把 `<USER>` 換成你的 Windows 使用者名稱。）

## 可用的環境變數（選填，放進 `env`）

| 變數 | 預設 | 說明 |
|---|---|---|
| `REVIT_BRIDGE_HOST` | `127.0.0.1` | 只應維持本機 |
| `REVIT_BRIDGE_PORT` | `9879` | 要和 Revit 內伺服器連接埠一致（Revit 端同名變數也會改它） |
| `REVIT_BRIDGE_TIMEOUT` | `30` | 單一指令逾時秒數 |
| `REVIT_BRIDGE_MAX_EDGE` | `1280` | 回傳給模型的截圖最長邊像素 |
| `REVIT_EXE` | 預設安裝路徑 | `revit_launch` 使用的 Revit.exe |
