# Claude Code — register the Blender MCP server / 註冊 Blender MCP

`install.py` does this for you. Manual registration / 手動註冊：

```powershell
claude mcp add blender --scope user -- "$env:USERPROFILE\BlenderAgentBridge\venv\Scripts\python.exe" -m blender_bridge.mcp_server
```

> Put the server name **before** `--scope` (otherwise the CLI treats `-m` as its own option).
> 伺服器名稱要放在 `--scope` **之前**，否則 CLI 會把 `-m` 當成自己的選項。

Verify / 確認：`claude mcp get blender` ・ Remove / 移除：`claude mcp remove --scope user blender`

Environment variables (optional, put in `env`) / 環境變數（選填）：

| Variable | Default | Description |
|---|---|---|
| `BLENDER_BRIDGE_HOST` | `127.0.0.1` | Keep local only / 只應維持本機 |
| `BLENDER_BRIDGE_PORT` | `9877` | Must match the Blender-side script / 要和 Blender 端一致 |
| `BLENDER_BRIDGE_TIMEOUT` | `120` | Seconds per command / 單一指令逾時秒數 |
| `BLENDER_BRIDGE_MAX_EDGE` | `1280` | Longest edge of returned images / 回傳圖片最長邊 |
