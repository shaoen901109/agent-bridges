# Claude Code 註冊 pad MCP

install.ps1 會自動做。手動註冊：

```powershell
claude mcp add pad --scope user -- "$env:USERPROFILE\PADAgentBridge\venv\Scripts\python.exe" -m pad_bridge.mcp_server
```

要讓 bridge 替自己觸發的流程自動按 PAD 的確認框：

```powershell
claude mcp add pad --scope user -e PAD_BRIDGE_AUTO_CONFIRM=1 -- "$env:USERPROFILE\PADAgentBridge\venv\Scripts\python.exe" -m pad_bridge.mcp_server
```

長時間流程：Claude Code 對單一 MCP 工具呼叫有逾時，跑很久的流程請用 `pad_run_flow(wait=False)`
再以 `pad_get_run` 追蹤。
