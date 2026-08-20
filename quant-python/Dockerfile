# syntax=docker/dockerfile:1

# ---- build stage: compile the Next.js web console ----
FROM node:22-bookworm-slim AS build
WORKDIR /app

# 国内网络可覆盖为镜像源：docker compose 中 build.args.NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=https://registry.npmjs.org

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
COPY web/package.json web/package-lock.json ./web/
RUN cd web && npm ci --no-audit --no-fund --registry=$NPM_REGISTRY
COPY web/ ./web/
RUN cd web && npm run build

# ---- runtime stage: Node (web + scheduler) + Python (signal engine) ----
FROM node:22-bookworm-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip

ENV NODE_ENV=production
ENV TZ=Asia/Shanghai
ENV PYTHON_BIN=/opt/venv/bin/python
ENV WEB_DATA_DIR=/app/data
ENV DATABASE_POOL_MAX=5

WORKDIR /app/web
COPY --from=build /app/web /app/web
COPY signal_system /app/signal_system

RUN /opt/venv/bin/pip install --no-cache-dir -r /app/signal_system/requirements.txt \
    && mkdir -p /app/data \
    /app/signal_system/output \
    /app/signal_system/cache \
    /app/signal_system/logs \
    /app/signal_system/state

EXPOSE 3111

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3111/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

# 定时任务由 web 进程内的 node-cron 调度，一个容器即可跑完整系统
CMD ["node", "node_modules/next/dist/bin/next", "start", "-H", "0.0.0.0", "-p", "3111"]
