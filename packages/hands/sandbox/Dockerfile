FROM python:3.11-slim

# Sandbox image for skill execution.
# Includes common system tools and package managers so skills can
# install their own dependencies at runtime without failing on
# missing binaries (e.g. git, curl, jq).
WORKDIR /sandbox

# ── System tooling ──────────────────────────────────────────────
# Keep the layer cacheable by sorting packages alphabetically.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    dnsutils \
    git \
    jq \
    openssh-client \
    procps \
    rsync \
    unzip \
    wget \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js (LTS) for node_executor and JS-heavy skills ────────
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Pre-installed Python packages ──────────────────────────────
RUN pip install --no-cache-dir \
    requests \
    beautifulsoup4 \
    httpx \
    pyyaml \
    python-dotenv

# ── Entrypoint ─────────────────────────────────────────────────
COPY entrypoint.py /sandbox/entrypoint.py

# Run as non-root for safety (but with a real home dir for git/npm)
RUN useradd -m -s /bin/bash sandbox
USER sandbox

ENTRYPOINT ["python", "/sandbox/entrypoint.py"]
