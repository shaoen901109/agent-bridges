<#
.SYNOPSIS
    安裝「Rhino 代理操控插件」（Rhino 7 + Grasshopper Agent Bridge）。

.DESCRIPTION
    每一步都可重複執行，且只會碰自己的檔案。Rhino 不必是開著的（關閉狀態也能安裝）：
      1. 編譯 C# 外掛 RhinoAgentBridge.rhp，連同 Python 指令腳本複製到
         %LOCALAPPDATA%\RhinoAgentBridge\plugin\（不需要 Visual Studio，用系統內建 / Build Tools 的 csc）
      2. 在 HKCU\SOFTWARE\McNeel\Rhinoceros\7.0\Plug-ins\<GUID> 登錄外掛（AtStartup）。
         Rhino 下次啟動就會自動載入、自動開伺服器。完全不修改 Rhino 的 settings XML。
         （若該登錄機碼已存在，會先匯出備份成 .reg；解除安裝時只刪這一個機碼。）
      3. 建立（或更新）獨立的 Python venv 並安裝 rhino_bridge（pip 來源預設官方 PyPI，可用 -PipIndex 改成鏡像）
      4. （選用）把 rhino-control skill 複製到 Claude Code 的 skills 資料夾；來源資料夾不存在就略過
      5. 註冊 MCP（名稱「rhino」）到 Claude Code（claude mcp add）。
         其他 MCP 客戶端請參考 mcp-examples\ 手動設定。
    不會碰 SketchUp / Blender 的安裝，也不會動其他 MCP 項目。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
[CmdletBinding()]
param(
    # 外掛安裝位置（含 RhinoAgentBridge.rhp 與 py\）
    [string] $PluginDir = (Join-Path $env:LOCALAPPDATA 'RhinoAgentBridge\plugin'),

    # Rhino 7 安裝目錄
    [string] $RhinoDir = 'C:\Program Files\Rhino 7',

    # Rhino 外掛登錄根機碼（測試用，一般不用改）
    [string] $RegistryRoot = 'HKCU:\SOFTWARE\McNeel\Rhinoceros\7.0\Plug-ins',

    # Python venv 位置。刻意放在純 ASCII 路徑
    [string] $VenvDir = (Join-Path $env:USERPROFILE 'RhinoAgentBridge\venv'),

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

$PluginGuid = '4bc68b53-2390-428e-a045-42bd3604fdab'
$SkillName  = 'rhino-control'
$McpName    = 'rhino'
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

Write-Host '安裝 Rhino 代理操控插件' -ForegroundColor White
Write-Info "來源目錄：$ProjectRoot"

$rhinoRunning = @(Get-Process -Name 'Rhino' -ErrorAction SilentlyContinue).Count -gt 0
if ($rhinoRunning) {
    Write-Warn2 'Rhino 目前正在執行。外掛檔若被鎖住會無法覆蓋；Python 腳本可以熱更新，.rhp 要關閉 Rhino 後重跑才會換成新版。'
}

# ---------------------------------------------------------------------------
# 1. 編譯並安裝外掛檔
# ---------------------------------------------------------------------------
Write-Step '1/5 編譯並安裝 Rhino 外掛（.rhp + Python 腳本）'

$srcPy = Join-Path $ProjectRoot 'rhino_plugin\py'
if (-not (Test-Path $srcPy)) { throw "找不到 Python 腳本資料夾：$srcPy" }
New-Item -ItemType Directory -Force -Path $PluginDir | Out-Null
$rhpTarget = Join-Path $PluginDir 'RhinoAgentBridge.rhp'

if ($SkipBuild) {
    Write-Info '已略過編譯（-SkipBuild）'
} else {
    $tmpBuild = Join-Path $env:TEMP ("rab_build_" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    & (Join-Path $ProjectRoot 'rhino_plugin\build.ps1') -OutDir $tmpBuild -RhinoDir $RhinoDir
    $built = Join-Path $tmpBuild 'RhinoAgentBridge.rhp'
    if (-not (Test-Path $built)) { throw '編譯失敗：找不到輸出的 RhinoAgentBridge.rhp。' }
    try {
        Copy-Item $built $rhpTarget -Force
        Write-Ok "已安裝 $rhpTarget"
    } catch {
        Write-Warn2 "無法覆蓋 $rhpTarget（Rhino 正在使用？）。請關閉 Rhino 後重新執行 install.ps1。"
    } finally {
        Remove-Item -Recurse -Force $tmpBuild -ErrorAction SilentlyContinue
    }
}

$pyTarget = Join-Path $PluginDir 'py'
New-Item -ItemType Directory -Force -Path $pyTarget | Out-Null
Get-ChildItem $pyTarget -Filter '*.py' -ErrorAction SilentlyContinue | Remove-Item -Force   # 只清自己的資料夾
Copy-Item -Path (Join-Path $srcPy '*.py') -Destination $pyTarget -Force
Write-Ok "已複製 Python 腳本到 $pyTarget"

# ---------------------------------------------------------------------------
# 2. 登錄外掛（AtStartup 自動載入）
# ---------------------------------------------------------------------------
Write-Step '2/5 登錄外掛到 Rhino（使用者層級登錄，不修改 settings XML）'

$keyPath = Join-Path $RegistryRoot $PluginGuid
if (Test-Path $keyPath) {
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    $regFile = Join-Path $BackupDir ("plugin_key_{0}.reg" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
    $nativePath = ($keyPath -replace '^HKCU:\\', 'HKEY_CURRENT_USER\')
    & reg.exe export $nativePath $regFile /y *> $null
    if ($LASTEXITCODE -eq 0) { Write-Info "原有登錄機碼已匯出備份：$regFile" }
}

New-Item -Path $keyPath -Force | Out-Null
$props = [ordered]@{
    'Name' = 'Rhino Agent Bridge'; 'Organization' = 'Rhino Agent Bridge'; 'Address' = 'local'
    'Country' = 'Taiwan'; 'Phone' = '0'; 'Fax' = '0'; 'EMail' = 'none@example.invalid'
    'WebSite' = ''; 'UpdateURL' = ''
    'Description' = '讓本機 AI agent 透過 127.0.0.1 操控 Rhino 與 Grasshopper'
    'RegPath' = '\\HKEY_CURRENT_USER\Software\McNeel\Rhinoceros\7.0\Plug-Ins\' + $PluginGuid
}
foreach ($k in $props.Keys) { New-ItemProperty -Path $keyPath -Name $k -Value $props[$k] -PropertyType String -Force | Out-Null }
# LoadMode=1 (AtStartup)、Type=16 (一般外掛)
$dwords = [ordered]@{ 'AddToHelpMenu' = 0; 'LoadMode' = 1; 'Type' = 16; 'IsDotNETPlugIn' = 1; 'DirectoryInstall' = 0 }
foreach ($k in $dwords.Keys) { New-ItemProperty -Path $keyPath -Name $k -Value $dwords[$k] -PropertyType DWord -Force | Out-Null }

$cmdKey = Join-Path $keyPath 'CommandList'
New-Item -Path $cmdKey -Force | Out-Null
foreach ($c in @('RhinoAgentBridge', 'RhinoBridgeStart', 'RhinoBridgeStop', 'RhinoBridgeStatus')) {
    New-ItemProperty -Path $cmdKey -Name $c -Value "2;$c" -PropertyType String -Force | Out-Null
}
$plugKey = Join-Path $keyPath 'PlugIn'
New-Item -Path $plugKey -Force | Out-Null
New-ItemProperty -Path $plugKey -Name 'FileName' -Value $rhpTarget -PropertyType String -Force | Out-Null
Write-Ok "已登錄：$keyPath"
Write-Info 'Rhino 下次啟動時自動載入外掛並啟動伺服器（預設 127.0.0.1:9878）。'

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

    Write-Info '安裝 rhino_bridge 與相依套件（mcp、pillow），第一次可能要一兩分鐘…'
    $pipArgs = @('-m', 'pip', 'install', '--upgrade', '--quiet', '--disable-pip-version-check')
    if ($PipIndex) { $pipArgs += @('-i', $PipIndex) }
    $pipArgs += (Join-Path $ProjectRoot 'agent_client')
    & $venvPython @pipArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'pip install 失敗。若是網路問題，可用 -PipIndex 指定其他鏡像後重跑。'
    }

    & $venvPython -c "import rhino_bridge, mcp, PIL; print('rhino_bridge', rhino_bridge.__version__)"
    if ($LASTEXITCODE -ne 0) { throw '安裝後匯入測試失敗。' }
    Write-Ok "venv 就緒：$venvPython"

    # setuptools 會在原始碼資料夾留下 build\ 與 *.egg-info\，清掉
    foreach ($leftover in @('agent_client\build', 'agent_client\rhino_bridge.egg-info')) {
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
Write-Step '5/5 註冊 MCP（名稱：rhino）'
$mcpArgs = @('-m', 'rhino_bridge.mcp_server')
$envPort = '9878'
$envEdge = '1280'

# --- Claude Code ---
if ($SkipClaudeCodeMcp) {
    Write-Info 'Claude Code：已略過'
} elseif (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Info 'Claude Code：找不到 claude 指令，略過。（用的話請手動執行：）'
    Write-Host "    claude mcp add $McpName --scope user -- `"$venvPython`" -m rhino_bridge.mcp_server" -ForegroundColor White
} else {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & claude mcp remove $McpName --scope user *> $null     # 先移除舊的（不存在會失敗，忽略）
    # 注意：若 claude 是 claude.ps1，裸的 -- 會被 PowerShell 吃掉，必須加引號傳成 '--'
    & claude mcp add $McpName --scope user '--' $venvPython @mcpArgs
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -eq 0) {
        Write-Ok 'Claude Code：已註冊 MCP「rhino」（使用者層級，所有專案可用）'
    } else {
        Write-Warn2 'Claude Code：claude mcp add 失敗，請手動執行：'
        Write-Host "    claude mcp add $McpName --scope user -- `"$venvPython`" -m rhino_bridge.mcp_server" -ForegroundColor White
    }
}

Write-Info "其他 agent：啟動指令是 `"$venvPython`" -m rhino_bridge.mcp_server ，格式範例見 mcp-examples\"

Write-Host ''
Write-Host '安裝完成。' -ForegroundColor Green
Write-Host '下一步：' -ForegroundColor Yellow
Write-Host '  1. 啟動（或重新啟動）Rhino 7，外掛會自動載入並開啟伺服器；也可呼叫 rhino_launch / `python -m rhino_bridge launch`' -ForegroundColor Yellow
Write-Host '  2. 在 Rhino 指令列輸入 RhinoBridgeStatus 可確認伺服器狀態；RhinoBridgeStart / RhinoBridgeStop 可手動開關' -ForegroundColor Yellow
Write-Host '  3. 重開 Claude Code（或你的 MCP 客戶端）讓它載入 rhino MCP' -ForegroundColor Yellow
