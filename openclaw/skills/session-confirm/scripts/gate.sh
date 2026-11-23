#!/bin/sh
# 调用 assistant-gate 的"动手"接口（会话确认、审批、应用）。三个 skill 共用。
# token 只从密钥文件读取，经 stdin 交给 curl（-H @-），不出现在命令行、输出或日志里。
# 用法：
#   gate.sh pending                          纳管会话中等待确认超过 5 分钟的
#   gate.sh reply <句柄> <文本> [--user-requested]   回复会话（危险的会自动转审批）
#   gate.sh manage <句柄> | unmanage <句柄>
#   gate.sh approvals                        未决审批单（不含码）
#   gate.sh confirm <审批单ID> <6位码>       仅当用户本人在私聊里给出码时使用
#   gate.sh apps | app-logs <应用id> [tail] | app-act <应用id> <start|stop|restart>
#   gate.sh audit [limit]
set -eu
GATE_URL="${GATE_URL:-http://172.28.0.1:8500}"
TOKEN_FILE="${GATE_TOKEN_FILE:-/run/secrets/gate_token}"
[ -r "$TOKEN_FILE" ] || { echo '{"error":"no_token","detail":"读不到网关 token 文件"}'; exit 2; }

call() {  # call <方法> <路径> [json 体文件]
  method="$1"; path="$2"; body="${3:-}"; tmp="$(mktemp)"
  if [ -n "$body" ]; then
    code="$(printf 'Authorization: Bearer %s\nContent-Type: application/json\n' "$(tr -d '\r\n ' < "$TOKEN_FILE")" \
      | curl -sS -m 60 -o "$tmp" -w '%{http_code}' -X "$method" -H @- --data-binary @"$body" "$GATE_URL$path")" \
      || { echo '{"error":"gate_unreachable","detail":"连不上网关"}'; rm -f "$tmp"; exit 3; }
  else
    code="$(printf 'Authorization: Bearer %s\n' "$(tr -d '\r\n ' < "$TOKEN_FILE")" \
      | curl -sS -m 60 -o "$tmp" -w '%{http_code}' -X "$method" -H @- "$GATE_URL$path")" \
      || { echo '{"error":"gate_unreachable","detail":"连不上网关"}'; rm -f "$tmp"; exit 3; }
  fi
  cat "$tmp"; echo; rm -f "$tmp"
  echo "HTTP $code" >&2
  case "$code" in 2??) exit 0 ;; *) exit 1 ;; esac
}
handle_ok() { case "$1" in term_[0-9A-Za-z-]*) case "$1" in *[!0-9A-Za-z_-]*) return 1 ;; esac ;; *) return 1 ;; esac; }
id_ok()     { case "$1" in ""|*[!a-z0-9-]*|-*) return 1 ;; esac; }
bad()       { echo "{\"error\":\"invalid_argument\",\"detail\":\"$1\"}"; exit 2; }
json_body() { python3 -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1]), ensure_ascii=False))' "$1" >"$2"; }

cmd="${1:-}"
case "$cmd" in
  pending)   call GET /sessions/pending ;;
  approvals) call GET /approvals ;;
  apps)      call GET /apps ;;
  audit)     call GET "/audit?limit=${2:-30}" ;;
  manage)    handle_ok "${2:-}" || bad "句柄必须形如 term_<uuid>"; call POST "/sessions/$2/manage" ;;
  unmanage)  handle_ok "${2:-}" || bad "句柄必须形如 term_<uuid>"; call DELETE "/sessions/$2/manage" ;;
  reply)
    handle_ok "${2:-}" || bad "句柄必须形如 term_<uuid>"; [ -n "${3:-}" ] || bad "缺少回复文本"
    flag=false; [ "${4:-}" = "--user-requested" ] && flag=true
    body="$(mktemp)"
    python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1], "user_requested": sys.argv[2] == "true"}, ensure_ascii=False))' "$3" "$flag" > "$body"
    call POST "/sessions/$2/reply" "$body" ;;
  confirm)
    case "${2:-}" in [A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]) ;; *) bad "审批单 ID 是 4 位大写字母数字" ;; esac
    case "${3:-}" in [0-9][0-9][0-9][0-9][0-9][0-9]) ;; *) bad "批准码是 6 位数字" ;; esac
    body="$(mktemp)"; printf '{"code":"%s"}' "$3" > "$body"
    call POST "/approvals/$2/confirm" "$body" ;;
  app-logs)  id_ok "${2:-}" || bad "应用 id 只能是小写字母、数字和中划线"; call GET "/apps/$2/logs?tail=${3:-200}" ;;
  app-act)
    id_ok "${2:-}" || bad "应用 id 只能是小写字母、数字和中划线"
    case "${3:-}" in start|stop|restart) ;; *) bad "动作只能是 start/stop/restart" ;; esac
    call POST "/apps/$2/$3" ;;
  *) echo '用法: gate.sh pending|approvals|apps|audit [n]|manage <句柄>|unmanage <句柄>|reply <句柄> <文本>|confirm <ID> <码>|app-logs <id> [tail]|app-act <id> <动作>' >&2; exit 2 ;;
esac
