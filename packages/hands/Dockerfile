FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY packages/hands/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared proto files and shared action schemas
COPY packages/shared/proto/ ../shared/proto/
COPY packages/shared/action_event_schema.py ../shared/action_event_schema.py
COPY packages/shared/action_receipt_schema.py ../shared/action_receipt_schema.py

# Copy hands source
COPY packages/hands/ .

ENV PYTHONUNBUFFERED=1
EXPOSE 50052
CMD ["python", "server.py"]
