<#
.SYNOPSIS
    安裝「Revit 代理操控插件」（Revit 2024 Agent Bridge）。

.DESCRIPTION
    每一步都可重複執行，且只會碰自己的檔案。安裝時 Revit 必須是關閉的（DLL 會被 Revit 鎖住）：
      1. 編譯 C# 外掛 RevitAgentBridge.dll（.NET Framework 4.8，不需要 Visual Studio，用 Build Tools 或系統內建 csc），
         複製到 %APPDATA%\RevitAgentBridge\addin\
      2. 在 %APPDATA%\Autodesk\Revit\Addins\2024\ 寫入 RevitAgentBridge.addin（固定 GUID）。
         Revit 下次啟動會問一次「是否載入此外掛」，請選「永遠載入」。之後自動載入、自動開伺服器。
         （若該 .addin 已存在，會先備份成 .bak-<時間>；解除安裝時只刪這一個檔案。）
      3. 建立（或更新）獨立的 Python venv 並安裝 revit_bridge（pip 來源預設官方 PyPI，可用 -PipIndex 改成鏡像）
      4. （選用）把 revit-control skill 複製到 Claude Code 的 skills 資料夾；來源資料夾不存在就略過
      5. 註冊 MCP（名稱「revit」）到 Claude Code（claude mcp add）。
         其他 MCP 客戶端請參考 mcp-examples\ 手動設定。
    不會碰 Revit / SketchUp / Blender 的安裝，也不會動其他 MCP 項目。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
[CmdletBinding()]
param(
    # 外掛 DLL 安裝位置
    [string] $PluginDir = (Join-Path $env:APPDATA 'RevitAgentBridge\addin'),

    # Revit 2024 安裝目錄（只當編譯參考）
    [string] $RevitDir = 'C:\Program Files\Autodesk\Revit 2024',

    # .addin 清單檔所在資料夾（Revit 2024 的使用者層級 Addins）
    [string] $AddinsDir = (Join-Path $env:APPDATA 'Autodesk\Revit\Addins\2024'),

    # Python venv 位置。刻意放在純 ASCII 路徑
    [string] $VenvDir = (Join-Path $env:USERPROFILE 'RevitAgentBridge\venv'),

    # 用來建立 venv 的 Python。留空 = 自動尋找 3.10 以上
    [string] $Python = '',

    # pip 套件來源（預設官方 PyPI；網路慢時可改成鏡像，例如 -PipIndex https://mirror.example/simple）
    [string] $PipIndex = 'https://pypi.org/simple',

    # skill / agent 設定的家目錄（測試用，一般不用改）
    [string] $HomeDir = $env:USERPROFILE,

    [switch] $SkipBuild,
    [switch] $SkipVenv,
    [switch] $SkipSkills,
    [switch] $SkipClaudeCodeMcp
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition

$PluginGuid = 'b7d3e1a4-5c2f-4e8a-9f61-3a2d7c0b8e15'
$SkillName  = 'revit-control'
$McpName    = 'revit'
$BackupDir  = Join-Path (Split-Path -Parent $PluginDir) 'backup'

function Write-Step  ([string] $Text) { Write-Host "`n=== $Text ===" -ForegroundColor Cyan }
function Write-Ok    ([string] $Text) { Write-Host "  [OK]   $Text" -ForegroundColor Green }
function Write-Warn2 ([string] $Text) { Write-Host "  [警告] $Text" -ForegroundColor Yellow }
function Write-Info  ([string] $Text) { Write-Host "  $Text" }

function Backup-File ([string] $Path) {
    if (Test-Path $Path) {
        $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
        $dest = "$Path.bak-$stamp"
        Copy-Item $Path $dest
        Write-Info "已備份：$dest"
    }
}

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

Write-Host '安裝 Revit 代理操控插件' -ForegroundColor White
Write-Info "來源目錄：$ProjectRoot"

$revitRunning = @(Get-Process -Name 'Revit' -ErrorAction SilentlyContinue).Count -gt 0
$dllTarget = Join-Path $PluginDir 'RevitAgentBridge.dll'
if ($revitRunning -and -not $SkipBuild) {
    Write-Warn2 'Revit 目前正在執行，RevitAgentBridge.dll 會被鎖住無法覆蓋。'
    if (Test-Path $dllTarget) {
        throw '請先關閉 Revit（記得先存檔），再重新執行 install.ps1。'
    }
}

# ---------------------------------------------------------------------------
# 1. 編譯並安裝外掛檔
# ---------------------------------------------------------------------------
Write-Step '1/5 編譯並安裝 Revit 外掛（RevitAgentBridge.dll）'

New-Item -ItemType Directory -Force -Path $PluginDir | Out-Null

if ($SkipBuild) {
    Write-Info '已略過編譯（-SkipBuild）'
} else {
    $tmpBuild = Join-Path $env:TEMP ("rvb_build_" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    & (Join-Path $ProjectRoot 'revit_plugin\build.ps1') -OutDir $tmpBuild -RevitDir $RevitDir
    $built = Join-Path $tmpBuild 'RevitAgentBridge.dll'
    if (-not (Test-Path $built)) { throw '編譯失敗：找不到輸出的 RevitAgentBridge.dll。' }
    try {
        Copy-Item $built $dllTarget -Force
        Write-Ok "已安裝 $dllTarget"
    } catch {
        throw "無法覆蓋 $dllTarget（Revit 正在使用？）。請關閉 Revit 後重新執行 install.ps1。"
    } finally {
        Remove-Item -Recurse -Force $tmpBuild -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# 2. 寫入 .addin 清單檔（Revit 啟動時自動載入）
# ---------------------------------------------------------------------------
Write-Step '2/5 寫入 Revit .addin 清單檔（使用者層級，不動 ProgramData）'

New-Item -ItemType Directory -Force -Path $AddinsDir | Out-Null
$addinPath = Join-Path $AddinsDir 'RevitAgentBridge.addin'
if (Test-Path $addinPath) { Backup-File $addinPath }

$addinXml = @"
<?xml version="1.0" encoding="utf-8"?>
<RevitAddIns>
  <AddIn Type="Application">
    <Name>Revit Agent Bridge</Name>
    <Assembly>$dllTarget</Assembly>
    <AddInId>$PluginGuid</AddInId>
    <FullClassName>RevitAgentBridge.App</FullClassName>
    <VendorId>RAGB</VendorId>
    <VendorDescription>Revit Agent Bridge - 讓本機 AI agent 透過 127.0.0.1 操控 Revit</VendorDescription>
  </AddIn>
</RevitAddIns>
"@
[System.IO.File]::WriteAllText($addinPath, $addinXml, (New-Object System.Text.UTF8Encoding($true)))
Write-Ok "已寫入：$addinPath"
Write-Info 'Revit 下次啟動時會跳出「是否載入此外掛」的安全性對話框，請選「永遠載入」；之後自動載入並啟動伺服器（預設 127.0.0.1:9879）。'

# ---------------------------------------------------------------------------
# 3. Python venv
# ---------------------------------------------------------------------------
$venvPython = Join-Path $VenvDir 'Scripts\python.exe'

if ($SkipVenv) {
    Write-Step '3/5 Python venv（已略過）'
} else {
    Write-Step '3/5 建立 / 更新 Python venv'

    $py = Find-Python -Preferred $Python
    if (-not $py) {
        Write-Host ''
        Write-Host '  找不到 Python 3.10 以上的版本。請先安裝 Python：' -ForegroundColor Red
        Write-Host '    https://www.python.org/downloads/  （安裝時勾選 "Add python.exe to PATH"）' -ForegroundColor Red
        Write-Host '  或在 PowerShell 執行：winget install Python.Python.3.12' -ForegroundColor Red
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

    Write-Info '安裝 revit_bridge 與相依套件（mcp、pillow），第一次可能要一兩分鐘…'
    $pipArgs = @('-m', 'pip', 'install', '--upgrade', '--quiet', '--disable-pip-version-check')
    if ($PipIndex) { $pipArgs += @('-i', $PipIndex) }
    $pipArgs += (Join-Path $ProjectRoot 'agent_client')
    & $venvPython @pipArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'pip install 失敗。若是網路問題，可用 -PipIndex 指定其他鏡像後重跑。'
    }

    & $venvPython -c "import revit_bridge, mcp, PIL; print('revit_bridge', revit_bridge.__version__)"
    if ($LASTEXITCODE -ne 0) { throw '安裝後匯入測試失敗。' }
    Write-Ok "venv 就緒：$venvPython"

    # setuptools 會在原始碼資料夾留下 build\ 與 *.egg-info\，清掉
    foreach ($leftover in @('agent_client\build', 'agent_client\revit_bridge.egg-info')) {
        $path = Join-Path $ProjectRoot $leftover
        if (Test-Path $path) { Remove-Item -Recurse -Force $path }
    }
}

# ---------------------------------------------------------------------------
# 4. skill（來源不存在就略過）
# ---------------------------------------------------------------------------
$skillSource = Join-Path $ProjectRoot "skill\$SkillName"
$skillTargets = [ordered]@{
    'Claude Code' = Join-Path $HomeDir ".claude\skills\$SkillName"
}

if ($SkipSkills) {
    Write-Step '4/5 安裝 skill（已略過）'
} elseif (-not (Test-Path $skillSource)) {
    Write-Step '4/5 安裝 skill'
    Write-Info "尚未提供 skill（找不到 $skillSource），略過。"
} else {
    Write-Step "4/5 安裝 $SkillName skill"
    foreach ($entry in $skillTargets.GetEnumerator()) {
        $target = $entry.Value
        $parent = Split-Path -Parent $target
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        if (Test-Path $target) { Remove-Item -Recurse -Force $target }   # 只動自己的資料夾
        Copy-Item -Path $skillSource -Destination $target -Recurse -Force
        Write-Ok "$($entry.Key) -> $target"
    }
}

# ---------------------------------------------------------------------------
# 5. MCP 註冊
# ---------------------------------------------------------------------------
Write-Step '5/5 註冊 MCP（名稱：revit）'
$mcpArgs = @('-m', 'revit_bridge.mcp_server')
$envPort = '9879'
$envEdge = '1280'

# --- Claude Code ---
if ($SkipClaudeCodeMcp) {
    Write-Info 'Claude Code：已略過'
} elseif (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Info 'Claude Code：找不到 claude 指令，略過。（用的話請手動執行：）'
    Write-Host "    claude mcp add $McpName --scope user -- `"$venvPython`" -m revit_bridge.mcp_server" -ForegroundColor White
} else {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & claude mcp remove $McpName --scope user *> $null     # 先移除舊的（不存在會失敗，忽略）
    # 注意：若 claude 是 claude.ps1，裸的 -- 會被 PowerShell 吃掉，必須加引號傳成 '--'
    & claude mcp add $McpName --scope user '--' $venvPython @mcpArgs
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -eq 0) {
        Write-Ok 'Claude Code：已註冊 MCP「revit」（使用者層級，所有專案可用）'
    } else {
        Write-Warn2 'Claude Code：claude mcp add 失敗，請手動執行：'
        Write-Host "    claude mcp add $McpName --scope user -- `"$venvPython`" -m revit_bridge.mcp_server" -ForegroundColor White
    }
}

Write-Info "其他 agent：啟動指令是 `"$venvPython`" -m revit_bridge.mcp_server ，格式範例見 mcp-examples\"

Write-Host ''
Write-Host '安裝完成。' -ForegroundColor Green
Write-Host '下一步：' -ForegroundColor Yellow
Write-Host '  1. 啟動（或重新啟動）Revit 2024，外掛第一次會問是否載入，請選「永遠載入」；之後自動開啟伺服器；也可呼叫 revit_launch / `python -m revit_bridge launch`' -ForegroundColor Yellow
Write-Host '  2. 看 Revit 功能區的「Agent Bridge」索引標籤：狀態 / 啟動伺服器 / 停止伺服器' -ForegroundColor Yellow
Write-Host '  3. 重開 Claude Code（或你的 MCP 客戶端）讓它載入 revit MCP' -ForegroundColor Yellow
