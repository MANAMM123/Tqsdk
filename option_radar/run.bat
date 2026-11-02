@echo off
chcp 65001 >nul
cd /d "%~dp0"

python -c "import tqsdk, pandas, numpy, matplotlib" >nul 2>&1
if errorlevel 1 (
    echo 首次运行，正在安装依赖...
    python -m pip install -r requirements.txt
)

rem 可选：在此设置账号密码，避免每次弹窗输入
rem set TQ_USERNAME=13923955955
rem set TQ_PASSWORD=你的密码

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw app.py
) else (
    python app.py
)
