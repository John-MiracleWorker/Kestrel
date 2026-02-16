FROM python:3.11-slim

# Minimal sandbox image for skill execution
WORKDIR /sandbox

# Install minimal deps
RUN pip install --no-cache-dir requests beautifulsoup4 httpx

# Copy entrypoint
COPY entrypoint.py /sandbox/entrypoint.py

# Run as non-root
RUN useradd -m -s /bin/bash sandbox
USER sandbox

ENTRYPOINT ["python", "/sandbox/entrypoint.py"]
