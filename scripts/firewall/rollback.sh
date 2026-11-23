#!/usr/bin/env bash
# 回滚：删除 INPUT 里的跳转，并清掉 ASSISTANT-* 两条自建链。幂等，需要 root。
set -euo pipefail
IPT="${IPTABLES:-$(command -v iptables || echo /usr/sbin/iptables)}"
if [ "$(id -u)" -ne 0 ]; then echo "需要 root：请用 sudo 执行" >&2; exit 1; fi
while "$IPT" -w -C INPUT -j ASSISTANT-INPUT 2>/dev/null; do "$IPT" -w -D INPUT -j ASSISTANT-INPUT; done
for c in ASSISTANT-INPUT ASSISTANT-FROM-NET; do
  if "$IPT" -w -L "$c" -n >/dev/null 2>&1; then "$IPT" -w -F "$c"; fi
done
for c in ASSISTANT-INPUT ASSISTANT-FROM-NET; do
  if "$IPT" -w -L "$c" -n >/dev/null 2>&1; then "$IPT" -w -X "$c"; fi
done
echo "已回滚：assistant 防火墙规则已移除"
