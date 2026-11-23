#!/usr/bin/env bash
# 在一次性的用户+网络命名空间里，用真实内核 netfilter 验证 apply.sh/rollback.sh 的规则逻辑。
# 不需要 root，不碰宿主机的任何规则（所有接口、规则都只存在于这个命名空间里，退出即消失）。
#   用法: bash scripts/firewall/test-netns.sh
set -u
export PATH="$PATH:/usr/sbin:/sbin"
if [ "${INSIDE_NETNS:-0}" != "1" ]; then
  exec unshare -Urn env INSIDE_NETNS=1 ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)" bash "$0"
fi
cd "$ROOT_DIR"
FAILS=0
ok()  { echo "PASS  $1"; }
bad() { echo "FAIL  $1"; FAILS=$((FAILS+1)); }
expect() {  # expect <ok|block> <netns-pid|host> <ip> <port> <说明>
  local want="$1" who="$2" ip="$3" port="$4" desc="$5" rc
  local cmd="import socket,sys; s=socket.socket(); s.settimeout(1.5); s.connect(('$ip',$port))"
  if [ "$who" = host ]; then python3 -c "$cmd" 2>/dev/null; rc=$?; else nsenter -t "$who" -n python3 -c "$cmd" 2>/dev/null; rc=$?; fi
  if [ "$want" = ok ]; then [ $rc -eq 0 ] && ok "$desc" || bad "$desc（期望通，实际不通）"; else [ $rc -ne 0 ] && ok "$desc" || bad "$desc（期望拒绝，实际通了）"; fi
}
mkns() {  # mkns <名字> -> 全局变量 PID_<名字>
  unshare -n sleep 600 & eval "PID_$1=$!"; sleep 0.2
}
link() {  # link <宿主端> <容器端> <pid> <宿主 ip/掩码> <容器 ip/掩码> [网关]
  ip link add "$1" type veth peer name "$2"; ip link set "$2" netns "$3"
  ip addr add "$4" dev "$1"; ip link set "$1" up
  nsenter -t "$3" -n sh -c "ip link set lo up; ip addr add $5 dev $2; ip link set $2 up${6:+; ip route add default via $6}"
}
ip link set lo up
echo 1 > /proc/sys/net/ipv4/ip_forward
mkns c1; mkns c2; mkns c3; mkns ext
link br-assistant c1e "$PID_c1" 172.28.0.1/24 172.28.0.2/24 172.28.0.1      # 模拟 assistant-net 里的 openclaw 容器
link docker0      c2e "$PID_c2" 172.17.0.1/16 172.17.0.2/16 172.17.0.1      # 模拟默认 bridge 上的别的容器
link br-other     c3e "$PID_c3" 172.30.0.1/24 172.30.0.2/24 172.30.0.1      # 模拟别的 compose 网络
link eth1         exte "$PID_ext" 203.0.113.1/24 203.0.113.9/24 203.0.113.1 # 模拟外网
ip link add eth0 type dummy; ip addr add 203.0.113.10/32 dev eth0; ip link set eth0 up   # 模拟宿主机公网 IP
# 宿主机上的"服务"：8500 只绑 172.28.0.1（网关），22 和 6768 绑 0.0.0.0（sshd、Orca）
python3 - <<'PY' &
import socket, threading, time
def serve(host, port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind((host, port)); s.listen(16)
    while True:
        c, _ = s.accept(); c.close()
for h, p in (("172.28.0.1", 8500), ("0.0.0.0", 22), ("0.0.0.0", 6768)):
    threading.Thread(target=serve, args=(h, p), daemon=True).start()
time.sleep(600)
PY
SRV=$!; sleep 0.7
EXT_IP=203.0.113.9
nsenter -t "$PID_ext" -n python3 -c "
import socket,threading,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('$EXT_IP',443)); s.listen(8)
def loop():
    while True:
        c,_=s.accept(); c.close()
threading.Thread(target=loop,daemon=True).start(); time.sleep(600)" &
sleep 0.5

echo "== 对照组：未加规则，应全通 =="
expect ok "$PID_c1" 172.28.0.1 8500 "对照：容器 -> 网关 8500"
expect ok "$PID_c1" 172.28.0.1 22   "对照：容器 -> 宿主机 22"
expect ok "$PID_c1" 203.0.113.10 6768 "对照：容器 -> 宿主机公网 IP:6768（发夹）"
expect ok "$PID_c2" 172.28.0.1 8500 "对照：其他网桥容器 -> 网关 8500"

echo "== 应用规则 =="
bash scripts/firewall/apply.sh apply >/dev/null || bad "apply.sh 执行失败"
bash scripts/firewall/apply.sh apply >/dev/null || bad "apply.sh 第二次执行失败（幂等）"
n=$(iptables -S INPUT | grep -c -- '-j ASSISTANT-INPUT'); [ "$n" = 1 ] && ok "幂等：INPUT 里只有 1 条跳转" || bad "INPUT 里跳转条数为 $n"
[ "$(iptables -S INPUT | sed -n 2p)" = "-A INPUT -j ASSISTANT-INPUT" ] && ok "跳转位于 INPUT 第 1 条" || bad "跳转不在第 1 条"
n=$(iptables -S ASSISTANT-FROM-NET | grep -c '^-A'); [ "$n" = 3 ] && ok "幂等：ASSISTANT-FROM-NET 恰好 3 条规则" || bad "ASSISTANT-FROM-NET 规则数 $n"

echo "== 该通的通 =="
expect ok "$PID_c1" 172.28.0.1 8500 "容器 -> 网关 8500"
expect ok host 172.28.0.1 8500 "宿主机本机进程 -> 网关 8500（走 lo）"
expect ok host 203.0.113.10 6768 "宿主机本机进程 -> 公网 IP:6768（网关访问 Orca，host 网络）"
expect ok "$PID_c1" "$EXT_IP" 443 "容器 -> 外网（FORWARD 不受影响）"
echo "== 该拒的拒 =="
expect block "$PID_c1" 172.28.0.1 22 "容器 -> 宿主机 22"
expect block "$PID_c1" 172.28.0.1 6768 "容器 -> 宿主机 6768（经桥地址）"
expect block "$PID_c1" 203.0.113.10 6768 "容器 -> 宿主机公网 IP:6768（发夹）"
expect block "$PID_c1" 203.0.113.10 22 "容器 -> 宿主机公网 IP:22（发夹）"
expect block "$PID_c2" 172.28.0.1 8500 "docker0 上的容器 -> 网关 8500"
expect block "$PID_c3" 172.28.0.1 8500 "别的 br-* 上的容器 -> 网关 8500"
echo "== 计数器：发夹流量确实是从 br-assistant 进 INPUT、被 ASSISTANT-FROM-NET 的 DROP 命中 =="
drops=$(iptables -L ASSISTANT-FROM-NET -nv -x | awk '$3=="DROP"{print $1}')
[ "${drops:-0}" -gt 0 ] && ok "ASSISTANT-FROM-NET 的 DROP 计数器 = $drops（>0）" || bad "DROP 计数器为 0"

echo "== 回滚 =="
bash scripts/firewall/rollback.sh >/dev/null || bad "rollback.sh 执行失败"
bash scripts/firewall/rollback.sh >/dev/null || bad "rollback.sh 第二次执行失败（幂等）"
iptables -S | grep -q ASSISTANT && bad "回滚后仍有 ASSISTANT 规则/链" || ok "回滚后没有任何 ASSISTANT 规则/链"
expect ok "$PID_c1" 172.28.0.1 22 "回滚后：容器 -> 宿主机 22 恢复"
expect ok "$PID_c2" 172.28.0.1 8500 "回滚后：其他网桥容器 -> 网关 恢复"

kill $(jobs -p) 2>/dev/null; kill "$PID_c1" "$PID_c2" "$PID_c3" "$PID_ext" 2>/dev/null
echo "======"; [ $FAILS -eq 0 ] && echo "全部通过" || echo "失败 $FAILS 项"; exit $([ $FAILS -eq 0 ] && echo 0 || echo 1)
