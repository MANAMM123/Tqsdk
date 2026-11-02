@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 双击本文件即可在桌面生成「期权波动率雷达」快捷方式
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_desktop_shortcut.ps1"

echo.
pause
