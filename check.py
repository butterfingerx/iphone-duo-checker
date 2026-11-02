"""
iPhone Duo Availability Checker — GitHub Actions version.

Checks all 6 Apple HK stores for iPhone Duo pickup availability.
Sends Telegram notification when stock changes from unavailable to available.
Sends a daily health check message.

State is stored in state.json in the repo.
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

HK_TZ = timezone(timedelta(hours=8))
STATE_FILE = Path(__file__).parent / "state.json"

# ─────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────

API_URL = "https://www.apple.com/hk/shop/pickup-message-recommendations"
BUY_URL = "https://www.apple.com/hk/shop/buy-iphone/iphone-duo"
DELAY = 2

PARTS = {
    "MK2E4ZA/A": {"color": "Night Sky", "storage": "256GB"},
    "MK2G4ZA/A": {"color": "Night Sky", "storage": "512GB"},
    "MK2J4ZA/A": {"color": "Night Sky", "storage": "1TB"},
    "MK2L4ZA/A": {"color": "Night Sky", "storage": "2TB"},
    "MK2D4ZA/A": {"color": "Star White", "storage": "256GB"},
    "MK2F4ZA/A": {"color": "Star White", "storage": "512GB"},
    "MK2H4ZA/A": {"color": "Star White", "storage": "1TB"},
    "MK2K4ZA/A": {"color": "Star White", "storage": "2TB"},
}

STORES = {
    "R409": "Causeway Bay, Hysan Place",
    "R428": "Central, ifc mall",
    "R499": "Tsim Sha Tsui, Canton Road",
    "R673": "Kwun Tong, apm Hong Kong",
    "R485": "Kowloon Tong, Festival Walk",
    "R610": "Sha Tin, New Town Plaza",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
    "Accept": "application/json,text/html",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.apple.com/hk/shop/buy-iphone/iphone-duo",
}


# ─────────────────────────────────────────────────────────────────────────
# State management (JSON file)
# ─────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def commit_state():
    """Commit state.json so it persists across runs."""
    try:
        subprocess.run(["git", "config", "user.email", "bot@iphone-duo-checker"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Stock Checker Bot"], check=True, capture_output=True)
        subprocess.run(["git", "add", "state.json"], check=True, capture_output=True)

        result = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
        if result.returncode != 0:
            subprocess.run(["git", "commit", "-m", "chore: update stock state"], check=True, capture_output=True)
            subprocess.run(["git", "push"], check=True, capture_output=True)
            logger.info("State committed and pushed")
        else:
            logger.info("No state changes to commit")
    except subprocess.CalledProcessError as e:
        logger.warning("Git commit failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=15,
        )
        result = resp.json()
        if result.get("ok"):
            logger.info("Telegram message sent")
            return True
        logger.error("Telegram error: %s", result.get("description"))
        return False
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return False


# ─────────────────────────────────────────────────────────────────────────
# Apple availability check
# ─────────────────────────────────────────────────────────────────────────

def check_apple() -> tuple[list[dict], list[str]]:
    results = []
    errors = []
    pn_list = list(PARTS.keys())

    for i, pn in enumerate(pn_list):
        try:
            resp = requests.get(API_URL, params={
                "fae": "true", "mts.0": "regular", "mts.1": "compact",
                "location": "hong kong", "product": pn,
            }, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            stores = data.get("body", {}).get("PickupMessage", {}).get("stores", [])
            if not stores:
                errors.append(f"No stores for {pn}")
                continue

            for s in stores:
                sid = s.get("storeNumber", "")
                if sid not in STORES:
                    continue
                pa = s.get("partsAvailability", {}).get(pn)
                avail = pa.get("pickupDisplay", "").lower() == "available" if pa else False
                title = (pa.get("messageTypes", {}).get("regular", {}).get("storePickupProductTitle")) if pa else None
                results.append({"pn": pn, "sid": sid, "avail": avail, "title": title})

        except Exception as e:
            errors.append(f"{pn}: {e}")

        if i < len(pn_list) - 1:
            time.sleep(DELAY)

    return results, errors


# ─────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────

def main():
    logger.info("Starting iPhone Duo availability check")
    state = load_state()

    # Daily health check
    today = datetime.now(HK_TZ).strftime("%Y-%m-%d")
    if state.get("last_health_check") != today:
        now = datetime.now(HK_TZ).strftime("%Y-%m-%d %H:%M HKT")
        msg = (
            f"💚 *iPhone Duo Monitor Active*\n\n"
            f"Monitoring all 6 Apple HK stores for iPhone Duo pickup stock.\n"
            f"Checking {len(PARTS)} models every 30 minutes.\n"
            f"🕐 {now}"
        )
        if send_telegram(msg):
            state["last_health_check"] = today

    # Check availability
    results, errors = check_apple()
    avail_count = sum(1 for r in results if r["avail"])
    logger.info("Results: %d items, %d available, %d errors", len(results), avail_count, len(errors))

    # Find newly available items
    prev = state.get("availability", {})
    newly = []
    for r in results:
        key = f"{r['pn']}:{r['sid']}"
        if r["avail"] and not prev.get(key, False):
            newly.append(r)

    # Notify
    if newly:
        lines = ["🚨 *iPhone Duo In Stock!*\n"]
        for r in newly:
            info = PARTS.get(r["pn"], {})
            label = r.get("title") or f"iPhone Duo {info.get('color', '')} {info.get('storage', '')}"
            lines.append(f"✅ *{label}*")
            lines.append(f"   📍 {STORES.get(r['sid'], r['sid'])}")
            lines.append("")
        now = datetime.now(HK_TZ).strftime("%Y-%m-%d %H:%M HKT")
        lines.append(f"🕐 {now}")
        lines.append(f"\n🛒 [Buy now]({BUY_URL})")
        send_telegram("\n".join(lines))
        logger.info("Sent alert for %d newly available items", len(newly))
    else:
        logger.info("No new availability to report")

    # Save state
    state["availability"] = {f"{r['pn']}:{r['sid']}": r["avail"] for r in results}
    state["last_check"] = datetime.now(HK_TZ).isoformat()
    save_state(state)
    commit_state()

    if errors:
        logger.warning("Errors: %s", errors)


if __name__ == "__main__":
    main()
