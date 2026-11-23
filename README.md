# AI-Assistant

基于 [OpenClaw](https://github.com/openclaw/openclaw) 的自托管私人助手，只服务一个人。你在 Telegram 私聊里和它对话，它也会主动推送提醒。

- **会话管理**：通过 Orca 查看 agent 会话列表和进度，接管会话后由助手代为确认。
- **主动提醒**：应用挂了或恢复、会话在等你确认、任务完成时推送提醒。
- **应用管理**：通过 Workbench 查看应用状态和日志；启动、停止应用需要审批码（默认关闭）。
- **可扩展**：要加新能力，只需添加 skill 和网关接口。

设计细节见 [docs/design.md](docs/design.md)。

## 架构

```
Telegram 私聊 ⇄ openclaw 容器（桥接网络 assistant-net）
                    │  Bearer token
                    ▼
               assistant-gate（host 网络，仅监听 172.28.0.1:8500）
                    ├─ Orca 运行时（固定参数的只读子命令 + 受控 reply）
                    └─ Workbench（只读 GET；启停需审批码且默认关闭）
```

所有会碰到宿主机的操作都要经过 `assistant-gate`。网关负责以下几件事：

| 职责 | 说明 |
|---|---|
| 接口白名单 | 只调用 Orca 和 Workbench 中列入白名单的接口 |
| 危险操作拦截 | 按确定性规则拦截，不依赖模型判断 |
| 一次性审批码 | 码通过 `sendMessage` 直接发到你的私聊，模型看不到 |
| 凭据脱敏 | 处理软换行、ANSI 控制码切断的凭据 |
| 监视器 | 采集和提醒由确定性代码完成 |
| 审计日志 | 记录网关执行过的操作 |

## 目录

| 路径 | 内容 |
|---|---|
| `compose.yaml` | 两个服务：`assistant-openclaw`、`assistant-gate` |
| `gate/` | 网关（Python 3 + FastAPI）及测试 |
| `openclaw/` | OpenClaw 配置模板、SOUL/AGENTS 规则、skills、定时任务的触发脚本 |
| `scripts/` | 初始化、插件安装、配置渲染、隔离验收、定时任务管理、模拟 Workbench |
| `scripts/firewall/` | 宿主机 INPUT 链规则（需 root）及其 netns 测试 |
| `docs/design.md` | 设计文档 |

## 部署

前提：Linux 主机、Docker Compose、宿主机已运行 Orca（`/opt/Orca`），以及一个专用的 Telegram Bot。

1. 运行 `bash scripts/bootstrap.sh`，创建 `secrets/`、`state/`、`gate/data/`（权限 700/600，并去掉继承的 ACL）。
2. 自己把凭据写进 `secrets/`，每个文件只放一行，不要贴到聊天或日志里。网关 token 和控制台 token 由 bootstrap 生成。
   - `telegram_bot_token`
   - `zhipu_api_key`
   - `orca_pairing_code`
3. 运行 `cp .env.example .env`，填写 `TELEGRAM_OWNER_ID` 等配置项。
4. 依次运行以下命令：
   ```
   bash scripts/install-plugins.sh
   python3 scripts/render_config.py
   docker compose up -d
   ```
5. 运行 `bash scripts/verify-isolation.sh` 做隔离验收。
6. 可选：运行 `sudo bash scripts/firewall/install.sh`，只允许容器访问宿主机上的网关端口，详见 [scripts/firewall/README.md](scripts/firewall/README.md)。
7. 运行 `bash scripts/openclaw-jobs.sh create --apply`，创建「纳管会话自动确认」定时任务。

## 测试

```
python3 -m venv .venv && .venv/bin/pip install -r gate/requirements-dev.txt
.venv/bin/python -m pytest gate/tests tests
bash scripts/firewall/test-netns.sh      # 需要 unshare，不需要 root
```

## 安全默认值

- 只接受 `TELEGRAM_OWNER_ID` 本人的私聊；群聊关闭；只发送最终结果。
- 以下操作都要审批码：危险回复、非纳管会话的回复、你点名要求的回复，以及应用启停（启停开关默认 `off`）。
- Orca 的 `computer`、`skills share`、`artifacts share`，以及 account 和 environment 类子命令一律拒绝。
- 控制台只映射到 `127.0.0.1:18789`，需要时用 SSH 隧道访问。
