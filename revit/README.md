# Revit Agent Bridge

**English** ・ [繁體中文](README.zh-TW.md)

Lets an AI agent (Claude Code or any MCP client) operate **Revit 2024**: see the active view, query
elements and parameters, create walls / floors / levels, place doors, windows and families, edit
parameters, and run C# snippets against the full Revit API for everything else.

- **See:** view export (`ExportImage`, active view), OS-level whole-window screenshot (ribbon, Project Browser, dialogs).
- **Act:** modeling shortcuts, parameter read / write, C# snippets (`revit_eval_csharp`, full Revit API) - 18 MCP tools: `revit_*`.
- **Does not:** simulate mouse / keyboard, dismiss dialogs automatically, or close Revit (there is no such tool).
  The whole-window screenshot is for viewing only.

> **Safety:** the server binds `127.0.0.1` only and drops any connection whose first message is not JSON
> (for example an HTTP request sent by a web page to the local port). It **starts automatically with Revit**
> (set `REVIT_BRIDGE_AUTOSTART=0` to turn this off; the ribbon tab "Agent Bridge" starts / stops it manually).
> Overwriting files, creating new models and opening files require `confirm` / `overwrite`.

> **Verified:** on Revit 2024.0.4 (Traditional Chinese default template), all 18 tools passed on a live
> instance (see "Verification status" below).

## Architecture

```
Revit 2024 (.NET Framework 4.8)
  RevitAgentBridge.dll   C# add-in (IExternalApplication), compiled with csc, C# 5 syntax only
    App           ribbon tab "Agent Bridge", start / stop / status, DialogBoxShowing logging
    BridgeServer  TcpListener on 127.0.0.1; accept thread + one reader thread per connection
    ApiBridge     request queue + ExternalEvent: every Revit API call runs on the Revit API thread
    Handlers      queries / modeling / views / screenshots / files; Evaluator runs eval_csharp
        │  127.0.0.1:9879, newline-delimited JSON {"id":1,"method":"ping","params":{}}
        │  (default ports in this repo: 9876 SketchUp, 9877 Blender, 9878 Rhino, 9879 Revit)
Python package revit_bridge: client.py (only protocol implementation), launcher.py, window_capture.py,
        images.py ── mcp_server.py (stdio MCP, 18 tools)  /  cli.py
```

Key design points:

- **Revit API only works on Revit's API thread.** Requests are queued by a background thread and drained by
  an `IExternalEventHandler`. When Revit is busy the event does not fire immediately, so it is re-raised
  every 500 ms until the request starts.
- **Timeouts neither hang nor execute later.** Each request is Pending / Started / Abandoned; a request that
  timed out before starting is marked abandoned and skipped.
- **Modal dialogs block the API thread.** `ping`, `get_status` and `list_commands` never enter the API thread;
  `get_info` falls back to a cached answer (`stale=true`) after 4 s. The add-in only *logs* `DialogBoxShowing`
  and **never dismisses a dialog**.
- **No Visual Studio needed.** `build.ps1` uses the Roslyn `csc` from VS Build Tools if present, otherwise the
  .NET Framework `csc` that ships with Windows (`/langversion:5`).
- **Safe edits.** Every modifying command is one Transaction with a warning swallower: warnings are deleted
  and returned in `warnings`; an error rolls the whole transaction back.
- **`eval_csharp`:** the snippet is wrapped into `AgentEval.Snippet.Run(UIApplication uiapp, UIDocument uidoc,
  Document doc, Action<object> print)` and compiled in memory with CodeDom (C# 5). Compile-error line numbers
  are mapped back to snippet lines; leading `using` lines are hoisted.
- **Units:** the protocol, MCP tools and CLI use **millimeters**. Inside `eval_csharp` you are in Revit internal
  units (**feet**, 1 ft = 304.8 mm).

The add-in is registered through a per-user manifest `%APPDATA%\Autodesk\Revit\Addins\2024\RevitAgentBridge.addin`
(no administrator rights; the DLL lives in `%APPDATA%\RevitAgentBridge\addin\`). On first load Revit asks whether
to trust the add-in; choose **Always Load**. Uninstall removes only that manifest and its own folders.

## Install

Requirements: Windows, Revit 2024 (default `C:\Program Files\Autodesk\Revit 2024`), Python 3.10+,
.NET Framework 4.x. **Close Revit first** (it locks the DLL).

```powershell
cd revit
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Steps (idempotent): (1) compile `RevitAgentBridge.dll` into `%APPDATA%\RevitAgentBridge\addin\`;
(2) write `RevitAgentBridge.addin` (an existing one is backed up first); (3) create
`%USERPROFILE%\RevitAgentBridge\venv` and install `agent_client` (a **copy** - re-run after changing Python code);
(4) copy the skill to `~\.claude\skills\`; (5) register MCP `revit` with Claude Code.
For other MCP clients see `mcp-examples\`.

Parameters: `-SkipBuild`, `-SkipVenv`, `-SkipSkills`, `-SkipClaudeCodeMcp`, `-PipIndex <mirror-url>`
(default official PyPI), `-Python`, `-RevitDir`. Uninstall: `.\uninstall.ps1 [-RemoveVenv]`.

After installing, start Revit and choose "Always Load"; the ribbon tab **Agent Bridge** confirms success.
Log: `%LOCALAPPDATA%\RevitAgentBridge\bridge.log`.

## Use

- Revit starts → add-in loads → server starts on `127.0.0.1:9879`.
- Revit not running: `revit_launch` (or `python -m revit_bridge launch`) starts it and waits for the bridge
  (about 40 s in testing); it never closes Revit.
- Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `REVIT_BRIDGE_PORT` | `9879` | shared by the Revit side and the client |
| `REVIT_BRIDGE_HOST` | `127.0.0.1` | client side; the server always binds localhost |
| `REVIT_BRIDGE_TIMEOUT` | `30` | per-command timeout in seconds |
| `REVIT_BRIDGE_MAX_EDGE` | `1280` | longest edge (px) of screenshots returned to the model |
| `REVIT_BRIDGE_AUTOSTART` | `1` | Revit side: `0` = do not start the server on launch |
| `REVIT_EXE` | default install path | Revit.exe used by `revit_launch` |

## MCP tools (18)

| Category | Tools |
|---|---|
| Status | `revit_get_info`, `revit_get_scene_info`, `revit_launch` |
| Query | `revit_list_elements`, `revit_get_element` |
| Modeling | `revit_create_wall`, `revit_create_floor`, `revit_create_level`, `revit_place_family_instance` |
| Modify | `revit_set_parameter`, `revit_delete_elements`, `revit_select_elements` |
| Views and screenshots | `revit_set_view`, `revit_view_screenshot`, `revit_window_screenshot` |
| Arbitrary C# | `revit_eval_csharp` |
| Undo and files | `revit_undo`, `revit_model_file` |

Error prefixes: `REVIT_NOT_AVAILABLE` (cannot connect), `REVIT_COMMAND_FAILED` (Revit reported a failure),
`REVIT_WINDOW_NOT_FOUND` (window screenshot found no window).

Notes: external lengths are in **millimeters**; inside `revit_eval_csharp` use **feet**. Screenshots use
`ExportImage` on the **active view**; if a view is rendered with DirectX / hardware graphics the export can
differ from what you see on screen, so use `revit_window_screenshot` to see the actual screen.

## CLI

`python -m revit_bridge <cmd>` prints JSON. Exit codes: `0` ok, `1` command failed, `2` cannot connect,
`3` screenshot failed. See `--help`; an agent-oriented guide is in [`AGENT-GUIDE.zh-TW.md`](AGENT-GUIDE.zh-TW.md)
(Traditional Chinese).

## Protocol

Newline-delimited JSON, one message per line:

```json
{"id": 1, "method": "create_wall", "params": {"start": [0,0], "end": [5000,0], "height": 3000}, "timeout": 29}
{"id": 1, "ok": true, "result": {"id": 312456, "length_mm": 5000.0}}
{"id": 1, "ok": false, "error": {"message": "...", "class": "BridgeError", "backtrace": ["..."]}}
```

## Security and limits

- Binds `127.0.0.1` only; no authentication. A connection that sends non-JSON data (for example a browser
  HTTP request) is closed immediately and nothing is executed.
- `revit_eval_csharp` runs arbitrary code with full Revit API access. Use with agents you trust.
- A request that exceeds its timeout is abandoned if it has not started yet, so it cannot run later.
- **Modal dialogs cannot be handled:** while Revit shows one, API requests time out. Close it yourself;
  `revit_window_screenshot` shows its content.
- Overwriting, `new` and `open` require explicit confirmation flags; there is no tool that closes Revit.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `install.ps1` says Revit is running | save and close Revit, re-run |
| no "Agent Bridge" tab after starting Revit | check `%LOCALAPPDATA%\RevitAgentBridge\bridge.log`; make sure the `<Assembly>` path in the `.addin` exists; if you chose "Do not load" at the first prompt, delete the `.addin` file, re-run `install.ps1` and restart Revit to be asked again |
| `REVIT_NOT_AVAILABLE` | Revit is not running → `revit_launch`; or press "Start server" on the ribbon tab |
| request times out | Revit is in a dialog or busy; take `revit_window_screenshot` and handle the dialog |
| compile error in `build.ps1` | point `-RevitDir` at the folder that contains `RevitAPI.dll`; source is C# 5 only |

## Verification status

Verified without Revit: `build.ps1` compiles (Roslyn and Framework C# 5 `csc`); the `eval_csharp` wrapper and
line mapping (`build.ps1 -RunTests`, 23 checks); Python side with a fake TCP server
(`python -m pytest -q --tb=line agent_client\tests`: client, CLI exit codes, MCP tool names, error prefixes).

**Verified live** (Revit 2024.0.4): `revit_launch` to server-ready in about 40 s; `new` model with the default
template; 4 walls, a floor, a level (with plan view), a door and a window hosted in walls; `get_element` /
`set_parameter`; `select`, `delete`, `undo` (asynchronous via PostCommand), `save`; `set_view`, `ExportImage`,
whole-window screenshot; `eval_csharp` (return values, print, compile-error line numbers, runtime exception
traces, compile cache); MCP stdio (18 tools, images as image content); `open` of an existing file; browser-style
HTTP requests are dropped without executing.

**Not verified live:** `save_as` overwrite; the timeout message and `DialogBoxShowing` log while Revit sits in a dialog.

## Tests

```powershell
python -m pytest -q --tb=line agent_client\tests
```

A fake TCP server simulates the Revit side; live tests need a running Revit 2024.
