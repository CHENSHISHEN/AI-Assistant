---
name: apps
description: 查看用户登记的应用（Workbench）的状态和日志；用户要求启动、停止、重启某个应用时发起审批。应用启停始终需要用户审批码，且目前可能处于关闭状态。
---

# apps：应用状态、日志与启停

```sh
G=/opt/assistant/skills/session-confirm/scripts/gate.sh
$G apps                        # 应用列表和状态（带采集时间）
$G app-logs <应用id> [tail]     # 日志（已脱敏）
$G app-act <应用id> restart    # 发起启停审批：start | stop | restart
```

## 怎么回答

- 状态和日志回答里带上 `collected_at`（换算成东八区）。Workbench 不可达（503 `workbench_unreachable`）就如实说"现在看不到应用状态"，不要编造。
- **启停永远需要用户审批，不要擅自重启。** 应用挂了只通知用户（网关会推送），用户明确要求才发起。
- `app-act` 返回 `403 app_actions_disabled`：说"应用启停功能目前是关闭的"，不要换办法。
- 返回 `approval_required`：告诉用户审批单已发到私聊，等他回复『批准 <ID> <码>』。不要重复创建，不要猜码。
