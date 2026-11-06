<#
.SYNOPSIS
    解除安裝「SketchUp 代理操控插件」。

.DESCRIPTION
    只移除本插件自己安裝的東西：
      * 各版本 SketchUp Plugins 資料夾內的 su_agent_bridge.rb 與 su_agent_bridge\
      * Claude Code 的 sketchup-control skill 資料夾
      * Claude Code / Claude Desktop 的 MCP 項目「sketchup」（只刪這一筆）
      * （選用 -RemoveVenv）獨立的 Python venv
    不會動 Plugins 內其他插件，也不會動其他 MCP 伺服器的設定。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
    powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -RemoveVenv
#>
[CmdletBinding()]
param(
    [string] $PluginsDir = '',
    [string] $VenvDir    = (Join-Path $env:USERPROFILE 'SketchUpAgentBridge\venv'),
    [string] $HomeDir    = $env:USERPROFILE,
    [string] $DesktopConfig = (Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'),
    [switch] $RemoveVenv,
    [switch] $SkipClaudeCodeMcp,
    [switch] $SkipDesktopMcp
)

$ErrorActionPreference = 'Stop'

$PluginLoaderName = 'su_agent_bridge.rb'
$PluginFolderName = 'su_agent_bridge'
$SkillName        = 'sketchup-control'
$McpName          = 'sketchup'

function Write-Step ([string] $Text) { Write-Host "`n=== $Text ===" -ForegroundColor Cyan }
function Write-Ok   ([string] $Text) { Write-Host "  [OK]   $Text" -ForegroundColor Green }
function Write-Skip ([string] $Text) { Write-Host "  [略過] $Text" -ForegroundColor DarkGray }

Write-Host '解除安裝 SketchUp 代理操控插件' -ForegroundColor White

# 1. SketchUp 擴充功能
Write-Step '移除 SketchUp 擴充功能'
$pluginDirs = @()
if ($PluginsDir) {
    $pluginDirs = @($PluginsDir)
} else {
    $root = Join-Path $env:APPDATA 'SketchUp'
    if (Test-Path $root) {
        foreach ($d in (Get-ChildItem $root -Directory -Filter 'SketchUp 20*')) {
            $p = Join-Path $d.FullName 'SketchUp\Plugins'
            if (Test-Path $p) { $pluginDirs += $p }
        }
    }
}
if ($pluginDirs.Count -eq 0) { Write-Skip '沒有找到 SketchUp 的 Plugins 資料夾' }
foreach ($dir in $pluginDirs) {
    foreach ($name in @($PluginLoaderName, $PluginFolderName)) {
        $path = Join-Path $dir $name
        if (Test-Path $path) {
            Remove-Item -Recurse -Force $path
            Write-Ok "已移除 $path"
        } else {
            Write-Skip "不存在 $path"
        }
    }
}

# 2. skill
Write-Step '移除 sketchup-control skill'
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

# 3. MCP
Write-Step '移除 MCP 項目'
if ($SkipClaudeCodeMcp) {
    Write-Skip 'Claude Code：已略過'
} elseif (Get-Command claude -ErrorAction SilentlyContinue) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & claude mcp remove $McpName --scope user *> $null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -eq 0) { Write-Ok 'Claude Code：已移除 MCP「sketchup」' } else { Write-Skip 'Claude Code：沒有註冊 sketchup' }
} else {
    Write-Skip 'Claude Code：找不到 claude 指令'
}

if ($SkipDesktopMcp) {
    Write-Skip 'Claude Desktop：已略過'
} elseif (Test-Path $DesktopConfig) {
    try {
        $cfg = Get-Content -Raw -Encoding UTF8 $DesktopConfig | ConvertFrom-Json
        if ($cfg.PSObject.Properties.Name -contains 'mcpServers' -and
            $cfg.mcpServers.PSObject.Properties.Name -contains $McpName) {
            Copy-Item $DesktopConfig ("$DesktopConfig.bak-" + (Get-Date -Format 'yyyyMMdd_HHmmss'))
            $cfg.mcpServers.PSObject.Properties.Remove($McpName)
            $json = $cfg | ConvertTo-Json -Depth 20
            [System.IO.File]::WriteAllText($DesktopConfig, $json, (New-Object System.Text.UTF8Encoding($false)))
            Write-Ok 'Claude Desktop：已移除 sketchup（原檔已備份）'
        } else {
            Write-Skip 'Claude Desktop：設定檔內沒有 sketchup'
        }
    } catch {
        Write-Skip "Claude Desktop：設定檔無法解析，請手動刪除 mcpServers.sketchup（$($_.Exception.Message)）"
    }
} else {
    Write-Skip 'Claude Desktop：沒偵測到'
}
Write-Host "  其他 MCP 客戶端的設定若有加過，請自行刪除 sketchup 那一筆。" -ForegroundColor DarkGray

# 4. venv
Write-Step 'Python venv'
if ($RemoveVenv) {
    $vroot = Split-Path -Parent $VenvDir
    if (Test-Path $VenvDir) {
        Remove-Item -Recurse -Force $VenvDir
        Write-Ok "已移除 $VenvDir"
        if ((Test-Path $vroot) -and -not (Get-ChildItem $vroot -Force)) {
            Remove-Item -Force $vroot
            Write-Ok "已移除空資料夾 $vroot"
        }
    } else {
        Write-Skip "不存在 $VenvDir"
    }
} else {
    Write-Skip "保留 $VenvDir（加上 -RemoveVenv 才會刪除）"
}

Write-Host ''
Write-Host '解除安裝完成。' -ForegroundColor Green
