# agent-bridges

**English** ・ [繁體中文](README.zh-TW.md)

Local MCP bridges that let an AI agent (Claude Code or any MCP client) operate desktop software
running on your own Windows machine: take screenshots, inspect the document, run code inside the
application, model geometry, and verify the result visually.

| Bridge | Software | Local port | MCP server name | Notes |
|---|---|---|---|---|
| [`blender/`](blender/README.md) | Blender 3.4 | `127.0.0.1:9877` | `blender` | startup script inside Blender; runs `bpy` on the main thread |
| [`rhino/`](rhino/README.md) | Rhino 7 + Grasshopper | `127.0.0.1:9878` | `rhino` | C# `.rhp` plug-in + IronPython scripts; 46 tools (`rhino_*`, `gh_*`) |
| [`sketchup/`](sketchup/README.md) | SketchUp 2024 | `127.0.0.1:9876` | `sketchup` | Ruby extension; server is **off by default**, you start it from the menu |
| [`power-automate/`](power-automate/README.md) | Power Automate Desktop (PAD) | none (UI Automation + local run logs) | `pad` | run desktop flows, read / write Robin scripts |

## Architecture

```
 AI agent (MCP client)                       other tools / scripts
        │  stdio (MCP)                               │  CLI
        ▼                                            ▼
 ┌──────────────────────────────────────────────────────────┐
 │  Python package  <name>_bridge   (venv in %USERPROFILE%) │
 │   mcp_server.py   cli.py   ──►   client.py (protocol)    │
 └───────────────────────────────┬──────────────────────────┘
                                 │ TCP, newline-delimited JSON, 127.0.0.1 only
                                 ▼
 ┌──────────────────────────────────────────────────────────┐
 │  Server running inside the application (plug-in/script)  │
 │  requests are queued and executed on the UI/main thread  │
 └──────────────────────────────────────────────────────────┘
```

Power Automate Desktop has no scripting socket, so `pad-bridge` drives PAD through Windows UI
Automation and reads PAD's local run logs instead.

Each bridge ships three things: a **Python MCP server + CLI**, the **in-application server**, and a
**Claude skill** (`skill/<name>-control/`) with workflow rules and code snippets.

## Quick start

Requirements: Windows 10/11, Python 3.10+, the target application (see table).

```powershell
# Rhino / SketchUp / Power Automate (PowerShell)
cd rhino            # or sketchup, power-automate
powershell -ExecutionPolicy Bypass -File .\install.ps1
# slow PyPI? add:  -PipIndex https://your-mirror.example/simple

# Blender
cd blender
python install.py
```

The installers register the MCP server with **Claude Code** (`claude mcp add --scope user ...`) and
copy the skill to `~\.claude\skills\`. For other MCP clients, see each bridge's `mcp-examples\`
folder (`mcp-config.json` is a generic `mcpServers` example). Restart the application and your MCP
client afterwards. Details, tool lists and troubleshooting are in each bridge's README.

## Security design

- Servers bind to **`127.0.0.1` only**; nothing is exposed to the LAN or internet. There is no
  authentication, so anyone who can run code on your machine can connect. **Do not** change the
  bind address or port-forward these ports.
- Tools that run arbitrary code (`*_eval_python`, `sketchup_eval_ruby`, `gh_eval_python`) give the
  agent full control of the application (and, through the language runtime, of your user account).
  Enable the bridge only for agents you trust.
- SketchUp's server is off by default and must be started by you from the menu.
- Destructive file actions (overwrite, open/new with unsaved changes) require explicit
  `confirm` / `overwrite` flags. No bridge can close or kill the host application.
- Model edits are wrapped in undo records where the host application supports it.

## Repository layout

```
blender/  rhino/  sketchup/  power-automate/
  README.md, README.zh-TW.md     per-bridge docs
  install.(ps1|py), uninstall.ps1
  skill/                         Claude skill (SKILL.md + references)
  mcp-examples/                  config examples for MCP clients
  agent_client/ or <name>_bridge/   Python package (MCP server, CLI, client)
  tests/                         unit tests (fake TCP server)
```

## Credits

| Purpose | Component | License |
|---|---|---|
| MCP server framework | [Model Context Protocol](https://modelcontextprotocol.io/) Python SDK (`mcp`) | MIT |
| Screenshot handling (optional) | [Pillow](https://python-pillow.org/) | HPND |
| Windows UI automation (power-automate bridge) | [uiautomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) | Apache-2.0 |
| Tests | [pytest](https://pytest.org/) | MIT |
| Controlled applications (not included) | Blender, Rhino / Grasshopper (McNeel), SketchUp (Trimble), Power Automate Desktop (Microsoft) | see each vendor |

Developed with the help of [Claude Code](https://claude.com/claude-code) (Anthropic).

## Author

- Website: https://shaoenstudio.com
- Email: shaoenstudio@gmail.com
- GitHub: [@shaoen901109](https://github.com/shaoen901109)

## License

[MIT](LICENSE)
