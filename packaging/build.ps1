#requires -Version 5.1
<#
    构建划词翻译：
      1. PyInstaller 免安装目录
      2. PyInstaller 单文件便携版
      3. Inno Setup 安装包
    在仓库根目录运行：
      powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#>
[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$Iscc = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$Pkg = $PSScriptRoot
$AppScript = Join-Path $Root "划词翻译.pyw"
$Icon = Join-Path $Root "图标.ico"
$Readme = Join-Path $Root "README.md"
$VersionFile = Join-Path $Pkg "version_info.txt"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令失败（退出码 $LASTEXITCODE）：$FilePath $($Arguments -join ' ')"
    }
}

Write-Host "==> 检查构建环境" -ForegroundColor Cyan
if (-not (Test-Path $AppScript)) { throw "找不到主程序：$AppScript" }
if (-not (Test-Path $Icon)) { throw "找不到图标：$Icon" }
if (-not (Test-Path $VersionFile)) { throw "找不到版本文件：$VersionFile" }

$pyVersion = & $Python --version 2>&1
Write-Host "Python: $pyVersion"
& $Python -m PyInstaller --version 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "未安装 PyInstaller，请先运行：python -m pip install -r requirements-dev.txt"
}

Write-Host "==> 清理旧构建" -ForegroundColor Cyan
foreach ($dir in @("dist", "build", "build_portable", "portable", "installer")) {
    $path = Join-Path $Pkg $dir
    if (Test-Path $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
Get-ChildItem -LiteralPath $Pkg -Filter "*.spec" -ErrorAction SilentlyContinue |
    Remove-Item -Force

Write-Host "==> 构建免安装目录" -ForegroundColor Cyan
$common = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean", "--windowed",
    "--name", "划词翻译",
    "--icon", $Icon,
    "--version-file", $VersionFile,
    "--add-data", "$Icon;.",
    "--add-data", "$Readme;.",
    "--specpath", $Pkg,
    "--distpath", (Join-Path $Pkg "dist"),
    "--workpath", (Join-Path $Pkg "build"),
    $AppScript
)
Invoke-Checked -FilePath $Python -Arguments $common

Write-Host "==> 构建便携版" -ForegroundColor Cyan
$portable = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "划词翻译-便携版",
    "--icon", $Icon,
    "--version-file", $VersionFile,
    "--add-data", "$Icon;.",
    "--add-data", "$Readme;.",
    "--specpath", $Pkg,
    "--distpath", (Join-Path $Pkg "portable"),
    "--workpath", (Join-Path $Pkg "build_portable"),
    $AppScript
)
Invoke-Checked -FilePath $Python -Arguments $portable

$appDir = Join-Path $Pkg "dist\划词翻译"
$appExe = Join-Path $appDir "划词翻译.exe"
$portableExe = Join-Path $Pkg "portable\划词翻译-便携版.exe"
if (-not (Test-Path $appExe)) { throw "免安装目录构建失败：$appExe 不存在" }
if (-not (Test-Path $portableExe)) { throw "便携版构建失败：$portableExe 不存在" }

Write-Host "==> 查找 Inno Setup" -ForegroundColor Cyan
$candidates = @()
if ($Iscc) { $candidates += $Iscc }
if ($env:ISCC) { $candidates += $env:ISCC }
$candidates += @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
)
$isccPath = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $isccPath) {
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { $isccPath = $cmd.Source }
}
if (-not $isccPath) {
    throw "找不到 ISCC.exe。请安装 Inno Setup 6/7，或设置 `$env:ISCC` 指向 ISCC.exe。"
}

Write-Host "==> 构建安装包" -ForegroundColor Cyan
Invoke-Checked -FilePath $isccPath -Arguments @((Join-Path $Pkg "安装包.iss"))

$installerExe = Join-Path $Pkg "installer\划词翻译-安装包.exe"
if (-not (Test-Path $installerExe)) { throw "安装包构建失败：$installerExe 不存在" }

Write-Host ""
Write-Host "构建完成：" -ForegroundColor Green
Write-Host "  免安装目录 : $appDir"
Write-Host "  便携版     : $portableExe"
Write-Host "  安装包     : $installerExe"
Write-Host ""
Write-Host "SHA256:" -ForegroundColor Cyan
Get-FileHash -LiteralPath $portableExe -Algorithm SHA256 |
    Select-Object Path, Hash | Format-List
Get-FileHash -LiteralPath $installerExe -Algorithm SHA256 |
    Select-Object Path, Hash | Format-List
