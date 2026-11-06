# Blender Agent Bridge

**English** ・ [繁體中文](README.zh-TW.md)

Lets Claude Code (or any MCP client / script) operate a **running Blender 3.4 GUI** on your machine:
inspect the scene, run `bpy` Python, transform / select / delete objects, switch the viewport,
take viewport and whole-window screenshots, render stills, undo, and open / save files.
MCP is the main interface, a CLI is the fallback, and a Claude skill is included.

## Architecture

```
 MCP client ──stdio──▶ python -m blender_bridge.mcp_server ─┐
 PowerShell ──CLI────▶ python -m blender_bridge <cmd> ──────┤  blender_bridge/client.py
                                                             │  TCP, one JSON line per connection
                                                             ▼  127.0.0.1:9877 (BLENDER_BRIDGE_PORT)
                     Blender 3.4 GUI: scripts/startup/blender_bridge.py
                     socket thread → queue → bpy.app.timers (main thread)
```

```
blender/
  blender_bridge/            Python package (client, MCP server, CLI, image helpers)
  blender_addon/blender_bridge.py   Blender-side startup script (single source of truth)
  skill/blender-control/     Claude skill
  mcp-examples/              MCP client config examples
  install.py                 one-shot installer (idempotent)
  test_bg.py                 automated test using a background Blender
```

## Install

```powershell
cd blender
python install.py        # system Python 3.10+
```

`install.py` (every step is idempotent):

1. creates `%USERPROFILE%\BlenderAgentBridge\venv`, installs `mcp[cli]` and `pillow`, then `pip install -e .`
   (so **keep this folder where it is** after installing);
2. copies `blender_addon/blender_bridge.py` to `%APPDATA%\Blender Foundation\Blender\<ver>\scripts\startup\`;
3. registers the MCP server `blender` with Claude Code (`claude mcp add --scope user`);
4. copies the skill to `~\.claude\skills\blender-control`.

Flags: `--skip-venv`, `--skip-deps`, `--skip-addon`, `--skip-mcp`, `--skip-skill`, `--blender-version 3.4`.
For other MCP clients, see `mcp-examples\`.

After installing: **restart Blender** (the System Console prints
`[blender_bridge] ... listening on 127.0.0.1:9877`) and start a new MCP-client session.
Without restarting, you can open `blender_bridge.py` in Blender's Text Editor and press Run Script.

## MCP tools

| Tool | Description |
|---|---|
| `blender_ping` | version, background mode, current file, `is_dirty`, pid, port; call first |
| `blender_get_scene_info` | scene overview, collection tree, selection, paged object list |
| `blender_list_objects` | objects with transforms, dimensions, parent, collections, modifiers |
| `blender_eval_python` | run Python on the main thread (`bpy`, `bmesh`, `mathutils`, `math` available; set `result`) |
| `blender_select_objects` / `blender_delete_objects` / `blender_transform_objects` | selection, deletion, absolute or relative transforms |
| `blender_set_view` | front/back/left/right/top/bottom/iso, ortho/persp, shading, zoom extents |
| `blender_viewport_screenshot` / `blender_window_screenshot` | 3D viewport / whole Blender window → image |
| `blender_undo_redo` | undo / redo with before / after object lists |
| `blender_model_file` | open / save / new with safety flags |
| `blender_render` | render a PNG (WORKBENCH / EEVEE / CYCLES) and return it |

Errors: `BLENDER_NOT_AVAILABLE:` (Blender not running or script not installed),
`BLENDER_COMMAND_FAILED:`, `BLENDER_REFUSED:` (missing safety flag).
Environment variables: `BLENDER_BRIDGE_HOST`, `BLENDER_BRIDGE_PORT` (9877), `BLENDER_BRIDGE_TIMEOUT` (120 s),
`BLENDER_BRIDGE_MAX_EDGE` (1280 px).

**Undo semantics:** mutating commands call `bpy.ops.ed.undo_push` *after* the change, so one Ctrl+Z
returns to the state before the command.

**Safety flags:** saving over the current file needs `confirm=true`; overwriting another existing
file needs `overwrite=true`; `open` / `new` with unsaved changes needs `confirm=true`.

## CLI

`python -m blender_bridge <cmd>` (venv python) prints UTF-8 JSON. Sub-commands: `ping`, `commands`,
`scene`, `objects`, `eval`, `select`, `delete`, `transform`, `view`, `screenshot`, `window-shot`,
`undo`, `redo`, `model`, `render`, `raw`. Exit codes: `0` ok, `1` command failed / refused,
`2` cannot connect, `3` screenshot / render failed. See `python -m blender_bridge --help`.

## Tests

```powershell
python test_bg.py --port 9899
```

Starts its own `blender --background`, exercises every command and CLI sub-command, and stops only
the Blender it started (your GUI session is untouched).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `BLENDER_NOT_AVAILABLE` | Blender not running, or script not installed / Blender not restarted |
| `could not bind 127.0.0.1:9877` | another Blender (or test) holds the port; close it or set `BLENDER_BRIDGE_PORT` on both sides |
| screenshot / `set_view` says "--background" | only the GUI Blender can take screenshots |
| command times out | a dialog or modal operator is open; use `blender_window_screenshot` |
| tools not visible in the client | start a new client session |

## Limitations

No mouse / keyboard simulation (screenshots are view-only); screenshots use the first (largest) 3D
viewport; `render` needs a camera and freezes the UI while EEVEE / Cycles render.

Security: the server binds `127.0.0.1` only and has no authentication; `blender_eval_python` runs
arbitrary Python. Use it only with agents you trust.
