#!/usr/bin/env bash
# 创建/删除/查看 OpenClaw 定时任务（任务保存在 OpenClaw 状态库里，不在配置模板里）。
# 默认只打印将要执行的命令（dry-run）；加 --apply 才真正执行。
#   scripts/openclaw-jobs.sh create [--apply]   每 3 分钟检查一次；只有出现新的待确认纳管会话才唤醒模型
#   scripts/openclaw-jobs.sh delete [--apply]
#   scripts/openclaw-jobs.sh list
set -euo pipefail
cd "$(dirname "$0")/.."
NAME=assistant-pending-confirm
APPLY=0; [ "${2:-}" = "--apply" ] && APPLY=1
OC=(docker exec -i assistant-openclaw openclaw automations)
MSG='有纳管会话在等确认超过 5 分钟。按 session-confirm skill 处理：先 pending 取列表；只对明显安全的常规确认回复很短的文本；拿不准、涉及删除/覆盖/强推/改权限/数据库写删/停服务/改凭据的一律不要自己批，什么都不做；返回 approval_required 就等用户批准，不要重复创建。不要发任何多余的消息。'
case "${1:-}" in
  create)
    CMD=("${OC[@]}" create --name "$NAME" --declaration-key "$NAME" --every 3m --session isolated --no-deliver
         --tools exec,read --timeout-seconds 180 --trigger-script - --message "$MSG")
    echo "将执行：${CMD[*]}  < openclaw/jobs/pending-confirm.trigger.js"
    if [ $APPLY -eq 1 ]; then "${CMD[@]}" < openclaw/jobs/pending-confirm.trigger.js; else echo "（dry-run，未执行；加 --apply 才创建）"; fi ;;
  delete)
    echo "将执行：docker exec assistant-openclaw openclaw automations list，找到 $NAME 后删除"
    if [ $APPLY -eq 1 ]; then
      id="$(docker exec assistant-openclaw openclaw automations list --json 2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin); jobs=d.get('jobs',d) if isinstance(d,dict) else d
print(next((j.get('id') for j in jobs if j.get('name')=='$NAME'),''))")"
      [ -n "$id" ] && docker exec assistant-openclaw openclaw automations remove "$id" || echo "没有找到任务 $NAME"
    else echo "（dry-run，未执行；加 --apply 才删除）"; fi ;;
  list) docker exec assistant-openclaw openclaw automations list ;;
  *) echo "用法: $0 create|delete [--apply] | list" >&2; exit 2 ;;
esac
