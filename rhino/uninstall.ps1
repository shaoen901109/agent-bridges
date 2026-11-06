<#
.SYNOPSIS
    解除安裝「Rhino 代理操控插件」。

.DESCRIPTION
    只移除本插件自己安裝的東西：
      * HKCU\SOFTWARE\McNeel\Rhinoceros\7.0\Plug-ins\<本外掛 GUID> 登錄機碼（刪除前先匯出 .reg 備份）
      * %LOCALAPPDATA%\RhinoAgentBridge\plugin\（.rhp 與 Python 腳本）與 bridge.log
      * Claude Code 的 rhino-control skill 資料夾
      * Claude Code 的 MCP 項目「rhino」（只刪這一筆；其他 MCP 客戶端的設定請自行刪除）
      * （選用 -RemoveVenv）獨立的 Python venv
    安裝時沒有修改 Rhino 的 settings XML，所以不需要還原它。
    不會動 SketchUp / Blender 的任何東西，也不會動其他 MCP 伺服器的設定。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
    powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -RemoveVenv
#>
[CmdletBinding()]
param(
    [string] $PluginDir    = (Join-Path $env:LOCALAPPDATA 'RhinoAgentBridge\plugin'),
    [string] $RegistryRoot = 'HKCU:\SOFTWARE\McNeel\Rhinoceros\7.0\Plug-ins',
    [string] $VenvDir      = (Join-Path $env:USERPROFILE 'RhinoAgentBridge\venv'),
    [string] $HomeDir      = $env:USERPROFILE,
    [switch] $RemoveVenv,
    [switch] $SkipClaudeCodeMcp
)

$ErrorActionPreference = 'Stop'

$PluginGuid = '4bc68b53-2390-428e-a045-42bd3604fdab'
$SkillName  = 'rhino-control'
$McpName    = 'rhino'
$DataDir    = Split-Path -Parent $PluginDir
$BackupDir  = Join-Path $DataDir 'backup'

function Write-Step ([string] $Text) { Write-Host "`n=== $Text ===" -ForegroundColor Cyan }
function Write-Ok   ([string] $Text) { Write-Host "  [OK]   $Text" -ForegroundColor Green }
function Write-Skip ([string] $Text) { Write-Host "  [略過] $Text" -ForegroundColor DarkGray }
function Write-Warn2 ([string] $Text) { Write-Host "  [警告] $Text" -ForegroundColor Yellow }

function Backup-File ([string] $Path) {
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    Copy-Item $Path "$Path.bak-$stamp"
}

Write-Host '解除安裝 Rhino 代理操控插件' -ForegroundColor White

if (@(Get-Process -Name 'Rhino' -ErrorAction SilentlyContinue).Count -gt 0) {
    Write-Warn2 'Rhino 正在執行：外掛檔可能被鎖住；登錄機碼會移除，檔案請關閉 Rhino 後再跑一次。'
}

# 1. 登錄機碼
Write-Step '移除 Rhino 外掛登錄'
$keyPath = Join-Path $RegistryRoot $PluginGuid
if (Test-Path $keyPath) {
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    $regFile = Join-Path $BackupDir ("plugin_key_uninstall_{0}.reg" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
    $nativePath = ($keyPath -replace '^HKCU:\\', 'HKEY_CURRENT_USER\')
    & reg.exe export $nativePath $regFile /y *> $null
    Remove-Item -Path $keyPath -Recurse -Force
    Write-Ok "已移除 $keyPath（備份：$regFile）"
} else {
    Write-Skip "不存在 $keyPath"
}

# 2. 外掛檔
Write-Step '移除外掛檔案'
foreach ($path in @($PluginDir, (Join-Path $DataDir 'bridge.log'))) {
    if (Test-Path $path) {
        try {
            Remove-Item -Recurse -Force $path
            Write-Ok "已移除 $path"
        } catch {
            Write-Warn2 "無法移除 $path（Rhino 正在使用？）：$($_.Exception.Message)"
        }
    } else {
        Write-Skip "不存在 $path"
    }
}

# 3. skill
Write-Step "移除 $SkillName skill"
$skillTargets = [ordered]@{
    'Claude Code' = Join-Path $HomeDir ".claude\skills\$SkillName"
}
foreach ($entry in $skillTargets.GetEnumerator()) {
    $path = $entry.Value
    if (Test-Path $path) {
        Remove-Item -Recurse -Force $path
        Write-Ok "$($entry.Key)：已移除 $path"
    } else {
        Write-Skip "$($entry.Key)：不存在 $path"
    }
}

# 4. MCP
Write-Step '移除 MCP 項目「rhino」'
if ($SkipClaudeCodeMcp) {
    Write-Skip 'Claude Code：已略過'
} elseif (Get-Command claude -ErrorAction SilentlyContinue) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & claude mcp remove $McpName --scope user *> $null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -eq 0) { Write-Ok 'Claude Code：已移除 rhino' } else { Write-Skip 'Claude Code：沒有 rhino 項目' }
} else {
    Write-Skip 'Claude Code：找不到 claude 指令'
}

# 5. venv
Write-Step 'Python venv'
if ($RemoveVenv) {
    if (Test-Path $VenvDir) {
        Remove-Item -Recurse -Force $VenvDir
        Write-Ok "已移除 $VenvDir"
        $parent = Split-Path -Parent $VenvDir
        if ((Test-Path $parent) -and -not (Get-ChildItem $parent -Force)) { Remove-Item -Force $parent }
    } else {
        Write-Skip "不存在 $VenvDir"
    }
} else {
    Write-Skip "保留 venv（$VenvDir）；要一併刪除請加 -RemoveVenv"
}

Write-Host ''
Write-Host '解除安裝完成。' -ForegroundColor Green
