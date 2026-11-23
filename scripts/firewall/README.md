# assistant-net 防火墙

目的：容器 `assistant-openclaw` 只能访问宿主机上的网关（`172.28.0.1:8500`），不能访问宿主机的其他端口（sshd 22、Orca 6768 等），也不能经宿主机公网 IP "发夹"回来。其他 Docker 网络上的容器也连不到网关。

## 现状（实测）

- 本机 `iptables` 是 **iptables-nft**（nf_tables 后端），Docker 的 Firewall Backend 也是 `iptables`，两者用同一套内核表；我们的规则放在自建链 `ASSISTANT-INPUT`/`ASSISTANT-FROM-NET` 里，只在 INPUT 链最前面加一条跳转，**不修改 Docker 的任何链**。
- 容器访问宿主机自己的地址（172.28.0.1、公网 IP）走 **INPUT** 链，不是 DOCKER-USER/FORWARD。实测：`ip route get 203.0.113.10 from 172.28.0.2 iif br-assistant` 返回 `local ... iif br-assistant`，即发夹流量也是从 `br-assistant` 进入 INPUT。
- `/etc/nftables.conf` 内容是 `flush ruleset`，但 `nftables.service` 未启用；**不要启用它**，否则开机会清空 Docker 的规则。`/etc/iptables/rules.v4` 是旧快照，也没有被加载，不要装 `iptables-persistent` 去恢复它。
- 本方案的持久化靠 systemd unit `assistant-firewall.service`（在 `docker.service` 之后加载，幂等）。
- `assistant-net` 的网桥接口名已固定为 `br-assistant`（compose 里的 `driver_opts`）。

## 规则

```
INPUT:  -j ASSISTANT-INPUT                       （第 1 条）
ASSISTANT-INPUT:
  -i br-assistant -j ASSISTANT-FROM-NET
  ! -i lo -d 172.28.0.1 -p tcp --dport 8500 -j DROP    （其他接口访问网关端口，本机 lo 放行）
ASSISTANT-FROM-NET:
  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  -d 172.28.0.1 -p tcp --dport 8500 -j ACCEPT
  -j DROP
```

不受影响：FORWARD（容器访问外网：Telegram、智谱）、Docker 内置 DNS（上游查询由 dockerd 在宿主机 OUTPUT 方向发出）、网关访问 Orca（host 网络，走 lo/OUTPUT）。

## 执行前先看（不需要 root）

```bash
cd <仓库目录>
bash scripts/firewall/apply.sh --dry-run        # 打印将加载的规则
bash scripts/firewall/test-netns.sh             # 在一次性命名空间里用真实内核规则验证（不碰宿主机）
```

## 安装（需要 root）

```bash
sudo bash <仓库目录>/scripts/firewall/install.sh
```

它会：把脚本复制到 root 属主的 `/usr/local/lib/assistant-firewall/`（避免 root 执行仓库里别人可写的脚本）、安装并启用 `assistant-firewall.service`、立即应用规则。

## 检查

```bash
sudo /usr/local/lib/assistant-firewall/apply.sh --status     # 规则与计数器
cd <仓库目录> && bash scripts/verify-isolation.sh --with-firewall   # 正反两组检查（不需要 root）
```

## 回滚 / 卸载

```bash
sudo /usr/local/lib/assistant-firewall/rollback.sh           # 只移除规则（systemd unit 仍在，重启后会重新应用）
sudo bash <仓库目录>/scripts/firewall/uninstall.sh    # 移除规则、unit、脚本
```

## 注意

- 修改规则后重新执行 `install.sh` 即可（幂等）。
- `systemctl restart docker` 会让本 unit 先停（回滚）再起（重新应用），中间有极短的空窗。
- 只处理 IPv4；`assistant-net` 没有开启 IPv6。
