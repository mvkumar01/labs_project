import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def notify_telegram(text: str) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    config_path = repo_root / "config" / "telegram.json"

    try:
        # Deferred import: keep `import live.notify` from ever crashing the
        # runner if `requests` is missing in this interpreter — a failed
        # notification must never take down order management.
        import requests

        if not config_path.exists():
            log.info("Telegram config not found at %s; notifications disabled", config_path)
            return

        payload = json.loads(config_path.read_text(encoding="utf-8"))
        bot_token = str(payload.get("bot_token") or "").strip()
        chat_id = str(payload.get("chat_id") or "").strip()
        if not bot_token or not chat_id:
            log.warning("Telegram config missing bot_token or chat_id at %s", config_path)
            return

        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=5,
        )
        if response.status_code != 200:
            log.warning(
                "Telegram sendMessage failed with status %s: %s",
                response.status_code,
                response.text,
            )
    except Exception:
        log.warning("Telegram notification failed", exc_info=True)
