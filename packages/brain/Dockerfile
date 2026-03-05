FROM python:3.11-slim-bookworm

WORKDIR /app

# System deps for llama-cpp, grpc, Node.js (for MCP servers), and Docker CLI
# (container_control tool needs the docker CLI + socket to manage Compose services)
RUN apt-get update && apt-get install -y --no-install-recommends \
   build-essential cmake git curl ca-certificates gnupg \
   && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
   && apt-get install -y --no-install-recommends nodejs \
   && rm -rf /var/lib/apt/lists/*

COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/
COPY --from=docker/compose-bin /docker-compose /usr/libexec/docker/cli-plugins/docker-compose

COPY packages/brain/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared proto files and tool catalog
COPY packages/shared/proto/ ../shared/proto/
COPY packages/shared/tool-catalog.json ../shared/tool-catalog.json

# Copy brain source
COPY packages/brain/ .

ENV PYTHONUNBUFFERED=1
EXPOSE 50051
CMD ["python", "server.py"]
