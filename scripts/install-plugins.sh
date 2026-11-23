#!/usr/bin/env bash
# 按固定版本把 zai（智谱）provider 插件装进 state/extensions（幂等）。
# 用一次性容器 + 临时配置路径执行，不改 state/openclaw.json，
# 因为部署后的配置启用了 installPolicy（会拦截一切安装）。
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
IMAGE="${OPENCLAW_IMAGE:?需要 OPENCLAW_IMAGE}"
ZAI_VERSION="${ZAI_PLUGIN_VERSION:-2026.11.6}"
UIDGID="${ASSISTANT_UID:-1001}:${ASSISTANT_GID:-1001}"

if [ -f state/extensions/zai/package.json ] && grep -q "\"version\": \"${ZAI_VERSION}\"" state/extensions/zai/package.json; then
  echo "zai@${ZAI_VERSION} 已安装，跳过"; exit 0
fi
mkdir -p state
docker run --rm --name assistant-install-plugins --user "$UIDGID" \
  -v "$PWD/state:/srv/openclaw" \
  -e HOME=/srv/openclaw/home -e OPENCLAW_HOME=/srv/openclaw/home -e OPENCLAW_STATE_DIR=/srv/openclaw \
  -e OPENCLAW_CONFIG_PATH=/srv/openclaw/install-tmp.json -e OPENCLAW_CONFIG_DIR=/srv/openclaw \
  --entrypoint openclaw "$IMAGE" plugins install "@openclaw/zai-provider@${ZAI_VERSION}"
rm -f state/install-tmp.json   # 临时配置（/tmp 下 fchmod 会 EPERM，所以放在 state 内）
