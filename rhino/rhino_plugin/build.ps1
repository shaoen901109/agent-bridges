<#
.SYNOPSIS
    編譯 Rhino 端外掛 RhinoAgentBridge.rhp（供 install.ps1 呼叫，也可單獨執行）。

.DESCRIPTION
    優先使用 Visual Studio Build Tools 的 Roslyn csc.exe；找不到就退回 .NET Framework 內建的 csc.exe
    （C# 5，原始碼刻意只用 C# 5 語法）。引用 Rhino 7 安裝目錄的 RhinoCommon.dll。
    不需要 Visual Studio 或 .NET SDK。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\build.ps1 -OutDir C:\temp\rab
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $OutDir,
    [string] $RhinoDir = 'C:\Program Files\Rhino 7'
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition

$rhinoCommon = Join-Path $RhinoDir 'System\RhinoCommon.dll'
if (-not (Test-Path $rhinoCommon)) { throw "找不到 RhinoCommon.dll：$rhinoCommon（請確認 Rhino 7 安裝路徑，或用 -RhinoDir 指定）" }

$fw = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319'
$csc = $null
$roslynCandidates = @()
foreach ($pf in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
    if (-not $pf) { continue }
    $vs = Join-Path $pf 'Microsoft Visual Studio'
    if (Test-Path $vs) {
        $roslynCandidates += Get-ChildItem $vs -Recurse -Filter csc.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match 'Roslyn' }
    }
}
if ($roslynCandidates.Count -gt 0) { $csc = ($roslynCandidates | Sort-Object FullName -Descending | Select-Object -First 1).FullName }
if (-not $csc) { $csc = Join-Path $fw 'csc.exe' }
if (-not (Test-Path $csc)) { throw "找不到 csc.exe（.NET Framework 4.x 或 VS Build Tools）。" }
Write-Host "  編譯器：$csc"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$out = Join-Path $OutDir 'RhinoAgentBridge.rhp'
$src = Join-Path $here 'src\BridgePlugIn.cs'

$refs = @(
    $rhinoCommon,
    (Join-Path $fw 'System.dll'),
    (Join-Path $fw 'System.Core.dll'),
    (Join-Path $fw 'System.Web.Extensions.dll'),
    (Join-Path $fw 'System.Drawing.dll')
)
$cscArgs = @('/nologo', '/target:library', '/platform:x64', '/optimize+', '/codepage:65001', '/utf8output', "/out:$out")
foreach ($r in $refs) { $cscArgs += "/reference:$r" }
$cscArgs += $src

& $csc @cscArgs
if ($LASTEXITCODE -ne 0) { throw "編譯失敗（csc 結束碼 $LASTEXITCODE）。" }
Write-Host "  已編譯：$out"
