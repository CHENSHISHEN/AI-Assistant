#!/usr/bin/env bash
# 幂等：创建 state/ gate/data/ secrets/，目录 700、secrets 下文件 600，
# 并去掉从父目录继承来的 ACL（否则同组的其他用户能读）。
# 可选：SECRETS_GRANT_USER=<登录用户> 时给 secrets/ 加该用户的 rwx ACL（默认不加）。
set -euo pipefail
cd "$(dirname "$0")/.."

for d in state gate/data secrets; do
  mkdir -p "$d"
  setfacl -R -b "$d"        # 去掉继承的 ACL（含 default）
  chgrp -R "$(id -gn)" "$d"   # 属组改成自己的主组（父目录 setgid 会带来父目录的属组）
  chmod 700 "$d"; chmod g-s "$d"   # 去掉 setgid，否则容器内 uid 1001 对新文件 fchmod 会 EPERM
done
# 内部 token：不存在才生成（32 字节随机数的 hex），已有的不覆盖
for t in gate_token openclaw_gateway_token; do
  if [ ! -s "secrets/$t" ]; then
    (umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "secrets/$t")
  fi
done
# 已存在的密钥文件统一 600（不读内容）
find secrets -type f -exec chmod 600 {} +
# state/gate 里已有文件保持原权限，只保证目录不对 group/other 开放
find state gate/data -type d -exec chmod 700 {} + -exec chmod g-s {} +

if [ -n "${SECRETS_GRANT_USER:-}" ]; then
  setfacl -m "u:${SECRETS_GRANT_USER}:rwx" secrets
fi
echo "bootstrap ok"
