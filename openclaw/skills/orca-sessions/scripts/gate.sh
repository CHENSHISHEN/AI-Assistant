#!/bin/sh
# 调用 assistant-gate 的只读会话接口。
# token 只从密钥文件读取，经 stdin 交给 curl（-H @-），不出现在命令行、输出或日志里。
# 用法：
#   gate.sh sessions
#   gate.sh read <句柄> [max_chars]
#   gate.sh search "<搜索词>" [limit]
#   gate.sh worktrees
set -eu
GATE_URL="${GATE_URL:-http://172.28.0.1:8500}"
TOKEN_FILE="${GATE_TOKEN_FILE:-/run/secrets/gate_token}"

[ -r "$TOKEN_FILE" ] || { echo '{"error":"no_token","detail":"读不到网关 token 文件"}'; exit 2; }

call() {  # call <curl 参数...>；输出响应体，并在最后一行追加 HTTP 状态码到 stderr
  tmp="$(mktemp)"
  code="$(printf 'Authorization: Bearer %s\n' "$(tr -d '\r\n ' < "$TOKEN_FILE")" \
    | curl -sS -m 60 -o "$tmp" -w '%{http_code}' -H @- "$@")" || { echo '{"error":"gate_unreachable","detail":"连不上网关"}'; rm -f "$tmp"; exit 3; }
  cat "$tmp"; echo; rm -f "$tmp"
  case "$code" in 2??) exit 0 ;; *) echo "HTTP $code" >&2; exit 1 ;; esac
}

cmd="${1:-}"
case "$cmd" in
  sessions)  call "$GATE_URL/sessions" ;;
  worktrees) call "$GATE_URL/worktrees" ;;
  read)
    h="${2:-}"
    case "$h" in term_[0-9A-Za-z-]*) ;; *) echo '{"error":"invalid_argument","detail":"句柄必须形如 term_<uuid>"}'; exit 2 ;; esac
    case "$h" in *[!0-9A-Za-z_-]*) echo '{"error":"invalid_argument","detail":"句柄含非法字符"}'; exit 2 ;; esac
    if [ -n "${3:-}" ]; then call --get --data-urlencode "max_chars=$3" "$GATE_URL/sessions/$h/read"
    else call "$GATE_URL/sessions/$h/read"; fi ;;
  search)
    q="${2:-}"; [ -n "$q" ] || { echo '{"error":"invalid_argument","detail":"缺少搜索词"}'; exit 2; }
    call --get --data-urlencode "q=$q" --data-urlencode "limit=${3:-20}" "$GATE_URL/search" ;;
  *) echo '用法: gate.sh sessions | read <句柄> [max_chars] | search "<词>" [limit] | worktrees' >&2; exit 2 ;;
esac
