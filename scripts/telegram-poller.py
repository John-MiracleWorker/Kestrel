#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PORT = os.environ.get("KESTREL_PORT", "8765")
CHANNEL_ID = os.environ.get("KESTREL_TELEGRAM_CHANNEL_ID", "telegram")
BASE = f"https://api.telegram.org/bot{TOKEN}"
INGEST = f"http://127.0.0.1:{PORT}/api/channels/ingest"
API_AUTH_TOKEN_ENV = os.environ.get("NEST_AGENT_API_AUTH_TOKEN_ENV", "NEST_AGENT_API_TOKEN")
API_AUTH_TOKEN = os.environ.get(API_AUTH_TOKEN_ENV, "")
OFFSET_PATH = Path(os.environ.get("KESTREL_TELEGRAM_OFFSET_PATH", str(Path.home() / ".kestrel" / "telegram.offset")))
HEALTH_PATH = Path(
    os.environ.get(
        "KESTREL_TELEGRAM_HEALTH_PATH",
        str(Path.home() / ".kestrel" / "telegram-poller-health.json"),
    )
)
ALLOWED_UPDATES = ["message", "edited_message", "callback_query"]

running = True


def _stop(*_: object) -> None:
    global running
    running = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


def post_form(method: str, data: dict[str, object], timeout: int = 35) -> dict[str, object]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(f"{BASE}/{method}", data=body)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - Telegram API URL is fixed.
        return json.loads(resp.read().decode())


def post_json(url: str, data: dict[str, object], timeout: int = 300) -> dict[str, object]:
    body = json.dumps(data).encode()
    headers = {"Content-Type": "application/json"}
    if API_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {API_AUTH_TOKEN}"
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if secret:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - local Kestrel URL.
        return json.loads(resp.read().decode())


def load_offset() -> int | None:
    try:
        raw = OFFSET_PATH.read_text().strip()
        return int(raw) if raw else None
    except FileNotFoundError:
        return None
    except ValueError:
        return None


def save_offset(offset: int) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(str(offset))


def write_health(status: str, *, error_type: str | None = None) -> None:
    try:
        HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = HEALTH_PATH.with_suffix(HEALTH_PATH.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema": "kestrel.telegram_poller_health.v1",
                    "status": status,
                    "updated_at_epoch": time.time(),
                    "pid": os.getpid(),
                    "error_type": error_type,
                },
                sort_keys=True,
            )
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, HEALTH_PATH)
    except OSError:
        pass


def main() -> int:
    # Polling cannot receive updates while a webhook is active.
    post_form("deleteWebhook", {"drop_pending_updates": "false"}, timeout=20)
    write_health("ready")
    print("Telegram polling bridge ready", flush=True)
    offset = load_offset()
    backoff = 1.0
    while running:
        try:
            payload: dict[str, object] = {
                "timeout": 30,
                "allowed_updates": json.dumps(ALLOWED_UPDATES),
            }
            if offset is not None:
                payload["offset"] = offset
            result = post_form("getUpdates", payload, timeout=40)
            if not result.get("ok"):
                write_health("error", error_type="telegram_not_ok")
                print(f"getUpdates returned not ok: {result}", file=sys.stderr, flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1.0
            write_health("healthy")
            updates = result.get("result", [])
            if not isinstance(updates, list):
                updates = []
            for update in updates:
                if not isinstance(update, dict):
                    continue
                update_id = int(update.get("update_id", 0))
                try:
                    post_json(
                        INGEST,
                        {
                            "provider": "telegram",
                            "channel_id": CHANNEL_ID,
                            "payload": update,
                            "send": True,
                        },
                        timeout=int(os.environ.get("KESTREL_TELEGRAM_INGEST_TIMEOUT", "360")),
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"ingest failed for update {update_id}: {exc}", file=sys.stderr, flush=True)
                    # Do not advance offset when Kestrel failed to process the update.
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    break
                offset = update_id + 1
                save_offset(offset)
        except urllib.error.HTTPError as exc:
            write_health("error", error_type=f"telegram_http_{exc.code}")
            detail = exc.read().decode(errors="replace")[:500]
            print(f"Telegram HTTPError {exc.code}: {detail}", file=sys.stderr, flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except Exception as exc:  # noqa: BLE001
            write_health("error", error_type=type(exc).__name__)
            print(f"Telegram poller error: {exc}", file=sys.stderr, flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    write_health("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
