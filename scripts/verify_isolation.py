#!/usr/bin/env python3
"""隔离验收：静态检查 compose 与渲染后的配置；容器在运行时再做运行期检查。

任何一项失败都打印原因，最终退出码为 1。不打印任何凭据。
"""
import json
import re
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILED = []


def check(ok: bool, name: str, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ((" -> " + detail) if (detail and not ok) else ""))
    if not ok:
        FAILED.append(name)


def sh(args, timeout=60):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    return p.returncode, p.stdout, p.stderr


def parse_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def static_compose() -> None:
    rc, out, err = sh(["docker", "compose", "config", "--format", "json"])
    if rc != 0:
        check(False, "docker compose config 可渲染", err.strip()[:200])
        return
    cfg = json.loads(out)
    svc = cfg.get("services", {}).get("assistant-openclaw")
    check(bool(svc), "compose 里有 assistant-openclaw 服务")
    if not svc:
        return
    ports = svc.get("ports", [])
    check(len(ports) == 1 and ports[0].get("host_ip") == "127.0.0.1" and str(ports[0].get("target")) == "18789",
          "控制台端口只映射到 127.0.0.1:18789", str(ports))
    check(str(svc.get("user")) == "1001:1001", "容器用户为 1001:1001", str(svc.get("user")))
    check(bool(svc.get("mem_limit")), "设置了内存上限")
    check("ALL" in [c.upper() for c in svc.get("cap_drop", [])], "cap_drop 包含 ALL")
    img = svc.get("image", "")
    check(":" in img.rsplit("/", 1)[-1] and not img.endswith(":latest"), "镜像带固定 tag（非 latest）", img)
    vols = [(v.get("source", ""), v.get("target", ""), bool(v.get("read_only"))) for v in svc.get("volumes", [])]
    bad = [v for v in vols if re.search(r"(/opt/Orca|docker\.sock|^/home|^/Github/?$|^/$)", v[0]) or v[1].startswith("/home")]
    check(not bad, "没有挂载 /opt/Orca、/home、/Github、docker.sock", str(bad))
    rw = [v for v in vols if not v[2]]
    check(len(rw) == 1 and rw[0][0].endswith("/state"), "唯一可写挂载是 ./state", str(rw))
    sec = [v for v in vols if "/secrets/" in v[0]]
    check(bool(sec) and all(v[2] for v in sec), "密钥文件均只读挂载")
    check(set(svc.get("networks", {}).keys()) == {"assistant-net"}, "只接入 assistant-net")
    net = cfg.get("networks", {}).get("assistant-net", {})
    check(net.get("name") == "assistant-net" or "assistant-net" in str(net.get("name", "")),
          "网络以 assistant- 开头")
    subnet = (net.get("ipam", {}).get("config") or [{}])[0].get("subnet")
    check(subnet == "172.28.0.0/24", "assistant-net 固定网段 172.28.0.0/24", str(subnet))
    check(all(s.startswith("assistant-") for s in cfg.get("services", {})), "所有服务名以 assistant- 开头")


def static_gate() -> None:
    rc, out, err = sh(["docker", "compose", "config", "--format", "json"])
    if rc != 0:
        return
    cfg = json.loads(out)
    gate = cfg.get("services", {}).get("assistant-gate")
    check(bool(gate), "compose 里有 assistant-gate 服务")
    if not gate:
        return
    check(gate.get("network_mode") == "host", "网关 network_mode: host", str(gate.get("network_mode")))
    vols = [(v.get("source", ""), v.get("target", ""), bool(v.get("read_only"))) for v in gate.get("volumes", [])]
    orca = [v for v in vols if v[0] == "/opt/Orca"]
    check(len(orca) == 1 and orca[0][2], "/opt/Orca 只读挂载到网关")
    check(any(v[0].endswith("/gate/data") and not v[2] for v in vols), "网关数据卷 ./gate/data 存在")
    check(not any("docker.sock" in v[0] for v in vols), "网关没有 docker.sock 挂载")
    srcs = " ".join(v[0] for v in vols)
    check("zhipu_api_key" not in srcs and "openclaw_gateway_token" not in srcs,
          "网关不持有智谱 key 和 OpenClaw 控制台密钥")
    check("telegram_bot_token" in srcs and any(v[2] for v in vols if "telegram_bot_token" in v[0]),
          "网关以只读方式持有 Telegram Bot token（只发通知）")
    genv = gate.get("environment", {})
    check(genv.get("GATE_NOTIFY_MODE") in ("dry-run", "live"), "网关通知模式已声明", str(genv.get("GATE_NOTIFY_MODE")))
    check(not any(re.search(r"\d{8,10}:[A-Za-z0-9_-]{35}", str(v)) for v in genv.values()), "网关环境变量里没有 token 明文")
    check(str(genv.get("GATE_APP_ACTIONS", "off")).lower() in ("off", "0", "false", "no"),
          "应用启停开关为 off（对真实 Workbench 不发写调用）", str(genv.get("GATE_APP_ACTIONS")))
    check(genv.get("GATE_USER_DIRECTED_REPLY", "approval") == "approval", "用户当次要求的回复走审批码（approval）", str(genv.get("GATE_USER_DIRECTED_REPLY")))
    check("orca_pairing_code" in srcs and "gate_token" in srcs, "网关持有 Orca 配对码和网关 token")
    check(gate.get("environment", {}).get("GATE_BIND_HOST") == "172.28.0.1", "网关只绑定 172.28.0.1",
          str(gate.get("environment", {}).get("GATE_BIND_HOST")))
    img = gate.get("image", "")
    check(":" in img and not img.endswith(":latest"), "网关镜像带固定 tag", img)
    check(str(gate.get("user")) == "1001:1001", "网关以非 root（1001）运行")
    oc = cfg.get("services", {}).get("assistant-openclaw", {})
    oc_srcs = " ".join(v.get("source", "") for v in oc.get("volumes", []))
    check("orca_pairing_code" not in oc_srcs and "/gate/data" not in oc_srcs and "/opt/Orca" not in oc_srcs,
          "openclaw 容器看不到 Orca 配对码和网关数据卷")
    keys = " ".join(oc.get("environment", {}).keys()).upper()
    check("ORCA" not in keys, "openclaw 容器环境变量里没有 Orca 相关项")


def static_gate_source() -> None:
    """网关只发不收：源码里不得出现收消息 / webhook 相关的 Telegram 方法名。"""
    forbidden = ("get" + "Updates", "set" + "Webhook", "delete" + "Webhook")
    hits = [str(p.relative_to(ROOT)) for p in (ROOT / "gate/app").rglob("*.py")
            if any(w in p.read_text(encoding="utf-8") for w in forbidden)]
    check(not hits, "网关源码不含收消息 / webhook 方法（只发不收）", str(hits))


def static_config() -> None:
    p = ROOT / "state/openclaw.json"
    if not p.exists():
        check(False, "state/openclaw.json 已渲染（先运行 scripts/render_config.py）")
        return
    raw = p.read_text()
    cfg = json.loads(raw)
    tg = cfg.get("channels", {}).get("telegram", {})
    owner = parse_env(ROOT / ".env").get("TELEGRAM_OWNER_ID", "")
    check(tg.get("dmPolicy") == "allowlist", "dmPolicy == allowlist", str(tg.get("dmPolicy")))
    check(len(tg.get("allowFrom", [])) == 1 and str(tg["allowFrom"][0]) == owner and bool(owner),
          "allowFrom 恰好一个且等于 TELEGRAM_OWNER_ID")
    check(tg.get("groupPolicy") == "disabled", "groupPolicy == disabled", str(tg.get("groupPolicy")))
    check("groups" not in tg, "没有群聊配置")
    check("dangerouslyDisableDeviceAuth" not in raw and "allowInsecureAuth" not in raw,
          "不含 dangerouslyDisableDeviceAuth / allowInsecureAuth")
    check(cfg.get("security", {}).get("installPolicy", {}).get("enabled") is True
          and "exec" not in cfg["security"]["installPolicy"], "installPolicy 启用且无放行命令（拦截一切安装）")
    # 明文凭据残留检查：配置里不能出现 token 形式的字符串
    check(not re.search(r"\d{8,10}:[A-Za-z0-9_-]{35}", raw) and "sk-" not in raw, "配置里没有明文凭据")


def static_secrets() -> None:
    for name in ("telegram_bot_token", "zhipu_api_key", "openclaw_gateway_token", "gate_token", "orca_pairing_code"):
        f = ROOT / "secrets" / name
        ok = f.is_file() and not f.is_symlink() and (f.stat().st_mode & 0o777) == 0o600
        check(ok, f"secrets/{name} 是 600 的普通文件")
    f = ROOT / "secrets/telegram_bot_token"
    if f.is_file():
        lines = [l for l in f.read_text().splitlines() if l.strip()]
        check(len(lines) == 1 and re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", lines[0].strip()) is not None,
              "telegram_bot_token 只有一行 token（tokenFile 要求）")
    d = ROOT / "secrets"
    check((d.stat().st_mode & 0o777) == 0o700, "secrets/ 目录权限 700")
    rc, out, _ = sh(["getfacl", "-p", "secrets", "state"])
    check("group:agents" not in out and "default:" not in out, "secrets/ state/ 无继承 ACL")


def gate_runtime(ex) -> None:
    rc, out, _ = sh(["docker", "ps", "--filter", "name=^assistant-gate$", "--format", "{{.Names}}"])
    if "assistant-gate" not in out:
        check(False, "assistant-gate 在运行")
        return
    rc, out, _ = sh(["ss", "-ltn"])
    listens = [l.split()[3] for l in out.splitlines() if l.startswith("LISTEN") and l.split()[3].endswith(":8500")]
    check(listens == ["172.28.0.1:8500"], "宿主机上 8500 只监听 172.28.0.1", str(listens))
    base = "http://172.28.0.1:8500"
    rc, out, _ = ex(f"curl -m 5 -s -o /dev/null -w '%{{http_code}}' {base}/sessions")
    check(out.strip() == "401", "openclaw 容器不带 token 访问网关 -> 401", out.strip())
    rc, out, _ = ex(f"curl -m 5 -s -o /dev/null -w '%{{http_code}}' -H 'Authorization: Bearer wrong' {base}/sessions")
    check(out.strip() == "401", "错误 token -> 401", out.strip())
    token = (ROOT / "secrets/gate_token").read_text().strip()
    # token 经 stdin 传给 curl（-H @-），不出现在进程参数和输出里
    p = subprocess.run(["docker", "exec", "-i", "assistant-openclaw", "sh", "-c",
                        f"curl -m 20 -s -o /dev/null -w '%{{http_code}}' -H @- {base}/healthz"],
                       input=f"Authorization: Bearer {token}\n", capture_output=True, text=True, timeout=40)
    check(p.stdout.strip() == "200", "带 token 访问 /healthz -> 200", p.stdout.strip())
    p = subprocess.run(["docker", "exec", "-i", "assistant-openclaw", "sh", "-c",
                        f"curl -m 40 -s -o /dev/null -w '%{{http_code}}' -H @- {base}/worktrees"],
                       input=f"Authorization: Bearer {token}\n", capture_output=True, text=True, timeout=60)
    check(p.stdout.strip() == "200", "带 token 访问 /worktrees（真实 Orca）-> 200", p.stdout.strip())
    rc, out, _ = sh(["docker", "exec", "assistant-gate", "sh", "-c",
                     "env | grep -ciE 'ZHIPU|ZAI_|OPENCLAW|[0-9]{8,10}:[A-Za-z0-9_-]{35}' || true"])
    check(out.strip() == "0", "网关容器环境变量里没有智谱、OpenClaw 凭据和 token 明文", out.strip())
    check(ex("ls /data /srv/gate")[0] != 0, "openclaw 容器里看不到网关数据卷和网关代码")
    p = subprocess.run(["docker", "exec", "-i", "assistant-openclaw", "sh", "-c",
                        f"curl -m 20 -s -o /dev/null -w '%{{http_code}}' -H @- {base}/apps/info/stop -X POST"],
                       input=f"Authorization: Bearer {token}\n", capture_output=True, text=True, timeout=40)
    check(p.stdout.strip() == "403", "应用启停关着：POST /apps/info/stop -> 403（不建审批单、不碰 Workbench）", p.stdout.strip())
    check(ex("curl -m 3 -s -o /dev/null http://127.0.0.1:8500/healthz")[0] != 0, "容器内 127.0.0.1:8500 不是网关（网关不在容器网络里）")


def public_ip() -> str:
    rc, out, _ = sh(["ip", "-4", "route", "get", "1.1.1.1"])
    m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


def firewall() -> None:
    """防火墙正反两组检查（不需要 root）：该通的通，该拒的拒。用户执行 scripts/firewall/install.sh 之后再跑。"""
    rc, out, _ = sh(["docker", "ps", "--filter", "name=^assistant-openclaw$", "--format", "{{.Names}}"])
    if "assistant-openclaw" not in out:
        check(False, "防火墙检查需要 assistant-openclaw 在运行")
        return
    check(sh(["ip", "link", "show", "br-assistant"])[0] == 0, "网桥接口名固定为 br-assistant")
    pub = public_ip()
    check(bool(pub), "能确定宿主机公网 IP", pub)
    ex = lambda cmd: sh(["docker", "exec", "assistant-openclaw", "bash", "-c", cmd], timeout=30)
    tcp = lambda host, port: ex(f"timeout 4 bash -c 'exec 3<>/dev/tcp/{host}/{port}'")[0]
    # ---- 该拒的拒 ----
    check(tcp("172.28.0.1", 22) != 0, "[拒] 容器 -> 宿主机 172.28.0.1:22")
    check(tcp("172.28.0.1", 6768) != 0, "[拒] 容器 -> 宿主机 172.28.0.1:6768（Orca）")
    if pub:
        check(tcp(pub, 22) != 0, f"[拒] 容器 -> 宿主机公网 IP:22（发夹）")
        check(tcp(pub, 6768) != 0, f"[拒] 容器 -> 宿主机公网 IP:6768（发夹）")
    rc, out, _ = sh(["docker", "run", "--rm", "--name", "assistant-spike-other", "--entrypoint", "bash",
                     "ghcr.io/openclaw/openclaw:2026.11.6", "-c",
                     "timeout 4 bash -c 'exec 3<>/dev/tcp/172.28.0.1/8500'"], timeout=60)
    check(rc != 0, "[拒] 默认 bridge（docker0）上的别的容器 -> 网关 172.28.0.1:8500")
    # ---- 该通的通 ----
    check(tcp("172.28.0.1", 8500) == 0, "[通] 容器 -> 网关 172.28.0.1:8500")
    rc, out, _ = ex("curl -m 8 -s -o /dev/null -w '%{http_code}' https://api.telegram.org/")
    check(out.strip() not in ("", "000"), "[通] 容器 -> 外网（api.telegram.org，Telegram 可达）", out.strip())
    rc, out, _ = ex("curl -m 8 -s -o /dev/null -w '%{http_code}' https://open.bigmodel.cn/")
    check(out.strip() not in ("", "000"), "[通] 容器 -> 外网（open.bigmodel.cn，智谱可达）", out.strip())
    check(ex("getent hosts api.telegram.org")[0] == 0, "[通] 容器内 Docker 内置 DNS 解析正常")
    try:
        import urllib.request
        code = urllib.request.urlopen("http://172.28.0.1:8500/healthz", timeout=5).status
    except Exception as e:  # noqa: BLE001
        code = type(e).__name__
    check(code == 200, "[通] 宿主机本机进程 -> 网关 /healthz", str(code))
    token = (ROOT / "secrets/gate_token").read_text().strip()
    p = subprocess.run(["docker", "exec", "-i", "assistant-openclaw", "sh", "-c",
                        "curl -m 40 -s -o /dev/null -w '%{http_code}' -H @- http://172.28.0.1:8500/worktrees"],
                       input=f"Authorization: Bearer {token}\n", capture_output=True, text=True, timeout=60)
    check(p.stdout.strip() == "200", "[通] 网关 -> 真实 Orca（经 /worktrees）", p.stdout.strip())


def runtime() -> None:
    rc, out, _ = sh(["docker", "ps", "--filter", "name=^assistant-openclaw$", "--format", "{{.Names}}"])
    if "assistant-openclaw" not in out:
        print("SKIP  assistant-openclaw 未运行，跳过运行期检查")
        return
    ex = lambda cmd: sh(["docker", "exec", "assistant-openclaw", "sh", "-c", cmd])
    check(ex("ls /opt/Orca")[0] != 0, "容器内看不到 /opt/Orca")
    check(ex("ls /Github")[0] != 0, "容器内看不到 /Github")
    check(ex("ls /var/run/docker.sock")[0] != 0, "容器内没有 docker.sock")
    check(ex("test -w /run/secrets/telegram_bot_token")[0] != 0, "密钥文件在容器内只读")
    # 宿主机回环端口不可达：起一个临时监听器（18400，不是 Workbench 的 8400）
    srv = HTTPServer(("127.0.0.1", 18400), BaseHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for host in ("172.28.0.1", "host.docker.internal"):
            rc, _, _ = ex(f"curl -m 3 -s -o /dev/null http://{host}:18400/")
            check(rc != 0, f"容器访问宿主机回环端口 {host}:18400 不通")
    finally:
        srv.shutdown()
    gate_runtime(ex)
    rc, out, _ = sh(["docker", "exec", "assistant-openclaw", "id", "-u"])
    check(out.strip() == "1001", "容器内进程 uid 为 1001", out.strip())


if __name__ == "__main__":
    with_fw = "--with-firewall" in sys.argv[1:]
    static_compose()
    static_gate()
    static_gate_source()
    static_config()
    static_secrets()
    runtime()
    if with_fw:
        firewall()
    else:
        print("SKIP  防火墙检查（用户执行 scripts/firewall/install.sh 后，加 --with-firewall 运行）")
    print("=" * 40)
    print("全部通过" if not FAILED else f"失败 {len(FAILED)} 项")
    sys.exit(1 if FAILED else 0)
