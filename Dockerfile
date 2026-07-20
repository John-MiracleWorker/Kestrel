# syntax=docker/dockerfile:1.7

ARG MEMVID_SDK_VERSION=2.0.160
ARG MEMVID_SDK_SDIST_SHA256=8eab5aec9a30eb459f553ed091038b6916d02a2f33569b32a7aee1b556820243
ARG UV_VERSION=0.11.16

FROM node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS web-build
WORKDIR /app
COPY scripts/generate-web-third-party-notices.mjs ./scripts/
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run licenses:check && npm run build

FROM rust:1.89-bookworm@sha256:948f9b08a66e7fe01b03a98ef1c7568292e07ec2e4fe90d88c07bb14563c84ff AS memvid-wheels
ARG MEMVID_SDK_VERSION
ARG MEMVID_SDK_SDIST_SHA256
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 \
        python3-dev \
        python3-venv \
    && python3 -m venv /opt/memvid-build \
    && /opt/memvid-build/bin/python -m pip install --upgrade pip \
    && mkdir -p /wheels /tmp/memvid-source \
    && /opt/memvid-build/bin/python -m pip download \
        --no-deps \
        --no-binary memvid-sdk \
        --dest /tmp/memvid-source \
        "memvid-sdk==${MEMVID_SDK_VERSION}" \
    && echo "${MEMVID_SDK_SDIST_SHA256}  /tmp/memvid-source/memvid_sdk-${MEMVID_SDK_VERSION}.tar.gz" \
        | sha256sum -c - \
    && /opt/memvid-build/bin/python -m pip wheel \
        --no-deps \
        --wheel-dir /wheels \
        "/tmp/memvid-source/memvid_sdk-${MEMVID_SDK_VERSION}.tar.gz"

FROM python:3.11-slim-trixie@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS dependency-lock
ARG UV_VERSION
WORKDIR /lock
COPY pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}" \
    && uv export \
        --frozen \
        --no-dev \
        --no-emit-local \
        --extra memvid \
        --extra openai \
        --extra anthropic \
        --extra gemini \
        --extra server \
        --extra mcp \
        --format requirements.txt \
        --output-file /lock/requirements-runtime.txt

FROM python:3.11-slim-trixie@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS runtime

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
RUN chmod a-s /usr/bin/mount /usr/bin/umount /usr/bin/su \
    && groupadd --system kestrel \
    && useradd --system --gid kestrel --home-dir /app kestrel

COPY pyproject.toml README.md LICENSE ./
COPY web/public/THIRD_PARTY_NOTICES.txt ./web/public/THIRD_PARTY_NOTICES.txt
COPY src ./src
COPY scripts ./scripts
COPY docs ./docs
COPY --from=web-build /app/web/dist ./web/dist
COPY --from=memvid-wheels /wheels /tmp/memvid-wheels
COPY --from=dependency-lock /lock/requirements-runtime.txt /tmp/requirements-runtime.txt

RUN python -m pip install /tmp/memvid-wheels/memvid_sdk-*.whl \
    && python -m pip install --require-hashes -r /tmp/requirements-runtime.txt \
    && python -m pip install --no-deps --no-build-isolation -e ".[${INSTALL_EXTRAS}]" \
    && python -c "import memvid_sdk; assert callable(memvid_sdk.create); assert callable(memvid_sdk.use)" \
    && test -f /app/LICENSE \
    && test ! -u /usr/bin/mount \
    && test ! -u /usr/bin/umount \
    && test ! -u /usr/bin/su \
    && python -c "from importlib.metadata import files; paths={str(path) for path in (files('nested-memvid-agent') or ())}; assert any(path.endswith('.dist-info/licenses/LICENSE') for path in paths); assert any(path.endswith('.dist-info/licenses/web/public/THIRD_PARTY_NOTICES.txt') for path in paths)" \
    && rm -r /tmp/memvid-wheels /tmp/requirements-runtime.txt \
    && mkdir -p /data/memory /data/logs /data/state /data/skills /data/config \
    && chown -R kestrel:kestrel /app /data

USER kestrel
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os, urllib.request; token=os.environ.get('NEST_AGENT_API_TOKEN','').strip(); req=urllib.request.Request('http://127.0.0.1:8765/api/health/ready', headers={'Authorization':'Bearer '+token}); urllib.request.urlopen(req, timeout=3).read()" || exit 1

ENTRYPOINT ["/bin/sh", "/app/scripts/docker-entrypoint.sh"]
CMD ["nest-agent", "server", "--host", "0.0.0.0", "--port", "8765", "--require-api-auth"]
