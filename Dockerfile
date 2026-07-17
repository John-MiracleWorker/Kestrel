# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS web-build
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NEST_AGENT_BACKEND=memvid \
    NEST_AGENT_MEMORY_DIR=/data/memory \
    NEST_AGENT_LOG_DIR=/data/logs \
    NEST_AGENT_STATE_PATH=/data/state/agent.db \
    NEST_AGENT_SKILLS_DIR=/data/skills \
    NEST_AGENT_MCP_CONFIG=/data/config/mcp_servers.json \
    NEST_AGENT_CHANNEL_CONFIG=/data/config/channels.json \
    NEST_AGENT_SECRET_STORE_PATH=/data/secrets/local_vault.json \
    NEST_AGENT_ALLOW_SHELL=false \
    NEST_AGENT_ALLOW_FILE_WRITE=false \
    NEST_AGENT_ALLOW_POLICY_WRITES=false \
    NEST_AGENT_ALLOW_CODEX_CLI=false \
    NEST_AGENT_ALLOW_PLUGIN_INSTALL=false \
    NEST_AGENT_ALLOW_GIT_COMMIT=false \
    NEST_AGENT_ALLOW_GIT_PUSH=false \
    NEST_AGENT_ALLOW_REMOTE_MUTATION=false \
    NEST_AGENT_GIT_WRITE_MODE=local_branch \
    NEST_AGENT_PROTECTED_BRANCHES=main,master,release/* \
    NEST_AGENT_ALLOW_MEMORY_IMPORT=false \
    NEST_AGENT_ALLOW_EXECUTABLE_SKILLS=false \
    NEST_AGENT_ALLOW_MCP_NETWORK_ENDPOINTS=false \
    NEST_AGENT_ENABLE_AUTO_CONSOLIDATION=false \
    NEST_AGENT_AUTO_CONSOLIDATION_DRY_RUN=true \
    NEST_AGENT_REQUIRE_API_AUTH=true

ARG INSTALL_EXTRAS=server,mcp,memvid,openai,anthropic,gemini

WORKDIR /app
RUN groupadd --system kestrel && useradd --system --gid kestrel --home-dir /app kestrel

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
COPY docs ./docs
COPY --from=web-build /app/web/dist ./web/dist

RUN python -m pip install --upgrade pip \
    && python -m pip install -e ".[${INSTALL_EXTRAS}]" \
    && mkdir -p /data/memory /data/logs /data/state /data/skills /data/config \
    && chown -R kestrel:kestrel /app /data

USER kestrel
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os, urllib.request; token=os.environ.get('NEST_AGENT_API_TOKEN','').strip(); req=urllib.request.Request('http://127.0.0.1:8765/api/health/ready', headers={'Authorization':'Bearer '+token}); urllib.request.urlopen(req, timeout=3).read()" || exit 1

ENTRYPOINT ["/bin/sh", "/app/scripts/docker-entrypoint.sh"]
CMD ["nest-agent", "server", "--host", "0.0.0.0", "--port", "8765", "--require-api-auth"]
