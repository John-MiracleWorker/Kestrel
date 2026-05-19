#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="${KESTREL_TELEGRAM_ENV:-.env.telegram}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE. Copy .env.telegram.example to .env.telegram and fill secrets." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN is required}"
: "${TELEGRAM_WEBHOOK_SECRET:?TELEGRAM_WEBHOOK_SECRET is required}"
: "${PUBLIC_URL:?PUBLIC_URL is required, e.g. https://example.trycloudflare.com}"

WEBHOOK_URL="${PUBLIC_URL%/}/api/channels/telegram/webhook"

echo "Bot identity:"
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" | python3 -m json.tool

echo
echo "Setting webhook to: ${WEBHOOK_URL}"
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${WEBHOOK_URL}" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  --data-urlencode "allowed_updates=[\"message\",\"edited_message\"]" \
  | python3 -m json.tool

echo
echo "Webhook info:"
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" | python3 -m json.tool
