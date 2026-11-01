#!/usr/bin/env bash
# 重建 cron 与期权波动率定时任务。
# 沙箱系统层重置后会清掉 cron / .venv / .secrets，重置后跑一次本脚本即可恢复。
#
# 用法:
#   bash /workspace/setup_cron.sh
# 若 /workspace/.secrets/tqsdk.env 也丢了，需要先导出凭据再跑:
#   TQ_USER=xxx TQ_PASS=xxx bash /workspace/setup_cron.sh
set -euo pipefail

VENV=/workspace/.venv
PY=$VENV/bin/python
AUTH=/workspace/.secrets/tqsdk.env
SKILL_SCRIPT=/workspace/.trae/skills/czce-option-vol-monitor/scripts/vol_monitor.py
PROXY=http://127.0.0.1:18080

# 1) Python 依赖
if [ ! -x "$PY" ]; then
  /root/.pyenv/versions/3.12.15/bin/python -m venv "$VENV"
  "$VENV/bin/pip" install -q tqsdk numpy
fi

# 2) 凭据
if [ ! -f "$AUTH" ]; then
  if [ -n "${TQ_USER:-}" ] && [ -n "${TQ_PASS:-}" ]; then
    mkdir -p "$(dirname "$AUTH")"
    printf 'TQ_USER=%s\nTQ_PASS=%s\n' "$TQ_USER" "$TQ_PASS" > "$AUTH"
    chmod 600 "$AUTH"
  else
    echo "缺少 $AUTH，且未设置 TQ_USER/TQ_PASS" >&2
    exit 1
  fi
fi

# 3) cron 服务（容器无 systemd，直接起二进制）
if ! command -v crontab >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq cron
fi
pgrep -x cron >/dev/null 2>&1 || /usr/sbin/cron

# 4) 定时任务：每交易日 15:05 + 11 月 2 日 09:10
CMD="cd /workspace && TZ=Asia/Shanghai HTTP_PROXY=$PROXY HTTPS_PROXY=$PROXY $PY $SKILL_SCRIPT --underlying CZCE.MA612,CZCE.TA612 --auth-file $AUTH --log /workspace/ma612_vol_log.csv >> /workspace/vol.log 2>&1"
{
  crontab -l 2>/dev/null | grep -v "vol_monitor.py" || true
  echo "TZ=Asia/Shanghai; 5 15 * * 1-5 $CMD"
  echo "10 9 2 11 * $CMD"
} | crontab -

echo "--- crontab ---"
crontab -l
echo "--- cron process ---"
pgrep -a cron
