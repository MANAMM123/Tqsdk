@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 双击本文件：把程序复制到桌面并创建快捷方式
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_to_desktop.ps1"

echo.
pause
