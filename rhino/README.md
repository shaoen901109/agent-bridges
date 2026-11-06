# Rhino Agent Bridge

**English** ・ [繁體中文](README.zh-TW.md)

Lets an AI agent (Claude Code or any MCP client) operate **Rhino 7** and **Grasshopper**: see the
viewport, model and edit geometry with RhinoCommon (IronPython), run Rhino commands, and place /
connect / solve / bake Grasshopper components.

- **See:** viewport capture, Grasshopper canvas capture, OS-level whole-window screenshot.
- **Act:** RhinoCommon + rhinoscriptsyntax (`rhino_eval_python`), modeling shortcuts, Rhino command strings, full Grasshopper control (46 MCP tools: `rhino_*`, `gh_*`).
- **Does not:** simulate mouse / keyboard (only `rhino_cancel_command` sends one Esc / Enter to a stuck command).

> **Safety:** the server binds `127.0.0.1` only. Unlike the SketchUp bridge it **starts automatically with
> Rhino** (turn off with `RhinoAgentBridge AutoStart`). There is no tool to close Rhino; overwriting or
> opening files requires `confirm` / `overwrite`.

## Architecture

```
Rhino 7 (.NET Framework 4.8 / IronPython 2.7)
  RhinoAgentBridge.rhp   C# plug-in (AtStartup) + 4 commands: TcpListener on 127.0.0.1,
                         each request → RhinoApp.InvokeOnUiThread → Python handle_line()
  py\*.py                plain-text IronPython scripts, hot-reloadable
                         (protocol, rb_common, rb_core = Rhino commands, rb_gh = Grasshopper commands)
        │  127.0.0.1:9878, newline-delimited JSON {"id":1,"method":"ping","params":{}}
Python package rhino_bridge: client.py (only protocol implementation), launcher.py, window_capture.py,
        images.py ── mcp_server.py (stdio MCP)  /  cli.py
```

Why a C# shell plus IronPython scripts: Rhino only auto-loads real plug-ins (`.rhp`, `LoadMode = AtStartup`);
the `.rhp` is just a .NET dll compiled by `install.ps1` with Roslyn `csc.exe` (or the .NET Framework
`csc.exe` that ships with Windows, C# 5 syntax only), so no Visual Studio is needed. Command logic
lives in hot-reloadable IronPython. All RhinoCommon / Grasshopper calls run on the UI thread.
The plug-in is registered under `HKCU\SOFTWARE\McNeel\Rhinoceros\7.0\Plug-ins\<GUID>` (no settings XML
is modified; an existing key is exported to a `.reg` backup first). `rhino_eval_python` uses **Python 2.7 syntax**.

## Install

Requirements: Windows, Rhino 7 (default `C:\Program Files\Rhino 7`), Python 3.10+, .NET Framework 4.x.

```powershell
cd rhino
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Steps (idempotent): (1) compile `BridgePlugIn.cs` into `%LOCALAPPDATA%\RhinoAgentBridge\plugin\RhinoAgentBridge.rhp` and copy `py\*.py`;
(2) write the registry key; (3) create `%USERPROFILE%\RhinoAgentBridge\venv` and install `agent_client`
(a **copy** - re-run after changing Python code); (4) copy the skill to `~\.claude\skills\`;
(5) register MCP `rhino` with Claude Code. For other MCP clients see `mcp-examples\`.

Parameters: `-SkipBuild`, `-SkipVenv`, `-SkipSkills`, `-SkipClaudeCodeMcp`, `-PipIndex <mirror-url>`
(default official PyPI), `-Python`, `-RhinoDir`. Uninstall: `.\uninstall.ps1 [-RemoveVenv]`.

If Rhino is running, the `.rhp` is locked; close Rhino and re-run to update it. For `py\*.py` changes only,
copy and run `python -m rhino_bridge raw reload_scripts`.

## Use

- Rhino starts → plug-in loads → server starts on the first Idle event (`127.0.0.1:9878`).
- Rhino command line: `RhinoBridgeStart` / `RhinoBridgeStop` / `RhinoBridgeStatus`, and
  `RhinoAgentBridge` (options `Start`, `Stop`, `Status`, `ReloadScripts`, `AutoStart`, `Port`).
- Rhino not running: `rhino_launch` (or `python -m rhino_bridge launch`) starts it and waits; it never closes Rhino.
  If a license / login / update dialog blocks startup, handle it yourself.
- Log: `%LOCALAPPDATA%\RhinoAgentBridge\bridge.log`.

Environment variables: `RHINO_BRIDGE_HOST` (127.0.0.1), `RHINO_BRIDGE_PORT` (9878), `RHINO_BRIDGE_TIMEOUT` (30 s),
`RHINO_BRIDGE_MAX_EDGE` (1280 px), `RHINO_EXE`.

## MCP tools (46)

**Rhino (27):** `rhino_get_info`, `rhino_get_scene_info`, `rhino_list_objects`, `rhino_select_objects`,
`rhino_delete_objects`, `rhino_viewport_screenshot`, `rhino_window_screenshot`, `rhino_set_view`,
`rhino_create_box` / `_sphere` / `_cylinder` / `_circle` / `_polyline` / `_curve`, `rhino_extrude`,
`rhino_boolean`, `rhino_loft`, `rhino_transform_objects`, `rhino_set_layer`, `rhino_set_material`,
`rhino_eval_python`, `rhino_run_command`, `rhino_cancel_command`, `rhino_undo_redo`, `rhino_model_file`, `rhino_launch`.

**Grasshopper (19):** `gh_status`, `gh_open_editor`, `gh_new_document` / `gh_open_document` / `gh_save_document`,
`gh_search_components`, `gh_add_component`, `gh_add_slider` / `gh_add_panel` / `gh_add_toggle`, `gh_set_input`,
`gh_connect` / `gh_disconnect`, `gh_list_document`, `gh_solve`, `gh_get_output`, `gh_bake`, `gh_delete_objects`,
`gh_canvas_screenshot`, `gh_eval_python`.

Notes: lengths use the **document's model units** unless `units` is given; Rhino objects are addressed by
GUID, Grasshopper objects by *instance* GUID; layers use `Parent::Child`; GH components / ports accept
Name, NickName (case-insensitive) or index. Each `*_eval_python` call is a single undo record; assign to `result`.

## CLI

`python -m rhino_bridge <cmd>` prints JSON. Exit codes: `0` ok, `1` command failed, `2` cannot connect,
`3` screenshot failed. Example:

```powershell
$py = "$env:USERPROFILE\RhinoAgentBridge\venv\Scripts\python.exe"
& $py -m rhino_bridge launch
& $py -m rhino_bridge box --width 20 --depth 20 --height 10 --layer Test::A
& $py -m rhino_bridge view --view iso --zoom-extents
```

See `--help`; an agent-oriented guide is in [`AGENT-GUIDE.zh-TW.md`](AGENT-GUIDE.zh-TW.md) (Traditional Chinese).

## Security and limits

- Binds `127.0.0.1` only; no authentication; 16 MB per message, 8 concurrent connections.
- `*_eval_python` runs arbitrary code. Use with agents you trust.
- A request that exceeds its timeout returns `TimeoutError`; later requests meet `BusyError` while the old one runs.
- **Modal dialogs cannot be handled** (license, save prompt, error box): requests time out. Close them yourself;
  `rhino_window_screenshot` shows the dialog.
- `save` needs `confirm`; `save_as` / `export` overwrite needs `overwrite`; `new` / `open` need `confirm`
  and **discard unsaved changes** (ask the user first).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `RHINO_NOT_AVAILABLE` | start Rhino (`rhino_launch`), run `install.ps1`, or `RhinoBridgeStart` |
| tool times out | long computation or a dialog; take `rhino_window_screenshot`, try `rhino_cancel_command` |
| edited `py\*.py` has no effect | `python -m rhino_bridge raw reload_scripts` |
| `.rhp` cannot be overwritten | close Rhino, re-run `install.ps1` |
| restore the registry key | double-click `%LOCALAPPDATA%\RhinoAgentBridge\backup\plugin_key_*.reg` |

## Tests

```powershell
python -m pytest -q --tb=line agent_client\tests
```

A fake TCP server simulates the Rhino side (fragments, large messages, errors, timeouts, disconnects);
CLI exit codes, MCP tool names and the `RHINO_NOT_AVAILABLE` prefix are checked. Live tests need a running Rhino.
