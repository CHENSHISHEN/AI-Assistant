# assistant-gate：基于 debian:bookworm-slim，装 Electron 运行库，宿主机 /opt/Orca 只读挂载。
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates \
      libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libgtk-3-0 \
      libgbm1 libasound2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
      libpango-1.0-0 libcairo2 libxss1 libxtst6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# 命令行入口固定为宿主机 Orca 自带的 orca-ide，版本与 Orca 服务始终一致
RUN printf '#!/bin/sh\nexec /opt/Orca/resources/bin/orca-ide "$@"\n' > /usr/local/bin/orca && chmod 755 /usr/local/bin/orca

COPY app /srv/gate/app
COPY entrypoint.sh /srv/gate/entrypoint.sh
RUN chmod 755 /srv/gate/entrypoint.sh
WORKDIR /srv/gate
USER 1001:1001
ENTRYPOINT ["/srv/gate/entrypoint.sh"]
