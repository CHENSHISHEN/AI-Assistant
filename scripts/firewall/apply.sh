#!/usr/bin/env bash
# assistant-net 的防火墙规则（宿主机 INPUT 链）。幂等：可以重复执行。
#
# 为什么是 INPUT 链：容器访问宿主机"自己"的地址（172.28.0.1、公网 IP）时，路由结果是 local，
# 数据包经过的是 INPUT，不经过 FORWARD/DOCKER-USER。已实测：
#   ip route get <公网IP> from 172.28.0.2 iif br-assistant  ->  local ... dev lo, iif br-assistant
# 所以发夹流量（容器 -> 宿主机公网 IP）也是从 br-assistant 进入 INPUT，按接口名一条规则就能管住。
#
# 目标：
#   1. 从 br-assistant 进来、访问宿主机本身的流量：只放行 ESTABLISHED/RELATED 和到 172.28.0.1 的 tcp 8500，其余 DROP
#      （含 22、6768、以及发夹到公网 IP 的流量）。
#   2. 别的接口（docker0、其他 br-*、网卡）访问 172.28.0.1:8500：DROP（本机进程走 lo，不受影响）。
#   3. 不动 FORWARD：容器访问外网（Telegram、智谱）和 Docker 内置 DNS（dockerd 在宿主机 OUTPUT 方向发起上游查询）不受影响。
#   4. 网关走 host 网络，访问 Orca 的流量走 lo/OUTPUT，不受影响。
#
# 用法：
#   apply.sh --dry-run   只打印将要加载的规则（不需要 root）
#   sudo apply.sh        应用
#   apply.sh --status    查看规则和计数器（需要 root）
# 环境变量：ASSISTANT_BRIDGE（默认 br-assistant）、ASSISTANT_GATE_IP（默认 172.28.0.1）、ASSISTANT_GATE_PORT（默认 8500）、IPTABLES（默认 iptables）
set -euo pipefail

BRIDGE="${ASSISTANT_BRIDGE:-br-assistant}"
GATE_IP="${ASSISTANT_GATE_IP:-172.28.0.1}"
GATE_PORT="${ASSISTANT_GATE_PORT:-8500}"
IPT="${IPTABLES:-$(command -v iptables || echo /usr/sbin/iptables)}"
RESTORE="${IPTABLES_RESTORE:-$(command -v iptables-restore || echo /usr/sbin/iptables-restore)}"

ruleset() {
cat <<RULES
*filter
:ASSISTANT-INPUT - [0:0]
:ASSISTANT-FROM-NET - [0:0]
-F ASSISTANT-INPUT
-F ASSISTANT-FROM-NET
# 来自 assistant-net 的流量：只放行已建立连接和到网关的 tcp ${GATE_PORT}，其余 DROP
-A ASSISTANT-FROM-NET -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
-A ASSISTANT-FROM-NET -d ${GATE_IP}/32 -p tcp -m tcp --dport ${GATE_PORT} -j ACCEPT
-A ASSISTANT-FROM-NET -j DROP
-A ASSISTANT-INPUT -i ${BRIDGE} -j ASSISTANT-FROM-NET
# 其他任何接口（docker0、别的 br-*、网卡）访问网关端口：DROP；本机进程走 lo，放行
-A ASSISTANT-INPUT ! -i lo -d ${GATE_IP}/32 -p tcp -m tcp --dport ${GATE_PORT} -j DROP
COMMIT
RULES
}

case "${1:-apply}" in
  --dry-run|dry-run)
    echo "# ---- 将通过 iptables-restore --noflush 原子加载（只触及 ASSISTANT-* 两条自建链）----"
    ruleset
    echo "# ---- 并确保 INPUT 链第 1 条是： -A INPUT -j ASSISTANT-INPUT （没有则插入）----"
    ;;
  --status|status)
    "$IPT" -w -S INPUT | head -5
    "$IPT" -w -L ASSISTANT-INPUT -nv --line-numbers
    "$IPT" -w -L ASSISTANT-FROM-NET -nv --line-numbers
    ;;
  apply|--apply)
    if [ "$(id -u)" -ne 0 ]; then echo "需要 root：请用 sudo 执行" >&2; exit 1; fi
    ruleset | "$RESTORE" --noflush --wait
    # INPUT 第 1 条必须是跳转；没有就插入；有多余的重复跳转就删掉
    first="$("$IPT" -w -S INPUT | sed -n '2p')"
    if [ "$first" != "-A INPUT -j ASSISTANT-INPUT" ]; then
      while "$IPT" -w -C INPUT -j ASSISTANT-INPUT 2>/dev/null; do "$IPT" -w -D INPUT -j ASSISTANT-INPUT; done
      "$IPT" -w -I INPUT 1 -j ASSISTANT-INPUT
    fi
    echo "已应用：INPUT -> ASSISTANT-INPUT（接口 ${BRIDGE}，网关 ${GATE_IP}:${GATE_PORT}）"
    ;;
  *) echo "用法: $0 [--dry-run|--status|apply]" >&2; exit 2 ;;
esac
