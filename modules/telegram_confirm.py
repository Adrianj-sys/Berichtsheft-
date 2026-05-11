#!/usr/bin/env python3
"""Module 2.5: Send predictions grouped by day to Telegram."""

import json
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
API_URL = f"https://api.telegram.org/bot{TOKEN}"

DAY_ORDER = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]


def get_pending_for_day(day):
    """Check if a specific day still has pending entries."""
    conn = sqlite3.connect(str(DB_PATH))
    count = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE status='pending' AND day=?",
        (day,)
    ).fetchone()[0]
    conn.close()
    return count


def get_next_day():
    """Get all pending predictions for the next unprocessed day."""
    conn = sqlite3.connect(str(DB_PATH))
    for day in DAY_ORDER:
        rows = conn.execute(
            "SELECT id, week, department, day, task, hours FROM predictions WHERE status='pending' AND day=? ORDER BY id",
            (day,)
        ).fetchall()
        if rows:
            conn.close()
            return day, rows
    conn.close()
    return None, []


def send_day(day, items):
    """Send all entries for one day in a single message."""
    week = items[0][1]
    dept = items[0][2]
    
    lines = [f"<b>Woche {week} — {day}</b>\n"]
    for pid, w, d, dy, task, hours in items:
        lines.append(f"• {task} <i>({hours}h)</i>")
    
    text = "\n".join(lines)
    
    keyboard = {"inline_keyboard": []}
    for pid, w, d, dy, task, hours in items:
        keyboard["inline_keyboard"].append([
            {"text": f"✅ {task[:30]}...", "callback_data": f"ok_{pid}"},
            {"text": "❌", "callback_data": f"no_{pid}"},
        ])
    
    keyboard["inline_keyboard"].append([
        {"text": "✅ ALLE genehmigen", "callback_data": f"dayok_{items[0][0]}"}
    ])
    
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(keyboard)
    }
    
    resp = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=10)
    logger.info(f"Sent day: {day} ({len(items)} entries)")
    return resp.json().get("ok", False)


def check_responses():
    """Check for NEW button presses. Returns list of action strings."""
    offset_file = Path(__file__).parent.parent / "data" / "last_update.txt"
    offset_file.parent.mkdir(parents=True, exist_ok=True)
    
    if offset_file.exists():
        offset = int(offset_file.read_text().strip())
    else:
        offset = 0
    
    resp = requests.get(f"{API_URL}/getUpdates", params={"offset": offset + 1}, timeout=10)
    data = resp.json()
    results = []
    
    for update in data.get("result", []):
        update_id = update["update_id"]
        offset_file.write_text(str(update_id))
        
        callback = update.get("callback_query")
        if callback:
            callback_id = callback["id"]
            requests.post(f"{API_URL}/answerCallbackQuery", json={
                "callback_query_id": callback_id,
                "text": "OK"
            })
            results.append(callback["data"])
    
    return results


def process_actions(actions, current_day_pids):
    """Process all button presses for the current day."""
    conn = sqlite3.connect(str(DB_PATH))
    
    for action_str in actions:
        if action_str.startswith("dayok_"):
            for pid in current_day_pids:
                conn.execute("UPDATE predictions SET status='approved' WHERE id=?", (pid,))
            logger.info("All approved for this day")
        elif "_" in action_str:
            action, pid = action_str.split("_", 1)
            pid = int(pid)
            if action == "ok":
                conn.execute("UPDATE predictions SET status='approved' WHERE id=?", (pid,))
                logger.info(f"Approved: {pid}")
            elif action == "no":
                conn.execute("DELETE FROM predictions WHERE id=?", (pid,))
                logger.info(f"Deleted: {pid}")
    
    conn.commit()
    conn.close()


def run_confirmation():
    """Process one day at a time."""
    logger.info("Starting confirmation...")
    
    while True:
        day, items = get_next_day()
        if not items:
            logger.info("All days processed!")
            break
        
        pids = [item[0] for item in items]
        send_day(day, items)
        
        logger.info(f"Waiting for your response on {day}...")
        responded = False
        for _ in range(120):
            actions = check_responses()
            if actions:
                process_actions(actions, pids)
                remaining = get_pending_for_day(day)
                if not remaining:
                    responded = True
                    break
            time.sleep(3)
        
        if not responded:
            logger.warning(f"Timeout waiting for {day}")
        else:
            logger.info(f"{day} complete")
            time.sleep(1)


if __name__ == "__main__":
    run_confirmation()
