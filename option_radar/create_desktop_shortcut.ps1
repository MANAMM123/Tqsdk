$ErrorActionPreference = "Stop"

# 在桌面创建「期权波动率雷达」快捷方式，指向本文件夹内的 OptionRadar.exe
$exe = Join-Path $PSScriptRoot "OptionRadar.exe"
if (-not (Test-Path $exe)) {
    Write-Host "未找到 OptionRadar.exe，请把本脚本放在 exe 所在目录。" -ForegroundColor Red
    exit 1
}

$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "期权波动率雷达.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.IconLocation = "$exe,0"
$shortcut.Description = "期权波动率雷达：四大交易所 IV / HV / 偏度 / GEX"
$shortcut.WindowStyle = 1
$shortcut.Save()

if (Test-Path $lnk) {
    Write-Host "已创建桌面快捷方式：$lnk" -ForegroundColor Green
} else {
    Write-Host "创建失败，请检查桌面路径权限。" -ForegroundColor Red
    exit 1
}
