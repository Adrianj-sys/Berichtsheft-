#!/usr/bin/env python3
"""Module 2.5: Send predictions by day. Rejected entries are immediately deleted and replaced."""

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
    conn = sqlite3.connect(str(DB_PATH))
    count = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE status='pending' AND day=?",
        (day,)
    ).fetchone()[0]
    conn.close()
    return count


def get_next_day():
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
    week = items[0][1]
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
    
    requests.post(f"{API_URL}/sendMessage", json={
        "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
        "reply_markup": json.dumps(keyboard)
    }, timeout=10)
    logger.info(f"Sent day: {day} ({len(items)} entries)")


def send_single(pid, week, day, task, hours):
    text = f"<b>Ersatz — Woche {week}, {day}</b>\n\n{task}\n<i>({hours}h)</i>"
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Genehmigen", "callback_data": f"ok_{pid}"},
        {"text": "❌ Ablehnen", "callback_data": f"no_{pid}"},
    ]]}
    requests.post(f"{API_URL}/sendMessage", json={
        "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
        "reply_markup": json.dumps(keyboard)
    }, timeout=10)
    logger.info(f"Sent replacement: {day} - {task}")


def send_message(text):
    requests.post(f"{API_URL}/sendMessage", json={"chat_id": CHAT_ID, "text": text})


def check_responses():
    offset_file = Path(__file__).parent.parent / "data" / "last_update.txt"
    offset_file.parent.mkdir(parents=True, exist_ok=True)
    offset = int(offset_file.read_text().strip()) if offset_file.exists() else 0
    
    resp = requests.get(f"{API_URL}/getUpdates", params={"offset": offset + 1}, timeout=10)
    data = resp.json()
    results = []
    
    for update in data.get("result", []):
        update_id = update["update_id"]
        offset_file.write_text(str(update_id))
        callback = update.get("callback_query")
        if callback:
            requests.post(f"{API_URL}/answerCallbackQuery", json={
                "callback_query_id": callback["id"], "text": "OK"
            })
            results.append(callback["data"])
    
    return results


def process_actions(actions, current_day_pids):
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
                conn.execute("UPDATE predictions SET status='rejected' WHERE id=?", (pid,))
                logger.info(f"Rejected: {pid}")
    conn.commit()
    conn.close()


def wait_for_responses():
    """Wait indefinitely for button presses."""
    while True:
        actions = check_responses()
        if actions:
            return actions
        time.sleep(3)


def handle_rejection(pid):
    """Delete rejected entry and generate a replacement immediately."""
    conn = sqlite3.connect(str(DB_PATH))
    rejected = conn.execute("SELECT week, day, hours FROM predictions WHERE id=?", (pid,)).fetchone()
    
    if not rejected:
        conn.close()
        return None
    
    week, day, hours = rejected
    
    rejected_tasks = conn.execute(
        "SELECT task FROM predictions WHERE status='rejected' AND week=? AND day=?",
        (week, day)
    ).fetchall()
    rejected_list = [t for (t,) in rejected_tasks]
    
    from predict_activities import regenerate_single
    result = regenerate_single(week, day, hours, rejected_list)
    
    if result:
        new_task, new_hours = result
        dept = conn.execute("SELECT department FROM predictions WHERE week=? LIMIT 1", (week,)).fetchone()[0]
        
        cursor = conn.execute(
            "INSERT INTO predictions (week, department, day, task, hours, status) VALUES (?, ?, ?, ?, ?, 'pending')",
            (week, dept, day, new_task, new_hours)
        )
        new_id = cursor.lastrowid
        
        conn.execute("DELETE FROM predictions WHERE id=?", (pid,))
        conn.commit()
        conn.close()
        
        logger.info(f"Generated replacement {new_id} for rejected {pid}")
        send_single(new_id, week, day, new_task, new_hours)
        return new_id
    else:
        conn.close()
        logger.error(f"Failed to generate replacement for {pid}")
        return None


def clear_update_queue():
    """Clear all pending Telegram updates before starting."""
    resp = requests.get(f"{API_URL}/getUpdates", timeout=10)
    data = resp.json()
    if data.get("result"):
        last_id = data["result"][-1]["update_id"]
        requests.get(f"{API_URL}/getUpdates", params={"offset": last_id + 1}, timeout=10)
        offset_file = Path(__file__).parent.parent / "data" / "last_update.txt"
        offset_file.parent.mkdir(parents=True, exist_ok=True)
        offset_file.write_text(str(last_id))
        logger.info(f"Cleared update queue up to {last_id}")


def run_confirmation():
    logger.info("Starting confirmation...")
    

    # === INITIAL PASS: Send all days ===
    clear_update_queue()
    
    while True:
        day, items = get_next_day()
        if not items:
            break
        
        pids = [item[0] for item in items]
        send_day(day, items)
        logger.info(f"Waiting for {day}...")
        
        actions = wait_for_responses()
        if actions:
            process_actions(actions, pids)
            
            # Check if there are still pending entries for this day
            remaining = get_pending_for_day(day)
            if remaining > 0:
                logger.info(f"{day} still has {remaining} pending, resending...")
                continue
        
        logger.info(f"{day} complete")
        time.sleep(1)



    
    logger.info("Initial pass complete")
    
    # === HANDLE REJECTIONS: One at a time ===
    while True:
        conn = sqlite3.connect(str(DB_PATH))
        rejected = conn.execute(
            "SELECT id, week, day, task, hours FROM predictions WHERE status='rejected' ORDER BY id LIMIT 1"
        ).fetchone()
        conn.close()
        
        if not rejected:
            break
        
        pid = rejected[0]
        new_id = handle_rejection(pid)
        
        if new_id:
            logger.info(f"Waiting for response on replacement {new_id}...")
            actions = wait_for_responses()
            if actions:
                process_actions(actions, [new_id])
        
        time.sleep(1)
    
    # === FINAL CLEANUP ===
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM predictions WHERE status != 'approved'")
    conn.commit()
    
    final_count = conn.execute("SELECT COUNT(*) FROM predictions WHERE status='approved'").fetchone()[0]
    conn.close()
    
    logger.info(f"Complete! {final_count} approved entries.")
    send_message(f"✅ Alle Eintraege bestaetigt! ({final_count} Eintraege)")


if __name__ == "__main__":
    run_confirmation()
