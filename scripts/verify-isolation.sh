#!/usr/bin/env bash
# P0/P1 隔离验收入口；检查逻辑在 verify_isolation.py（失败即非 0）。
cd "$(dirname "$0")/.." || exit 1
exec python3 -I scripts/verify_isolation.py "$@"
