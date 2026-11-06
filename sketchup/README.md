# SketchUp Agent Bridge

**English** ・ [繁體中文](README.zh-TW.md)

Lets an AI agent (Claude Code or any MCP client) operate **SketchUp 2024**: see the viewport, model
and edit geometry through the Ruby API, and switch tools.

- **See:** viewport screenshot (`view.write_image`) and OS-level whole-window screenshot (toolbars, panels, dialogs).
- **Act:** the full SketchUp Ruby API (`sketchup_eval_ruby`) plus shortcut tools for common modeling.
- **Does not:** simulate mouse or keyboard. The whole-window screenshot is view-only.

> **Safety:** the server inside SketchUp is **off by default**, binds `127.0.0.1` only, and must be
> started by you from *Extensions → Agent Bridge → Start Server* (the menu is localized; in a Chinese
> UI: 延伸程式 → Agent Bridge → 啟動伺服器). An agent cannot start it by itself.

## Architecture

```
SketchUp 2024 (Ruby 3.2)
  Plugins\su_agent_bridge.rb      SketchupExtension loader
  Plugins\su_agent_bridge\        main.rb (menu), server.rb (non-blocking TCP via UI.start_timer),
                                  commands.rb (27 commands, main thread), units.rb, serialize.rb
        │  127.0.0.1:9876, newline-delimited JSON  {"id":1,"method":"ping","params":{}}
Python package sketchup_bridge:  client.py (only protocol implementation), window_capture.py,
        images.py ── mcp_server.py (18 MCP tools)  /  cli.py
```

## Install

```powershell
cd sketchup
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Idempotent; touches only its own files:

1. copies the Ruby extension into every detected `%APPDATA%\SketchUp\SketchUp 20xx\SketchUp\Plugins\`;
2. creates `%USERPROFILE%\SketchUpAgentBridge\venv` and installs `mcp`, `pillow` and `sketchup_bridge`
   (a **copy**, so re-run `install.ps1` after changing Python code);
3. copies the `sketchup-control` skill to `~\.claude\skills\`;
4. registers MCP `sketchup` with Claude Code (`claude mcp add --scope user`) and merges it into
   Claude Desktop's config if detected (a `.bak-*` backup is made first).

Parameters: `-PipIndex <mirror-url>` (default: official PyPI), `-Python <python.exe>`, `-PluginsDir <path>`,
`-SkipVenv`, `-SkipSkills`, `-SkipClaudeCodeMcp`, `-SkipDesktopMcp`. Uninstall:
`.\uninstall.ps1 [-RemoveVenv]`. Other MCP clients: see `mcp-examples\` (launch command:
`%USERPROFILE%\SketchUpAgentBridge\venv\Scripts\python.exe -m sketchup_bridge.mcp_server`).

Then restart SketchUp and start the server from the menu. Menu items: Start Server, Stop Server,
Status, Set Port (default 9876).

## MCP tools (18)

`sketchup_get_info` (ping + version, model, units), `sketchup_get_scene_info`, `sketchup_list_entities`,
`sketchup_select_entities`, `sketchup_delete_entities`, `sketchup_viewport_screenshot`,
`sketchup_window_screenshot`, `sketchup_get_camera`, `sketchup_set_camera`, `sketchup_send_action`,
`sketchup_create_box`, `sketchup_create_face`, `sketchup_push_pull`, `sketchup_transform_entities`,
`sketchup_set_material`, `sketchup_eval_ruby`, `sketchup_undo_redo`, `sketchup_model_file`.

Units default to **millimetres** (`units`: `mm` / `cm` / `m` / `in` / `ft`). Every mutating command
is one `start_operation` / `commit_operation`, so one Ctrl+Z reverts it.

Environment variables: `SKETCHUP_BRIDGE_HOST` (127.0.0.1), `SKETCHUP_BRIDGE_PORT` (9876),
`SKETCHUP_BRIDGE_TIMEOUT` (30 s), `SKETCHUP_BRIDGE_MAX_EDGE` (1280 px).

## CLI

```powershell
$SK = "$env:USERPROFILE\SketchUpAgentBridge\venv\Scripts\python.exe"
& $SK -m sketchup_bridge ping
& $SK -m sketchup_bridge box --width 3000 --depth 2000 --height 2800 --name "mass"
& $SK -m sketchup_bridge set-camera --view iso --zoom-extents
& $SK -m sketchup_bridge screenshot --out C:\temp\view.png --max-edge 1280
```

Exit codes: `0` ok, `1` command failed, `2` cannot connect, `3` screenshot failed. All output is JSON.
For agents without MCP, see [`AGENT-GUIDE.zh-TW.md`](AGENT-GUIDE.zh-TW.md) (Traditional Chinese).

## Security

| Item | Design |
|---|---|
| Off by default | the server never starts automatically |
| localhost only | `TCPServer.new('127.0.0.1', port)` |
| No authentication | anyone who can run programs on this machine can connect; never bind `0.0.0.0` or port-forward |
| `eval_ruby` | runs arbitrary Ruby (files, processes, network). Stop the server when you are done |
| Limits | max 8 connections, 8 MB per request |
| File safety | saving over the current file needs `confirm=true`, overwriting needs `overwrite=true`, `new` / `open` need `confirm=true` |

## Troubleshooting

| Symptom | Fix |
|---|---|
| cannot connect | SketchUp not running, or the server was not started from the menu |
| no "Agent Bridge" menu | re-run `install.ps1`, restart SketchUp, check the Extension Manager |
| port in use | `netstat -ano \| findstr :9876`, or change the port in the menu (and `SKETCHUP_BRIDGE_PORT`) |
| commands time out | a dialog may be blocking SketchUp; take a window screenshot |
| black screenshot | `PrintWindow` failed for a GPU-composited window; the tool falls back to `ImageGrab` (window must be visible) |
| `.ps1` syntax errors | the scripts must stay **UTF-8 with BOM** for Windows PowerShell 5.1 |

## Tests

```powershell
python -m pytest -q --tb=line agent_client\tests
```

Uses a fake TCP server (round trips, fragmented and 3 MB messages, timeouts, refused connections,
bad JSON, error responses, CLI parsing).
