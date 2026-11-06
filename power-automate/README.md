# Power Automate Agent Bridge (pad-bridge)

**English** ・ [繁體中文](README.zh-TW.md)

Lets an AI agent (Claude Code or any MCP client) operate **Power Automate Desktop (PAD)** on your machine:
run desktop flows and read their results, create or edit flows with Robin scripts, and look up action syntax.

## What it can do

| MCP tool | Purpose | How |
|---|---|---|
| `pad_list_flows` | list flows, run state, account | UI Automation reads the console list |
| `pad_run_flow` | run a flow with inputs, wait, return output variables and the failed action | official run URL + local run logs |
| `pad_get_run` / `pad_list_runs` | inspect a run / recent runs | reads `RunDefinition.json` and `Actions.log` |
| `pad_stop_flow` | stop a running flow | console Stop button |
| `pad_get_dialogs` / `pad_dismiss_dialogs` | read / close console error and output dialogs | UI Automation |
| `pad_get_flow_script` | read a flow's Robin script and variables | designer: select all → copy |
| `pad_create_flow` / `pad_update_flow` | create / rewrite flows from Robin, create input / output variables | designer: paste + variable dialog |
| `pad_search_actions` / `pad_action_template` | search the action catalog, get PAD-generated syntax | action pane search → add to scratch flow → copy |

The same functions are available from the CLI: `python -m pad_bridge --help`.

Tested (PAD 2.72, Store version, personal Microsoft account): a free personal account can trigger flows via
the run URL; text / number / boolean inputs and outputs (including special characters) pass correctly;
failures report the failing action, error code and message; script syntax errors are caught from the
designer's error list (with line numbers) before saving.

## Limits

- **Confirmation dialog:** PAD asks for confirmation on every external trigger. Disable "show confirmation when a flow
  is invoked externally" in the PAD console settings, or install with `-AutoConfirm` (the bridge only confirms flows
  it has just triggered, matched by name).
- **It takes over the screen:** create / update / read-script / syntax lookup drive the designer with real keyboard
  and mouse for about 20-60 s; do not touch the PC meanwhile.
- One flow at a time (PAD attended mode). Only the **Main** subflow can be read / written.
- Inputs: text, number, boolean; run URL limit is about 2000 characters (pass file paths for big data).
- UI elements are located mostly by AutomationId; a few buttons by Traditional Chinese name with English fallback;
  a PAD update may require adjustments.
- Must run in your own interactive desktop session (not another Windows account or a remote session).
- First use of `pad_search_actions` / `pad_action_template` creates an empty flow `_pad_bridge_scratch` as a canvas.

## Install

```powershell
cd power-automate
powershell -ExecutionPolicy Bypass -File .\install.ps1
# slow PyPI? add:  -PipIndex https://your-mirror.example/simple
```

The venv is `%USERPROFILE%\PADAgentBridge\venv`; the skill goes to `~/.claude/skills/pad-control`; MCP `pad` is
registered with Claude Code at user scope. For other MCP clients see `mcp-examples\`.
Parameters: `-AutoConfirm`, `-SkipVenv`, `-SkipSkills`, `-SkipClaudeCodeMcp`, `-PipIndex`, `-Python`.

Long-running flows: MCP clients time out single tool calls; use `pad_run_flow(wait=False)` and follow with `pad_get_run`.

## Layout

```
agent_client/pad_bridge/
  uia.py         UI Automation helpers (find, click, keys, clipboard, processes)
  runs.py        read local run logs (files only)
  console.py     console: list flows, run URL, dialogs, stop
  designer.py    designer: read / write scripts, variables, error list, action catalog and templates
  mcp_server.py  MCP shell        cli.py  CLI
skill/pad-control/  workflow and Robin snippets for agents (snippets verified in PAD)
```

Tests (pure logic, no PAD needed): `python -m pytest -q --tb=line agent_client/tests`

Security: no network listener is opened; the bridge acts through your own desktop session. A flow can do
anything you can do, so only give this to agents you trust.
