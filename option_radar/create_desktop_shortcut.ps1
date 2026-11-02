$ErrorActionPreference = "Stop"

# 在桌面创建「期权波动率雷达」快捷方式，指向本文件夹内的 OptionRadar.exe
$exe = Join-Path $PSScriptRoot "OptionRadar.exe"
if (-not (Test-Path $exe)) {
    Write-Host "未找到 OptionRadar.exe，请把本脚本放在 exe 所在目录。" -ForegroundColor Red
    exit 1
}

$desktop = [Environment]::GetFolderPath("Desktop")
$finalLnk = Join-Path $desktop "期权波动率雷达.lnk"
# 先在纯 ASCII 路径上创建，再重命名，避免非中文区域设置下中文路径被转成 "?"
$tmpLnk = Join-Path $desktop "OptionRadar-Shortcut-Temp.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($tmpLnk)
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.IconLocation = "$exe,0"
$shortcut.Description = "OptionRadar: IV / HV / Skew / GEX"
$shortcut.WindowStyle = 1
$shortcut.Save()

if (Test-Path $tmpLnk) {
    if (Test-Path $finalLnk) { Remove-Item $finalLnk -Force }
    Rename-Item -Path $tmpLnk -NewName "期权波动率雷达.lnk" -Force
}

$lnk = $finalLnk

if (Test-Path $lnk) {
    Write-Host "已创建桌面快捷方式：$lnk" -ForegroundColor Green
} else {
    Write-Host "创建失败，请检查桌面路径权限。" -ForegroundColor Red
    exit 1
}
