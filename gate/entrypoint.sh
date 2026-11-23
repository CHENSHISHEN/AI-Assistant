#!/bin/sh
# 启动顺序：1) 等桥接网关地址出现  2) 用配对码文件登记 Orca 环境（每次启动重新登记，
# 登记信息只放在容器的 tmpfs 里，换配对码 = 替换 secrets/orca_pairing_code 后重启）  3) 启动 uvicorn。
set -eu
: "${GATE_BIND_HOST:?需要 GATE_BIND_HOST}"
: "${GATE_ORCA_ENVIRONMENT:?需要 GATE_ORCA_ENVIRONMENT}"
: "${GATE_PAIRING_FILE:=/run/secrets/orca_pairing_code}"

i=0
until ip_ok=$(python3 -c "
import socket,sys
s=socket.socket()
try:
    s.bind(('$GATE_BIND_HOST',0)); print('ok')
except OSError:
    sys.exit(1)
finally:
    s.close()
" 2>/dev/null); do
  i=$((i+1)); [ "$i" -gt 60 ] && { echo "等待 $GATE_BIND_HOST 超时" >&2; exit 1; }
  sleep 2
done

PAIRING="$(tr -d '\r\n ' < "$GATE_PAIRING_FILE")"
[ -n "$PAIRING" ] || { echo "配对码文件为空" >&2; exit 1; }
if ! orca environment add --name "$GATE_ORCA_ENVIRONMENT" --pairing-code "$PAIRING" --json >/tmp/orca-add.out 2>&1; then
  echo "orca environment add 失败（详情不输出，避免泄露配对信息）" >&2
  exit 1
fi
unset PAIRING

exec /opt/venv/bin/uvicorn --factory app.main:app_from_env \
  --host "$GATE_BIND_HOST" --port "${GATE_BIND_PORT:-8500}" --no-access-log
