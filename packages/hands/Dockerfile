FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY packages/hands/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared proto files
COPY packages/shared/proto/ ../shared/proto/

# Copy hands source
COPY packages/hands/ .

ENV PYTHONUNBUFFERED=1
EXPOSE 50052
CMD ["python", "server.py"]
