<#
.SYNOPSIS
    安裝「Power Automate 代理操控」（pad-bridge）。

.DESCRIPTION
    每一步都可重複執行，且只會碰自己的檔案：
      1. 建立（或更新）獨立的 Python venv 並安裝 pad_bridge（mcp、uiautomation）
      2. 把 pad-control skill 複製到 Claude Code 的 skills 資料夾
      3. 註冊 MCP 到 Claude Code（使用者層級；需有 claude 指令）
    不會修改 Power Automate 的任何設定。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -PipIndex https://mirror.example/simple

.EXAMPLE
    # 讓 bridge 替自己觸發的流程自動按 PAD 的「繼續」確認框
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -AutoConfirm
#>
[CmdletBinding()]
param(
    # Python venv 位置。刻意放在純 ASCII 路徑，避免中文路徑在 agent 啟動時出問題
    [string] $VenvDir = (Join-Path $env:USERPROFILE 'PADAgentBridge\venv'),

    # 用來建立 venv 的 Python。留空 = 用 py -3 / python
    [string] $Python = '',

    # pip 套件來源（預設官方 PyPI；網路慢時可改成鏡像）
    [string] $PipIndex = 'https://pypi.org/simple',

    # MCP 註冊時加上 PAD_BRIDGE_AUTO_CONFIRM=1
    [switch] $AutoConfirm,

    [switch] $SkipVenv,
    [switch] $SkipSkills,
    [switch] $SkipClaudeCodeMcp
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$SkillName = 'pad-control'
$McpName   = 'pad'

function Write-Step  ([string] $Text) { Write-Host "`n=== $Text ===" -ForegroundColor Cyan }
function Write-Ok    ([string] $Text) { Write-Host "  [OK]   $Text" -ForegroundColor Green }
function Write-Warn2 ([string] $Text) { Write-Host "  [警告] $Text" -ForegroundColor Yellow }
function Write-Info  ([string] $Text) { Write-Host "  $Text" }

Write-Host '安裝 Power Automate 代理操控（pad-bridge）' -ForegroundColor White
Write-Info "來源目錄：$ProjectRoot"

if (-not (Get-AppxPackage Microsoft.PowerAutomateDesktop -ErrorAction SilentlyContinue) -and
    -not (Test-Path 'C:\Program Files (x86)\Power Automate Desktop')) {
    Write-Warn2 '沒有偵測到 Power Automate（桌面版）。bridge 仍會安裝，但要先裝好並登入 PAD 才能使用。'
}

# ---------------------------------------------------------------------------
# 1. Python venv
# ---------------------------------------------------------------------------
$venvPython = Join-Path $VenvDir 'Scripts\python.exe'
if ($SkipVenv) {
    Write-Step '1/3 Python venv（已略過）'
} else {
    Write-Step '1/3 建立 / 更新 Python venv'
    if (-not (Test-Path $venvPython)) {
        $py = if ($Python) { @($Python) } elseif (Get-Command py -ErrorAction SilentlyContinue) { @('py', '-3') } else { @('python') }
        Write-Info "建立 venv：$VenvDir（使用 $($py -join ' ')）"
        $exe, $pre = $py[0], @($py | Select-Object -Skip 1)
        & $exe @pre -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw 'venv 建立失敗；需要 Python 3.10 以上。' }
    } else {
        Write-Info "venv 已存在，更新套件：$VenvDir"
    }
    $pipArgs = @('-m', 'pip', 'install', '--upgrade', '--quiet', '--disable-pip-version-check')
    if ($PipIndex) { $pipArgs += @('--index-url', $PipIndex) }
    $pipArgs += (Join-Path $ProjectRoot 'agent_client')
    & $venvPython @pipArgs
    if ($LASTEXITCODE -ne 0) { throw 'pip install 失敗。網路慢時加上 -PipIndex 指定鏡像後重跑。' }
    & $venvPython -c "import pad_bridge, mcp, uiautomation; print('pad_bridge', pad_bridge.__version__)"
    if ($LASTEXITCODE -ne 0) { throw '安裝後匯入測試失敗。' }
    Write-Ok "venv 就緒：$venvPython"
    foreach ($leftover in @('agent_client\build', 'agent_client\pad_bridge.egg-info')) {
        $path = Join-Path $ProjectRoot $leftover
        if (Test-Path $path) { Remove-Item -Recurse -Force $path }
    }
}

# ---------------------------------------------------------------------------
# 2. skill
# ---------------------------------------------------------------------------
$skillSource = Join-Path $ProjectRoot "skill\$SkillName"
$skillTargets = [ordered]@{ 'Claude Code' = Join-Path $env:USERPROFILE ".claude\skills\$SkillName" }
if ($SkipSkills) {
    Write-Step '2/3 安裝 skill（已略過）'
} else {
    Write-Step '2/3 安裝 pad-control skill'
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
# 3. MCP 註冊
# ---------------------------------------------------------------------------
Write-Step '3/3 註冊 MCP'
$envArgs = if ($AutoConfirm) { @('-e', 'PAD_BRIDGE_AUTO_CONFIRM=1') } else { @() }
$manual = "claude mcp add $McpName --scope user $($envArgs -join ' ') -- `"$venvPython`" -m pad_bridge.mcp_server"
if ($SkipClaudeCodeMcp) {
    Write-Info 'Claude Code：已略過'
} elseif (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Info 'Claude Code：找不到 claude 指令，請手動執行：'
    Write-Host "    $manual" -ForegroundColor White
} else {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & claude mcp remove $McpName --scope user *> $null
    # 字面的 -- 會被 PowerShell 吃掉，放進陣列才會原樣傳給 claude
    $addArgs = @('mcp', 'add', $McpName, '--scope', 'user') + $envArgs + @('--', $venvPython, '-m', 'pad_bridge.mcp_server')
    & claude @addArgs
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -eq 0) { Write-Ok "Claude Code：已註冊 MCP「$McpName」（使用者層級）" }
    else { Write-Warn2 'claude mcp add 失敗，請手動執行：'; Write-Host "    $manual" -ForegroundColor White }
}
Write-Info "其他 MCP 客戶端：啟動指令是 `"$venvPython`" -m pad_bridge.mcp_server ，格式範例見 mcp-examples\"

Write-Host ''
Write-Host '安裝完成。重開 Claude Code 後即可使用 pad_* 工具。' -ForegroundColor Green
if (-not $AutoConfirm) {
    Write-Host '提醒：PAD 預設每次外部觸發都會跳確認框。要免確認，可在 PAD console 設定關閉' -ForegroundColor Yellow
    Write-Host '「從外部叫用流程時顯示確認對話方塊」，或重跑本腳本加 -AutoConfirm。' -ForegroundColor Yellow
}
