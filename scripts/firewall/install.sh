#!/usr/bin/env bash
# 安装并启用 assistant 防火墙（需要 root）：
# 把脚本复制到 root 属主的目录（避免 root 执行仓库里别人可写的脚本），装 systemd unit，开机自启并立即应用。
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "需要 root：请用 sudo 执行" >&2; exit 1; fi
SRC="$(cd "$(dirname "$0")" && pwd)"
DST=/usr/local/lib/assistant-firewall
install -d -m 755 -o root -g root "$DST"
install -m 755 -o root -g root "$SRC/apply.sh" "$SRC/rollback.sh" "$DST/"
install -m 644 -o root -g root "$SRC/assistant-firewall.service" /etc/systemd/system/assistant-firewall.service
systemctl daemon-reload
systemctl enable --now assistant-firewall.service
echo "---- 状态 ----"
systemctl --no-pager --full status assistant-firewall.service | head -8 || true
"$DST/apply.sh" --status
