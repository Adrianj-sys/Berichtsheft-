#!/usr/bin/env python3
"""Module 2.5: Telegram confirmation with batch regeneration, before/after display, timer."""

import json
import os
import logging
import sqlite3
import time
import threading
from collections import defaultdict
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

# Timer for background pre-processing
last_interaction_time = time.time()
pre_process_triggered = False
current_report_nr = None


# ========== HELPERS ==========

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
            "SELECT id, report_nr, department, day, task, original_task, hours FROM predictions WHERE status='pending' AND day=? ORDER BY id",
            (day,)
        ).fetchall()
        if rows:
            conn.close()
            return day, rows
    conn.close()
    return None, []


def send_message(text):
    requests.post(f"{API_URL}/sendMessage", json={"chat_id": CHAT_ID, "text": text})


def clear_update_queue():
    resp = requests.get(f"{API_URL}/getUpdates", timeout=10)
    data = resp.json()
    if data.get("result"):
        last_id = data["result"][-1]["update_id"]
        requests.get(f"{API_URL}/getUpdates", params={"offset": last_id + 1}, timeout=10)
        offset_file = Path(__file__).parent.parent / "data" / "last_update.txt"
        offset_file.parent.mkdir(parents=True, exist_ok=True)
        offset_file.write_text(str(last_id))


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
            results.append((callback["data"], callback))
    
    return results


def wait_for_responses():
    global last_interaction_time
    while True:
        actions = check_responses()
        if actions:
            last_interaction_time = time.time()
            return actions
        time.sleep(3)


def process_actions(actions, current_day_pids):
    conn = sqlite3.connect(str(DB_PATH))
    
    for action_str, callback in actions:
        chat_id = callback["message"]["chat"]["id"]
        message_id = callback["message"]["message_id"]
        original_text = callback["message"].get("text", "")
        
        if action_str.startswith("dayok_"):
            for pid in current_day_pids:
                conn.execute("UPDATE predictions SET status='approved' WHERE id=?", (pid,))
            new_text = original_text + "\n\n✅ Alle genehmigt!"
            
        elif action_str.startswith("redo_"):
            day = action_str.replace("redo_", "")
            report_row = conn.execute(
                "SELECT DISTINCT report_nr FROM predictions WHERE day=? AND status IN ('pending','approved') LIMIT 1",
                (day,)
            ).fetchone()
            if report_row:
                report_nr = report_row[0]
                conn.execute("DELETE FROM predictions WHERE day=? AND report_nr=?", (day, report_nr))
                dept = conn.execute("SELECT department FROM predictions WHERE report_nr=? LIMIT 1", (report_nr,)).fetchone()[0]
                conn.execute(
                    "INSERT INTO predictions (report_nr, department, day, task, hours, status) VALUES (?, ?, ?, 'Neu generieren', 0.0, 'pending')",
                    (report_nr, dept, day)
                )
                logger.info(f"Redoing entire day: Report {report_nr}, {day}")
                new_text = original_text + f"\n\n🔄 {day} wird komplett neu generiert..."
            
        elif action_str.startswith("skip_report_"):
            report_nr = int(action_str.replace("skip_report_", ""))
            conn.execute("DELETE FROM predictions WHERE report_nr=?", (report_nr,))
            dept = conn.execute("SELECT department FROM predictions WHERE report_nr=? LIMIT 1", (report_nr,)).fetchone()
            dept = dept[0] if dept else "Skipped"
            week = conn.execute("SELECT week FROM predictions WHERE report_nr=? LIMIT 1", (report_nr,)).fetchone()
            week = week[0] if week else 0
            for d in DAY_ORDER:
                target = 5.5 if d == "Freitag" else 8.0
                conn.execute(
                    "INSERT INTO predictions (report_nr, week, department, day, task, hours, status) VALUES (?, ?, ?, ?, 'Skipped', ?, 'approved')",
                    (report_nr, week, dept, d, target)
                )
            logger.info(f"Report {report_nr} skipped")
            new_text = original_text + f"\n\n⏭️ Bericht {report_nr} uebersprungen"
            
        elif action_str.startswith("ok_") or action_str.startswith("no_"):
            parts = action_str.split("_", 1)
            if len(parts) == 2:
                action, pid = parts
                pid = int(pid)
                if action == "ok":
                    conn.execute("UPDATE predictions SET status='approved' WHERE id=?", (pid,))
                    logger.info(f"Approved: {pid}")
                    new_text = original_text + "\n✅ Genehmigt"
                elif action == "no":
                    conn.execute("UPDATE predictions SET status='rejected' WHERE id=?", (pid,))
                    logger.info(f"Rejected: {pid}")
                    new_text = original_text + "\n❌ Abgelehnt"
        
        requests.post(f"{API_URL}/editMessageText", json={
            "chat_id": chat_id, "message_id": message_id,
            "text": new_text, "parse_mode": "HTML"
        })
        requests.post(f"{API_URL}/answerCallbackQuery", json={
            "callback_query_id": callback["id"]
        })
    
    conn.commit()
    conn.close()


# ========== DAY CONFIRMATION ==========

def send_day(day, items):
    report_nr = items[0][1]
    lines = [f"<b>Bericht {report_nr} — {day}</b>\n"]
    for row in items:
        pid, rn, dept, dy, task, original, hours = row
        if original:
            lines.append(f"• <i>Original:</i> {original}")
            lines.append(f"  <i>Korrektur:</i> {task} <b>({hours}h)</b>")
        else:
            lines.append(f"• {task} <i>({hours}h)</i>")
    text = "\n".join(lines)
    
    keyboard = {"inline_keyboard": []}
    for row in items:
        pid, rn, dept, dy, task, original, hours = row
        keyboard["inline_keyboard"].append([
            {"text": f"✅ {task[:25]}...", "callback_data": f"ok_{pid}"},
            {"text": "❌", "callback_data": f"no_{pid}"},
        ])
    keyboard["inline_keyboard"].append([
        {"text": "✅ ALLE genehmigen", "callback_data": f"dayok_{items[0][0]}"},
        {"text": "🔄 Tag wiederholen", "callback_data": f"redo_{day}"},
    ])
    
    requests.post(f"{API_URL}/sendMessage", json={
        "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
        "reply_markup": json.dumps(keyboard)
    }, timeout=10)
    logger.info(f"Sent day: {day} ({len(items)} entries)")


def send_day_batch(day, items):
    """Send multiple replacement entries for a day in one message."""
    report_nr = items[0][1]
    lines = [f"<b>Ersatz — Bericht {report_nr}, {day}</b>\n"]
    for row in items:
        pid, rn, dept, dy, task, original, hours = row
        lines.append(f"• {task} <i>({hours}h)</i>")
    text = "\n".join(lines)
    
    keyboard = {"inline_keyboard": []}
    for row in items:
        pid, rn, dept, dy, task, original, hours = row
        keyboard["inline_keyboard"].append([
            {"text": f"✅ {task[:25]}...", "callback_data": f"ok_{pid}"},
            {"text": "❌", "callback_data": f"no_{pid}"},
        ])
    keyboard["inline_keyboard"].append([
        {"text": "✅ ALLE genehmigen", "callback_data": f"dayok_{items[0][0]}"},
    ])
    
    requests.post(f"{API_URL}/sendMessage", json={
        "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
        "reply_markup": json.dumps(keyboard)
    }, timeout=10)
    logger.info(f"Sent replacements batch: {day} ({len(items)} entries)")


def handle_rejection_batch(report_nr, day, rejected_entries):
    """Delete rejected entries and regenerate all at once."""
    conn = sqlite3.connect(str(DB_PATH))
    
    # Delete rejected entries
    for pid, _, _, _, _, _ in rejected_entries:
        conn.execute("DELETE FROM predictions WHERE id=?", (pid,))
    
    # Get the rejected tasks and hours for regeneration
    rejected_data = [(task, hours) for _, _, _, _, task, hours in rejected_entries]
    dept = conn.execute("SELECT department FROM predictions WHERE report_nr=? LIMIT 1", (report_nr,)).fetchone()[0]
    conn.commit()
    conn.close()
    
    from predict_activities import regenerate_day
    replacements = regenerate_day(report_nr, day, rejected_data)
    
    if replacements:
        conn = sqlite3.connect(str(DB_PATH))
        for rep in replacements:
            conn.execute(
                "INSERT INTO predictions (report_nr, department, day, task, hours, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                (report_nr, dept, day, rep["task"], rep["hours"])
            )
        conn.commit()
        conn.close()
        logger.info(f"Generated {len(replacements)} replacements for {day}")
    
    return replacements


# ========== MAIN CONFIRMATION FLOW ==========

def run_confirmation(report_nr=None):
    global last_interaction_time, pre_process_triggered, current_report_nr
    logger.info("Starting confirmation...")
    last_interaction_time = time.time()
    pre_process_triggered = False
    
    if report_nr:
        current_report_nr = report_nr
    
    # Get report_nr from first pending if not provided
    if not current_report_nr:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT DISTINCT report_nr FROM predictions WHERE status='pending' ORDER BY report_nr LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            current_report_nr = row[0]
    
    if current_report_nr:
        # Skip report button
        keyboard = {"inline_keyboard": [[
            {"text": "⏭️ BERICHT ÜBERSPRINGEN", "callback_data": f"skip_report_{current_report_nr}"}
        ]]}
        requests.post(f"{API_URL}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": f"Bericht {current_report_nr} — zum Überspringen klicken:",
            "reply_markup": json.dumps(keyboard)
        })
    
    clear_update_queue()
    
    # Collect all pending days
    all_days = []
    while True:
        day, items = get_next_day()
        if not items:
            break
        all_days.append((day, items))
    
    # Send all days at once
    for day, items in all_days:
        send_day(day, items)
        time.sleep(1)
    
    # Process each day
    for day, items in all_days:
        pids = [item[0] for item in items]
        logger.info(f"Waiting for {day}...")
        
        actions = wait_for_responses()
        if actions:
            process_actions(actions, pids)
        
        logger.info(f"{day} complete")
    
    logger.info("Initial pass complete")
    
    # Handle rejections — batch by day
    while True:
        conn = sqlite3.connect(str(DB_PATH))
        rejected = conn.execute(
            "SELECT id, report_nr, day, task, hours FROM predictions WHERE status='rejected' AND report_nr=? ORDER BY day, id",
            (current_report_nr,)
        ).fetchall()
        conn.close()
        
        if not rejected:
            break
        
        by_day = defaultdict(list)
        for pid, rn, day, task, hours in rejected:
            by_day[day].append((pid, rn, day, task, hours))
        
        for day, entries in by_day.items():
            logger.info(f"Regenerating {len(entries)} entries for {day}...")
            send_message(f"🔄 {len(entries)} abgelehnte Einträge für {day} werden neu generiert...")
            
            replacements = handle_rejection_batch(current_report_nr, day, entries)
            
            if replacements:
                conn = sqlite3.connect(str(DB_PATH))
                new_rows = conn.execute(
                    "SELECT id, report_nr, department, day, task, original_task, hours FROM predictions WHERE status='pending' AND report_nr=? AND day=? ORDER BY id",
                    (current_report_nr, day)
                ).fetchall()
                conn.close()
                
                if new_rows:
                    send_day_batch(day, new_rows)
                    logger.info(f"Waiting for response on {len(new_rows)} replacements for {day}...")
                    actions = wait_for_responses()
                    if actions:
                        pids = [r[0] for r in new_rows]
                        process_actions(actions, pids)
        
        time.sleep(1)
    
    # Final cleanup
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM predictions WHERE status != 'approved'")
    conn.commit()
    
    final_count = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE status='approved' AND report_nr=? AND task != 'Skipped'",
        (current_report_nr,)
    ).fetchone()[0]
    conn.close()
    
    logger.info(f"Complete! {final_count} approved entries for report {current_report_nr}.")
    send_weekly_summary(current_report_nr)


# ========== WEEKLY SUMMARY ==========

def send_weekly_summary(report_nr):
    """Send a summary of all approved entries for a report."""
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT day, task, hours FROM predictions WHERE status='approved' AND report_nr=? AND task != 'Skipped' ORDER BY CASE day WHEN 'Montag' THEN 1 WHEN 'Dienstag' THEN 2 WHEN 'Mittwoch' THEN 3 WHEN 'Donnerstag' THEN 4 WHEN 'Freitag' THEN 5 END",
        (report_nr,)
    ).fetchall()
    conn.close()
    
    if not rows:
        return
    
    text = f"📋 <b>Wochenübersicht — Bericht {report_nr}</b>\n\n"
    current_day = None
    day_total = 0
    
    for day, task, hours in rows:
        if day != current_day:
            if current_day:
                text += f"  ─────────\n  <b>{day_total}h</b>\n\n"
            current_day = day
            day_total = 0
            text += f"<b>{day}</b>\n"
        text += f"  • {task} <i>({hours}h)</i>\n"
        day_total += hours
    
    if current_day:
        text += f"  ─────────\n  <b>{day_total}h</b>\n"
    
    total = sum(h for _, _, h in rows)
    text += f"\n<b>Gesamt: {total}h</b>"
    
    send_message(text)


# ========== EMPTY WEEK NOTIFICATION ==========

def notify_empty(report_nr):
    send_message(f"📭 Bericht {report_nr} ist leer (keine Berichte) und wurde übersprungen.")


def notify_backlog_start(report_nr):
    send_message(f"⏳ Keine Antwort seit 10 Minuten. KI beginnt mit Vorverarbeitung des Backlogs ab Bericht {report_nr}...")


if __name__ == "__main__":
    run_confirmation()
