#!/usr/bin/env bash
# 卸载 assistant 防火墙（需要 root）：停用 unit（ExecStop 会回滚规则）、删除 unit 和脚本。
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "需要 root：请用 sudo 执行" >&2; exit 1; fi
DST=/usr/local/lib/assistant-firewall
systemctl disable --now assistant-firewall.service 2>/dev/null || true
[ -x "$DST/rollback.sh" ] && "$DST/rollback.sh" || true
rm -f /etc/systemd/system/assistant-firewall.service
systemctl daemon-reload
rm -rf "$DST"
echo "已卸载：规则、unit、脚本均已移除"
