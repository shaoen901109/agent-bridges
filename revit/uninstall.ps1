<#
.SYNOPSIS
    解除安裝「Revit 代理操控插件」。

.DESCRIPTION
    只移除本插件自己安裝的東西：
      * %APPDATA%\Autodesk\Revit\Addins\2024\RevitAgentBridge.addin（刪除前先備份）
      * %APPDATA%\RevitAgentBridge\addin\（DLL）與 %LOCALAPPDATA%\RevitAgentBridge\bridge.log
      * Claude Code 的 revit-control skill 資料夾
      * Claude Code 的 MCP 項目「revit」（只刪這一筆；其他 MCP 客戶端的設定請自行刪除）
      * （選用 -RemoveVenv）獨立的 Python venv
    不會動 Revit / SketchUp / Blender 的任何東西，也不會動其他 MCP 伺服器的設定。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
    powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -RemoveVenv
#>
[CmdletBinding()]
param(
    [string] $PluginDir    = (Join-Path $env:APPDATA 'RevitAgentBridge\addin'),
    [string] $AddinsDir    = (Join-Path $env:APPDATA 'Autodesk\Revit\Addins\2024'),
    [string] $VenvDir      = (Join-Path $env:USERPROFILE 'RevitAgentBridge\venv'),
    [string] $HomeDir      = $env:USERPROFILE,
    [switch] $RemoveVenv,
    [switch] $SkipClaudeCodeMcp
)

$ErrorActionPreference = 'Stop'

$SkillName  = 'revit-control'
$McpName    = 'revit'
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

Write-Host '解除安裝 Revit 代理操控插件' -ForegroundColor White

if (@(Get-Process -Name 'Revit' -ErrorAction SilentlyContinue).Count -gt 0) {
    Write-Warn2 'Revit 正在執行：DLL 可能被鎖住；.addin 會移除，DLL 請關閉 Revit 後再跑一次。'
}

# 1. .addin 清單檔
Write-Step '移除 Revit .addin'
$addinPath = Join-Path $AddinsDir 'RevitAgentBridge.addin'
if (Test-Path $addinPath) {
    Backup-File $addinPath
    Remove-Item -Force $addinPath
    Write-Ok "已移除 $addinPath（備份：$addinPath.bak-*）"
} else {
    Write-Skip "不存在 $addinPath"
}

# 2. 外掛檔
Write-Step '移除外掛檔案'
$logDir = Join-Path $env:LOCALAPPDATA 'RevitAgentBridge'
foreach ($path in @($PluginDir, (Join-Path $logDir 'bridge.log'))) {
    if (Test-Path $path) {
        try {
            Remove-Item -Recurse -Force $path
            Write-Ok "已移除 $path"
        } catch {
            Write-Warn2 "無法移除 $path（Revit 正在使用？）：$($_.Exception.Message)"
        }
    } else {
        Write-Skip "不存在 $path"
    }
}
foreach ($dir in @($DataDir, $logDir)) {
    if ((Test-Path $dir) -and -not (Get-ChildItem $dir -Force -ErrorAction SilentlyContinue)) { Remove-Item -Force $dir -ErrorAction SilentlyContinue }
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
Write-Step '移除 MCP 項目「revit」'
if ($SkipClaudeCodeMcp) {
    Write-Skip 'Claude Code：已略過'
} elseif (Get-Command claude -ErrorAction SilentlyContinue) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & claude mcp remove $McpName --scope user *> $null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -eq 0) { Write-Ok 'Claude Code：已移除 revit' } else { Write-Skip 'Claude Code：沒有 revit 項目' }
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
