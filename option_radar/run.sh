#!/usr/bin/env bash
# Linux / macOS 桌面启动器：双击或在终端运行
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"

if ! "$PY" -c "import tqsdk, pandas, numpy, matplotlib" >/dev/null 2>&1; then
    echo "首次运行，正在安装依赖…"
    "$PY" -m pip install -r requirements.txt
fi

exec "$PY" app.py
