#!/usr/bin/env python3
"""Module 2.5: Send predictions to Telegram, wait for approval, update database."""

import os
import logging
import sqlite3
import time
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TARGET_CHAT_ID")
DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
BATCH_SIZE = 5
API_URL = f"https://api.telegram.org/bot{TOKEN}"


def get_pending():
    """Get pending predictions."""
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT id, week, department, day, task, hours FROM predictions WHERE status='pending' ORDER BY day, id"
    ).fetchall()
    conn.close()
    return rows


def send_message(text, keyboard=None):
    """Send a message to Telegram."""
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
    
    resp = requests.post(f"{API_URL}/sendMessage", json=payload)
    return resp.json().get("result", {}).get("message_id")


def build_keyboard(items):
    """Build inline keyboard for a batch."""
    keyboard = {"inline_keyboard": []}
    
    for pid, week, dept, day, task, hours in items:
        keyboard["inline_keyboard"].append([
            {"text": f"✅", "callback_data": f"ok_{pid}"},
            {"text": f"❌", "callback_data": f"no_{pid}"},
        ])
    
    keyboard["inline_keyboard"].append([
        {"text": "✅ ALLE", "callback_data": "all_ok"}
    ])
    
    return keyboard


def wait_for_response(message_id, items):
    """Poll Telegram for button presses."""
    pids = [i[0] for i in items]
    offset = 0
    
    logger.info("Waiting for your response on Telegram...")
    
    for _ in range(120):  # Poll for 2 minutes
        resp = requests.get(f"{API_URL}/getUpdates", params={
            "offset": offset,
            "timeout": 10
        }).json()
        
        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            callback = update.get("callback_query")
            
            if not callback:
                continue
            
            data = callback["data"]
            
            if data == "all_ok":
                # Approve all shown items
                conn = sqlite3.connect(str(DB_PATH))
                for pid in pids:
                    conn.execute("UPDATE predictions SET status='approved' WHERE id=?", (pid,))
                conn.commit()
                conn.close()
                logger.info("All approved")
                return True
            
            action, pid = data.split("_")
            pid = int(pid)
            
            conn = sqlite3.connect(str(DB_PATH))
            if action == "ok":
                conn.execute("UPDATE predictions SET status='approved' WHERE id=?", (pid,))
            elif action == "no":
                conn.execute("DELETE FROM predictions WHERE id=?", (pid,))
            conn.commit()
            conn.close()
        
        time.sleep(3)
    
    logger.warning("No response received")
    return False


def confirm_batch(items):
    """Send one batch and wait for approval."""
    text_parts = [f"<b>Woche {items[0][1]} — {items[0][2]}</b>\n"]
    
    for pid, week, dept, day, task, hours in items:
        text_parts.append(f"• {day}: {task} <i>({hours}h)</i>")
    
    text = "\n".join(text_parts)
    keyboard = build_keyboard(items)
    
    msg_id = send_message(text, keyboard)
    if msg_id:
        return wait_for_response(msg_id, items)
    return False


def run_confirmation():
    """Process all pending predictions in batches."""
    all_pending = get_pending()
    
    if not all_pending:
        logger.info("No pending predictions to confirm")
        return
    
    logger.info(f"Found {len(all_pending)} pending predictions")
    
    for i in range(0, len(all_pending), BATCH_SIZE):
        batch = all_pending[i:i + BATCH_SIZE]
        confirm_batch(batch)
    
    remaining = len(get_pending())
    logger.info(f"Confirmation complete. {remaining} remaining (rejected)")


if __name__ == "__main__":
    run_confirmation()
