# -*- coding: utf-8 -*-
"""
install.py — Blender Agent Bridge 一鍵安裝（可重複執行，每一步都是冪等的）。

    python install.py                 # 全部：venv → 依賴 → startup 腳本 → MCP 註冊 → skill
    python install.py --skip-mcp      # 不動 claude mcp 註冊
    python install.py --skip-skill    # 不複製 Claude skill
    python install.py --blender-version 3.4

步驟：
  1. 建立 venv（%USERPROFILE%\\BlenderAgentBridge\\venv），pip install "mcp[cli]" pillow，
     再 pip install -e .（失敗則退回寫 .pth）讓 venv 的 python 能 import blender_bridge。
  2. 把 blender_addon/blender_bridge.py 複製到
     %APPDATA%\\Blender Foundation\\Blender\\<ver>\\scripts\\startup\\blender_bridge.py。
     內容有變動時提醒「重啟 Blender 才會生效」。
  3. claude mcp remove blender -s user（忽略失敗）→ claude mcp add --scope user blender -- <venv python> -m blender_bridge.mcp_server
     → claude mcp get blender 顯示狀態。
  4. 把 skill/blender-control 複製到 ~/.claude/skills/blender-control。
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(os.path.expanduser("~"), "BlenderAgentBridge", "venv")
VENV_PYTHON = os.path.join(VENV_DIR, "Scripts", "python.exe") if os.name == "nt" \
    else os.path.join(VENV_DIR, "bin", "python")
ADDON_SRC = os.path.join(HERE, "blender_addon", "blender_bridge.py")
SKILL_SRC = os.path.join(HERE, "skill", "blender-control")
SKILL_DST = os.path.join(os.path.expanduser("~"), ".claude", "skills", "blender-control")
MCP_NAME = "blender"


def log(msg: str) -> None:
    print(f"[install] {msg}", flush=True)


def run(cmd, check=True, shell=False, **kw) -> subprocess.CompletedProcess:
    shown = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    log(f"$ {shown}")
    return subprocess.run(cmd, check=check, shell=shell, text=True, encoding="utf-8",
                          errors="replace", **kw)


# --- 1. venv ---------------------------------------------------------------

def ensure_venv(skip_deps: bool) -> None:
    if not os.path.exists(VENV_PYTHON):
        log(f"建立 venv：{VENV_DIR}")
        run([sys.executable, "-m", "venv", VENV_DIR])
    else:
        log(f"venv 已存在：{VENV_DIR}")
    if skip_deps:
        return
    run([VENV_PYTHON, "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=False)
    run([VENV_PYTHON, "-m", "pip", "install", "--quiet", "mcp[cli]", "pillow"])
    result = run([VENV_PYTHON, "-m", "pip", "install", "--quiet", "-e", HERE], check=False)
    if result.returncode != 0:
        log("pip install -e . 失敗，改寫 .pth 檔讓 venv 找得到套件")
        site = subprocess.check_output(
            [VENV_PYTHON, "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
            text=True).strip()
        with open(os.path.join(site, "blender_bridge.pth"), "w", encoding="utf-8") as handle:
            handle.write(HERE + "\n")
    check = subprocess.run(
        [VENV_PYTHON, "-c", "import blender_bridge, blender_bridge.mcp_server as m;"
                            "print(blender_bridge.__version__, len(m.mcp._tool_manager._tools) if hasattr(m.mcp,'_tool_manager') else 'ok')"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check.returncode != 0:
        log("venv 匯入 blender_bridge 失敗：\n" + check.stderr)
        sys.exit(1)
    log(f"venv 可匯入 blender_bridge（版本/工具數：{check.stdout.strip()}）")


# --- 2. Blender startup 腳本 -------------------------------------------------

def blender_startup_dirs(version: str | None) -> list[str]:
    base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Blender Foundation", "Blender")
    if version:
        return [os.path.join(base, version, "scripts", "startup")]
    if not os.path.isdir(base):
        return []
    dirs = []
    for name in sorted(os.listdir(base)):
        if name[:1].isdigit() and os.path.isdir(os.path.join(base, name)):
            dirs.append(os.path.join(base, name, "scripts", "startup"))
    return dirs


def install_addon(version: str | None) -> bool:
    """回傳 True 表示有檔案變動（需要重啟 Blender）。"""
    changed = False
    dirs = blender_startup_dirs(version)
    if not dirs:
        log("找不到 Blender 使用者設定目錄（%APPDATA%\\Blender Foundation\\Blender\\<ver>）。"
            "請先啟動過一次 Blender，或用 --blender-version 指定。")
        return False
    for directory in dirs:
        os.makedirs(directory, exist_ok=True)
        dst = os.path.join(directory, "blender_bridge.py")
        if os.path.exists(dst) and filecmp.cmp(ADDON_SRC, dst, shallow=False):
            log(f"startup 腳本已是最新：{dst}")
            continue
        shutil.copyfile(ADDON_SRC, dst)
        log(f"已複製 startup 腳本：{dst}")
        changed = True
    return changed


# --- 3. claude mcp ---------------------------------------------------------

def find_claude() -> str | None:
    for name in ("claude.cmd", "claude.exe", "claude"):
        path = shutil.which(name)
        if path:
            return path
    return None


def register_mcp() -> None:
    claude = find_claude()
    if not claude:
        log("找不到 claude CLI，略過 MCP 註冊。手動註冊指令：")
        log(f'  claude mcp add --scope user {MCP_NAME} -- "{VENV_PYTHON}" -m blender_bridge.mcp_server')
        return
    use_shell = claude.lower().endswith((".cmd", ".bat"))

    def claude_cmd(args: list[str]):
        if use_shell:
            return subprocess.list2cmdline([claude] + args)
        return [claude] + args

    run(claude_cmd(["mcp", "remove", MCP_NAME, "-s", "user"]), check=False, shell=use_shell)
    run(claude_cmd(["mcp", "add", "--scope", "user", MCP_NAME, "--",
                    VENV_PYTHON, "-m", "blender_bridge.mcp_server"]), shell=use_shell)
    run(claude_cmd(["mcp", "get", MCP_NAME]), check=False, shell=use_shell)


# --- 4. skill --------------------------------------------------------------

def install_skill() -> None:
    if not os.path.isdir(SKILL_SRC):
        log(f"沒有 skill 來源目錄，略過：{SKILL_SRC}")
        return
    os.makedirs(os.path.dirname(SKILL_DST), exist_ok=True)
    if os.path.isdir(SKILL_DST):
        shutil.rmtree(SKILL_DST)
    shutil.copytree(SKILL_SRC, SKILL_DST)
    log(f"已安裝 skill：{SKILL_DST}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Blender Agent Bridge 安裝")
    parser.add_argument("--skip-venv", action="store_true", help="不建 venv / 不裝依賴")
    parser.add_argument("--skip-deps", action="store_true", help="建 venv 但不 pip install")
    parser.add_argument("--skip-addon", action="store_true", help="不複製 startup 腳本")
    parser.add_argument("--skip-mcp", action="store_true", help="不執行 claude mcp add")
    parser.add_argument("--skip-skill", action="store_true", help="不複製 skill")
    parser.add_argument("--blender-version", help="例如 3.4（預設自動偵測所有版本）")
    args = parser.parse_args()

    if not args.skip_venv:
        ensure_venv(args.skip_deps)
    changed = False
    if not args.skip_addon:
        changed = install_addon(args.blender_version)
    if not args.skip_mcp:
        register_mcp()
    if not args.skip_skill:
        install_skill()

    print()
    log("完成。")
    if changed:
        log("startup 腳本有更新：請重新啟動 Blender 才會生效（不會自動重啟）。")
    log("新開的 Claude session 才看得到 MCP 工具 blender_*。")
    log(f'CLI 備援：& "{VENV_PYTHON}" -m blender_bridge ping')
    return 0


if __name__ == "__main__":
    sys.exit(main())
