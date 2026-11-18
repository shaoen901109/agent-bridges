<#
.SYNOPSIS
    編譯 Revit 端外掛 RevitAgentBridge.dll（供 install.ps1 呼叫，也可單獨執行）。

.DESCRIPTION
    優先使用 Visual Studio Build Tools 的 Roslyn csc.exe；找不到就退回 .NET Framework 內建的 csc.exe。
    原始碼刻意只用 C# 5 語法（編譯時加 /langversion:5 強制檢查）。引用 Revit 安裝目錄的 RevitAPI.dll / RevitAPIUI.dll
    （只當編譯參考，不會複製進輸出）。不需要 Visual Studio 或 .NET SDK。
    加 -RunTests 會另外編譯 tests\EvalWrapperTests.cs（只含不依賴 Revit 的片段包裝邏輯）並執行。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\build.ps1 -OutDir C:\temp\rab
    powershell -ExecutionPolicy Bypass -File .\build.ps1 -OutDir C:\temp\rab -RunTests
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $OutDir,
    [string] $RevitDir = 'C:\Program Files\Autodesk\Revit 2024',
    [switch] $RunTests
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition

$revitApi = Join-Path $RevitDir 'RevitAPI.dll'
$revitApiUi = Join-Path $RevitDir 'RevitAPIUI.dll'
if (-not (Test-Path $revitApi)) { throw "找不到 RevitAPI.dll：$revitApi（請確認 Revit 2024 安裝路徑，或用 -RevitDir 指定）" }
if (-not (Test-Path $revitApiUi)) { throw "找不到 RevitAPIUI.dll：$revitApiUi" }

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
$out = Join-Path $OutDir 'RevitAgentBridge.dll'
$srcFiles = Get-ChildItem (Join-Path $here 'src') -Filter '*.cs' | ForEach-Object { $_.FullName }

$refs = @(
    $revitApi,
    $revitApiUi,
    (Join-Path $fw 'System.dll'),
    (Join-Path $fw 'System.Core.dll'),
    (Join-Path $fw 'System.Web.Extensions.dll')
)
$cscArgs = @('/nologo', '/target:library', '/platform:x64', '/optimize+', '/langversion:5', '/codepage:65001', '/utf8output', "/out:$out")
foreach ($r in $refs) { $cscArgs += "/reference:$r" }
$cscArgs += $srcFiles

& $csc @cscArgs
if ($LASTEXITCODE -ne 0) { throw "編譯失敗（csc 結束碼 $LASTEXITCODE）。" }
Write-Host "  已編譯：$out（csc 結束碼 $LASTEXITCODE）"

if ($RunTests) {
    $testExe = Join-Path $OutDir 'EvalWrapperTests.exe'
    $testArgs = @('/nologo', '/target:exe', '/optimize+', '/langversion:5', '/codepage:65001', '/utf8output', "/out:$testExe",
        "/reference:$(Join-Path $fw 'System.dll')", "/reference:$(Join-Path $fw 'System.Core.dll')",
        (Join-Path $here 'src\EvalWrapper.cs'), (Join-Path $here 'tests\EvalWrapperTests.cs'))
    & $csc @testArgs
    if ($LASTEXITCODE -ne 0) { throw "測試程式編譯失敗（csc 結束碼 $LASTEXITCODE）。" }
    & $testExe
    $code = $LASTEXITCODE
    Write-Host "  EvalWrapperTests 結束碼 $code"
    if ($code -ne 0) { throw "EvalWrapper 測試失敗（結束碼 $code）。" }
}
