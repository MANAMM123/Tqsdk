@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 在 Windows 上一键打包：生成 dist\OptionRadar\OptionRadar.exe
rem 需要先安装 Python 3.9 ~ 3.12，并把 python 加入 PATH

pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail

pyinstaller --noconfirm --windowed --name OptionRadar ^
    --collect-submodules scipy._external.array_api_compat ^
    app.py
if errorlevel 1 goto fail

echo.
echo 打包完成：dist\OptionRadar\OptionRadar.exe
echo 把整个 dist\OptionRadar 文件夹拷走即可双击运行。
pause
exit /b 0

:fail
echo.
echo 打包失败，请检查上方报错。
pause
exit /b 1
