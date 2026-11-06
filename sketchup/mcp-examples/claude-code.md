# Claude Code — 註冊 SketchUp MCP server

## 建議做法：用 CLI（會自動寫進使用者層級設定）

```powershell
claude mcp add sketchup --scope user -- "$env:USERPROFILE\SketchUpAgentBridge\venv\Scripts\python.exe" -m sketchup_bridge.mcp_server
```

> **參數順序很重要**（實測於 Claude Code 2.1.268）：
> 伺服器名稱要放在 `--scope` **之前**。若寫成
> `claude mcp add --scope user sketchup -- ...`，CLI 會把後面的 `-m` 當成自己的選項，
> 回報 `error: unknown option '-m'`。
> `claude mcp add-json` 在這個版本會回報 `Invalid configuration: : Invalid input`，
> 請用上面的 `claude mcp add` 寫法。

確認：

```powershell
claude mcp list
claude mcp get sketchup
```

移除：

```powershell
claude mcp remove --scope user sketchup
```

## 手動做法（CLI 不可用時）

使用者層級的 MCP 設定存在 `%USERPROFILE%\.claude.json` 的頂層 `mcpServers` 物件內。
**請先備份再手動編輯**，並且只新增 `sketchup` 這一個鍵、不要動其他既有內容：

```json
{
  "mcpServers": {
    "sketchup": {
      "type": "stdio",
      "command": "C:\\Users\\<使用者名稱>\\SketchUpAgentBridge\\venv\\Scripts\\python.exe",
      "args": ["-m", "sketchup_bridge.mcp_server"],
      "env": {}
    }
  }
}
```

## 可用的環境變數（選填，放進 `env`）

| 變數 | 預設 | 說明 |
|---|---|---|
| `SKETCHUP_BRIDGE_HOST` | `127.0.0.1` | 只應維持本機 |
| `SKETCHUP_BRIDGE_PORT` | `9876` | 要和 SketchUp 內設定的連接埠一致 |
| `SKETCHUP_BRIDGE_TIMEOUT` | `30` | 單一指令逾時秒數 |
| `SKETCHUP_BRIDGE_MAX_EDGE` | `1280` | 回傳給模型的截圖最長邊像素 |
