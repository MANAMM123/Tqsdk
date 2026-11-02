$ErrorActionPreference = "Stop"

# 把程序安装到当前用户桌面，并创建快捷方式
$src = $PSScriptRoot
$exeName = "OptionRadar.exe"
if (-not (Test-Path (Join-Path $src $exeName))) {
    Write-Host "未找到 OptionRadar.exe，请把本脚本放在程序目录内。" -ForegroundColor Red
    exit 1
}

$desktop = [Environment]::GetFolderPath("Desktop")
$dest = Join-Path $desktop "OptionRadar"

if (-not (Test-Path $dest)) {
    New-Item -ItemType Directory -Path $dest | Out-Null
}

if ($src -ne $dest) {
    Write-Host "正在复制程序到 $dest ..." -ForegroundColor Cyan
    # /E 只覆盖同名文件，不会删除已有的 config.json
    $rc = Start-Process -FilePath "robocopy.exe" `
        -ArgumentList @("`"$src`"", "`"$dest`"", "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS", "/R:2", "/W:2") `
        -Wait -PassThru -NoNewWindow
    if ($rc.ExitCode -ge 8) {
        Write-Host "复制失败（robocopy 退出码 $($rc.ExitCode)）。" -ForegroundColor Red
        exit 1
    }
}

$exe = Join-Path $dest $exeName
$lnk = Join-Path $desktop "期权波动率雷达.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = $dest
$shortcut.IconLocation = "$exe,0"
$shortcut.Description = "期权波动率雷达：四大交易所 IV / HV / 偏度 / GEX"
$shortcut.WindowStyle = 1
$shortcut.Save()

Write-Host "安装完成：$dest" -ForegroundColor Green
Write-Host "桌面快捷方式：$lnk" -ForegroundColor Green

$answer = Read-Host "现在启动程序？(Y/n)"
if ($answer -eq "" -or $answer -match "^[Yy]") {
    Start-Process -FilePath $exe -WorkingDirectory $dest
}
