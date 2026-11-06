<#
.SYNOPSIS
    安裝「SketchUp 代理操控插件」（Agent Bridge）。

.DESCRIPTION
    每一步都可重複執行，且只會碰自己的檔案：
      1. 把 Ruby 擴充功能複製到 SketchUp 的使用者 Plugins 資料夾
         （自動偵測已安裝的各版本；也可用 -PluginsDir 指定）
      2. 建立（或更新）獨立的 Python venv 並安裝 sketchup_bridge
         （自動尋找 Python 3.10+；找不到會告訴你怎麼裝）
      3. 把 sketchup-control skill 複製到 Claude Code 的 skills 資料夾
      4. 註冊 MCP：Claude Code（需有 claude 指令）與 Claude Desktop（偵測到才做）
    絕對不會刪除或修改 Plugins 資料夾內其他插件。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1

.EXAMPLE
    # 網路對 PyPI 很慢時指定鏡像
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -PipIndex https://mirror.example/simple
#>
[CmdletBinding()]
param(
    # SketchUp Plugins 資料夾。留空 = 自動偵測所有已安裝的 SketchUp 版本
    [string] $PluginsDir = '',

    # Python venv 位置。刻意放在純 ASCII 路徑，避免中文路徑在 agent 啟動時出問題
    [string] $VenvDir = (Join-Path $env:USERPROFILE 'SketchUpAgentBridge\venv'),

    # 用來建立 venv 的 Python。留空 = 自動尋找 3.10 以上
    [string] $Python = '',

    # pip 套件來源（預設官方 PyPI；網路慢時可改成鏡像）
    [string] $PipIndex = 'https://pypi.org/simple',

    # skill / agent 設定的家目錄（測試用，一般不用改）
    [string] $HomeDir = $env:USERPROFILE,

    # Claude Desktop 設定檔位置
    [string] $DesktopConfig = (Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'),

    [switch] $SkipVenv,
    [switch] $SkipSkills,
    [switch] $SkipClaudeCodeMcp,
    [switch] $SkipDesktopMcp
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition

$PluginLoaderName = 'su_agent_bridge.rb'
$PluginFolderName = 'su_agent_bridge'
$SkillName        = 'sketchup-control'
$McpName          = 'sketchup'

function Write-Step  ([string] $Text) { Write-Host "`n=== $Text ===" -ForegroundColor Cyan }
function Write-Ok    ([string] $Text) { Write-Host "  [OK]   $Text" -ForegroundColor Green }
function Write-Warn2 ([string] $Text) { Write-Host "  [警告] $Text" -ForegroundColor Yellow }
function Write-Info  ([string] $Text) { Write-Host "  $Text" }

# 試跑一個 Python，回傳 @{Path; Ver}（Ver = 主*100+次，例如 312）；不可用則回傳 $null
function Get-PythonInfo {
    param([string] $Exe, [string[]] $PreArgs = @())
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $null }
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        $out = & $Exe @PreArgs -c "import sys, venv; print(sys.executable); print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
        $code = $LASTEXITCODE
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $old
    }
    $lines = @($out)
    if ($code -ne 0 -or $lines.Count -lt 2) { return $null }
    $ver = 0
    if (-not [int]::TryParse([string]$lines[1], [ref] $ver)) { return $null }
    return [pscustomobject]@{ Path = [string]$lines[0]; Ver = $ver }
}

function Find-Python {
    param([string] $Preferred)
    if ($Preferred) {
        $info = Get-PythonInfo -Exe $Preferred
        if ($info) { return $info }
        throw "指定的 Python 無法執行：$Preferred"
    }
    $tries = @(
        @{ Exe = 'py';      Pre = @('-3') },
        @{ Exe = 'python';  Pre = @() },
        @{ Exe = 'python3'; Pre = @() }
    )
    foreach ($t in $tries) {
        $info = Get-PythonInfo -Exe $t.Exe -PreArgs $t.Pre
        if ($info -and $info.Ver -ge 310) { return $info }
    }
    $dirs = @()
    if ($env:LOCALAPPDATA) {
        $dirs += Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue
    }
    $dirs += Get-ChildItem 'C:\Program Files' -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue
    foreach ($d in ($dirs | Sort-Object Name -Descending)) {
        $exe = Join-Path $d.FullName 'python.exe'
        if (Test-Path $exe) {
            $info = Get-PythonInfo -Exe $exe
            if ($info -and $info.Ver -ge 310) { return $info }
        }
    }
    return $null
}

Write-Host '安裝 SketchUp 代理操控插件' -ForegroundColor White
Write-Info "來源目錄：$ProjectRoot"

# ---------------------------------------------------------------------------
# 1. SketchUp 擴充功能
# ---------------------------------------------------------------------------
Write-Step '1/4 安裝 SketchUp 擴充功能'

$sourcePluginDir = Join-Path $ProjectRoot 'sketchup_plugin'
$sourceLoader    = Join-Path $sourcePluginDir $PluginLoaderName
$sourceFolder    = Join-Path $sourcePluginDir $PluginFolderName
if (-not (Test-Path $sourceLoader)) { throw "找不到載入器：$sourceLoader" }
if (-not (Test-Path $sourceFolder)) { throw "找不到程式資料夾：$sourceFolder" }

$pluginDirs = @()
if ($PluginsDir) {
    $pluginDirs = @($PluginsDir)
} else {
    $root = Join-Path $env:APPDATA 'SketchUp'
    if (Test-Path $root) {
        foreach ($d in (Get-ChildItem $root -Directory -Filter 'SketchUp 20*' | Sort-Object Name)) {
            $p = Join-Path $d.FullName 'SketchUp\Plugins'
            if (Test-Path $p) { $pluginDirs += $p }
        }
    }
    if ($pluginDirs.Count -eq 0) {
        $fallback = Join-Path $root 'SketchUp 2024\SketchUp\Plugins'
        Write-Warn2 '沒有偵測到 SketchUp 的使用者資料夾（SketchUp 還沒裝或沒開過？）。'
        Write-Warn2 "先建立 2024 版的 Plugins 資料夾：$fallback"
        $pluginDirs = @($fallback)
    }
}

foreach ($dir in $pluginDirs) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $targetFolder = Join-Path $dir $PluginFolderName
    $targetLoader = Join-Path $dir $PluginLoaderName
    if (Test-Path $targetFolder) { Remove-Item -Recurse -Force $targetFolder }   # 只刪自己的資料夾
    Copy-Item -Path $sourceFolder -Destination $targetFolder -Recurse -Force
    Copy-Item -Path $sourceLoader -Destination $targetLoader -Force
    Write-Ok "已安裝到 $dir"
    if ($dir -notmatch 'SketchUp 2024') {
        Write-Warn2 '這個插件在 SketchUp 2024 上測試過；其他版本理論上可用，但未經驗證。'
    }
}
Write-Info '（Plugins 內其他插件未被更動）'

# ---------------------------------------------------------------------------
# 2. Python venv
# ---------------------------------------------------------------------------
$venvPython = Join-Path $VenvDir 'Scripts\python.exe'

if ($SkipVenv) {
    Write-Step '2/4 Python venv（已略過）'
} else {
    Write-Step '2/4 建立 / 更新 Python venv'

    $py = Find-Python -Preferred $Python
    if (-not $py) {
        Write-Host ''
        Write-Host '  找不到 Python 3.10 以上的版本。請先安裝 Python：' -ForegroundColor Red
        Write-Host '    https://www.python.org/downloads/  （安裝時勾選 "Add python.exe to PATH"）' -ForegroundColor Red
        Write-Host '  或在 PowerShell 執行：winget install Python.Python.3.12' -ForegroundColor Red
        Write-Host '  裝好後重新執行本安裝程式。' -ForegroundColor Red
        throw '缺少 Python 3.10+。'
    }
    Write-Info ("使用 Python {0}.{1}：{2}" -f [math]::Floor($py.Ver / 100), ($py.Ver % 100), $py.Path)

    if (-not (Test-Path $venvPython)) {
        Write-Info "建立 venv：$VenvDir"
        & $py.Path -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw 'venv 建立失敗。' }
    } else {
        Write-Info "venv 已存在，更新套件：$VenvDir"
    }

    Write-Info '安裝 sketchup_bridge 與相依套件（mcp、pillow），第一次可能要一兩分鐘…'
    $pipArgs = @('-m', 'pip', 'install', '--upgrade', '--quiet', '--disable-pip-version-check')
    if ($PipIndex) { $pipArgs += @('--index-url', $PipIndex) }
    $pipArgs += (Join-Path $ProjectRoot 'agent_client')
    & $venvPython @pipArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'pip install 失敗。若是網路太慢或連不上，可加上 -PipIndex 指定鏡像後重跑。'
    }

    & $venvPython -c "import sketchup_bridge, mcp, PIL; print('sketchup_bridge', sketchup_bridge.__version__)"
    if ($LASTEXITCODE -ne 0) { throw '安裝後匯入測試失敗。' }
    Write-Ok "venv 就緒：$venvPython"

    # setuptools 會在原始碼資料夾留下 build\ 與 *.egg-info\，清掉
    foreach ($leftover in @('agent_client\build', 'agent_client\sketchup_bridge.egg-info')) {
        $path = Join-Path $ProjectRoot $leftover
        if (Test-Path $path) { Remove-Item -Recurse -Force $path }
    }
}

# ---------------------------------------------------------------------------
# 3. skill
# ---------------------------------------------------------------------------
$skillSource = Join-Path $ProjectRoot "skill\$SkillName"
$skillTargets = [ordered]@{
    'Claude Code' = Join-Path $HomeDir ".claude\skills\$SkillName"
}

if ($SkipSkills) {
    Write-Step '3/4 安裝 skill（已略過）'
} else {
    Write-Step '3/4 安裝 sketchup-control skill'
    if (-not (Test-Path $skillSource)) { throw "找不到 skill 來源：$skillSource" }
    foreach ($entry in $skillTargets.GetEnumerator()) {
        $target = $entry.Value
        $parent = Split-Path -Parent $target
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        if (Test-Path $target) { Remove-Item -Recurse -Force $target }   # 只動自己的資料夾
        Copy-Item -Path $skillSource -Destination $target -Recurse -Force
        Write-Ok "$($entry.Key) -> $target"
    }
    Write-Info '（Claude Desktop / claude.ai 的 skill 要在介面手動上傳，見 README）'
}

# ---------------------------------------------------------------------------
# 4. MCP 註冊
# ---------------------------------------------------------------------------
Write-Step '4/4 註冊 MCP'
$mcpArgs = @('-m', 'sketchup_bridge.mcp_server')

# Claude Code
if ($SkipClaudeCodeMcp) {
    Write-Info 'Claude Code：已略過'
} elseif (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Info 'Claude Code：找不到 claude 指令，略過。（沒用 Claude Code 可忽略；用的話請手動執行：）'
    Write-Host "    claude mcp add $McpName --scope user -- `"$venvPython`" -m sketchup_bridge.mcp_server" -ForegroundColor White
} else {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & claude mcp remove $McpName --scope user *> $null     # 先移除舊的（不存在會失敗，忽略）
    & claude mcp add $McpName --scope user -- $venvPython @mcpArgs
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -eq 0) {
        Write-Ok 'Claude Code：已註冊 MCP「sketchup」（使用者層級，所有專案可用）'
    } else {
        Write-Warn2 'Claude Code：claude mcp add 失敗，請手動執行：'
        Write-Host "    claude mcp add $McpName --scope user -- `"$venvPython`" -m sketchup_bridge.mcp_server" -ForegroundColor White
    }
}

# Claude Desktop
function Show-DesktopManual {
    Write-Host '    在 claude_desktop_config.json 的 mcpServers 內加入：' -ForegroundColor White
    $snippet = @{ command = $venvPython; args = $mcpArgs } | ConvertTo-Json -Depth 5
    Write-Host "    `"$McpName`": $snippet" -ForegroundColor White
}

$desktopDir = Split-Path -Parent $DesktopConfig
if ($SkipDesktopMcp) {
    Write-Info 'Claude Desktop：已略過'
} elseif (-not (Test-Path $desktopDir)) {
    Write-Info 'Claude Desktop：沒偵測到，略過。'
} else {
    try {
        $cfg = $null
        if ((Test-Path $DesktopConfig) -and ((Get-Item $DesktopConfig).Length -gt 0)) {
            $cfg = Get-Content -Raw -Encoding UTF8 $DesktopConfig | ConvertFrom-Json
            $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
            Copy-Item $DesktopConfig "$DesktopConfig.bak-$stamp"
        }
        if ($null -eq $cfg) { $cfg = [pscustomobject]@{} }
        if (-not ($cfg.PSObject.Properties.Name -contains 'mcpServers')) {
            $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
        }
        $entry = [pscustomobject]@{ command = $venvPython; args = $mcpArgs }
        $cfg.mcpServers | Add-Member -NotePropertyName $McpName -NotePropertyValue $entry -Force
        $json = $cfg | ConvertTo-Json -Depth 20
        [System.IO.File]::WriteAllText($DesktopConfig, $json, (New-Object System.Text.UTF8Encoding($false)))
        Write-Ok "Claude Desktop：已寫入 $DesktopConfig（原檔已備份為 .bak-*；需完全結束並重開 Claude Desktop）"
    } catch {
        Write-Warn2 "Claude Desktop：自動寫入失敗（$($_.Exception.Message)），請手動設定："
        Show-DesktopManual
    }
}

Write-Info "其他 MCP 客戶端：啟動指令是 `"$venvPython`" -m sketchup_bridge.mcp_server ，格式範例見 mcp-examples\"

Write-Host ''
Write-Host '安裝完成。' -ForegroundColor Green
Write-Host '下一步：' -ForegroundColor Yellow
Write-Host '  1. 重新啟動 SketchUp，並重開 Claude Code / Claude Desktop（或你的 MCP 客戶端）' -ForegroundColor Yellow
Write-Host '  2. SketchUp：延伸程式(Extensions) → Agent Bridge → 啟動伺服器' -ForegroundColor Yellow
Write-Host '  （伺服器預設關閉，必須由你手動啟動，AI agent 無法自行開啟。）' -ForegroundColor Yellow
