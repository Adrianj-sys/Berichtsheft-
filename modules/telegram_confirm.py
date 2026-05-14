#!/usr/bin/env python3
"""Module 2.5: Telegram confirmation with batch regeneration, before/after display, timer."""

import json
import os
import logging
import sqlite3
import time
import html as html_module
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
DB_PATH = str(Path(__file__).parent.parent / "data" / "predictions.db")
API_URL = f"https://api.telegram.org/bot{TOKEN}"

DAY_ORDER = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]

last_interaction_time = time.time()
pre_process_triggered = False
current_report_nr = None


def _connect():
    return sqlite3.connect(DB_PATH, timeout=30)


def _escape(text):
    """Escape HTML special characters."""
    if not text:
        return ""
    return html_module.escape(text)


def get_pending_for_day(day, report_nr=None):
    conn = _connect()
    if report_nr:
        count = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE status='pending' AND day=? AND report_nr=?",
            (day, report_nr)
        ).fetchone()[0]
    else:
        count = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE status='pending' AND day=?",
            (day,)
        ).fetchone()[0]
    conn.close()
    return count


def get_next_day(report_nr=None):
    conn = _connect()
    for day in DAY_ORDER:
        if report_nr:
            rows = conn.execute(
                "SELECT id, report_nr, department, day, task, original_task, hours FROM predictions WHERE status='pending' AND day=? AND report_nr=? ORDER BY id",
                (day, report_nr)
            ).fetchall()
        else:
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
    try:
        requests.post(f"{API_URL}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
    except:
        pass


def clear_update_queue():
    try:
        resp = requests.get(f"{API_URL}/getUpdates", timeout=10)
        data = resp.json()
        if data.get("result"):
            last_id = data["result"][-1]["update_id"]
            requests.get(f"{API_URL}/getUpdates", params={"offset": last_id + 1}, timeout=10)
            offset_file = Path(__file__).parent.parent / "data" / "last_update.txt"
            offset_file.parent.mkdir(parents=True, exist_ok=True)
            offset_file.write_text(str(last_id))
    except:
        pass


def check_responses():
    offset_file = Path(__file__).parent.parent / "data" / "last_update.txt"
    offset_file.parent.mkdir(parents=True, exist_ok=True)
    offset = int(offset_file.read_text().strip()) if offset_file.exists() else 0
    
    try:
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
    except:
        return []


def wait_for_responses():
    global last_interaction_time
    while True:
        actions = check_responses()
        if actions:
            last_interaction_time = time.time()
            return actions
        time.sleep(3)


def process_actions(actions, current_day_pids):
    conn = _connect()
    
    for action_str, callback in actions:
        chat_id = callback["message"]["chat"]["id"]
        message_id = callback["message"]["message_id"]
        original_text = callback["message"].get("text", "")
        new_text = original_text
        
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
                new_text = original_text + f"\n\n🔄 {day} wird komplett neu generiert..."
            
        elif action_str.startswith("skip_report_"):
            report_nr = int(action_str.replace("skip_report_", ""))
            conn.execute("DELETE FROM predictions WHERE report_nr=?", (report_nr,))
            dept = conn.execute("SELECT department FROM predictions WHERE report_nr=? LIMIT 1", (report_nr,)).fetchone()
            dept = dept[0] if dept else "Skipped"
            for d in DAY_ORDER:
                target = 5.5 if d == "Freitag" else 8.0
                conn.execute(
                    "INSERT INTO predictions (report_nr, department, day, task, hours, status) VALUES (?, ?, ?, 'Skipped', ?, 'approved')",
                    (report_nr, dept, d, target)
                )
            new_text = original_text + f"\n\n⏭️ Bericht {report_nr} uebersprungen"
            
        elif "_" in action_str and not action_str.startswith("skip_") and not action_str.startswith("redo_"):
            parts = action_str.split("_", 1)
            if len(parts) == 2:
                action, pid = parts
                try:
                    pid = int(pid)
                    if action == "ok":
                        conn.execute("UPDATE predictions SET status='approved' WHERE id=?", (pid,))
                        new_text = original_text + "\n✅ Genehmigt"
                    elif action == "no":
                        conn.execute("UPDATE predictions SET status='rejected' WHERE id=?", (pid,))
                        new_text = original_text + "\n❌ Abgelehnt"
                except ValueError:
                    pass
        
        try:
            requests.post(f"{API_URL}/editMessageText", json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": new_text,
                "parse_mode": "HTML"
            }, timeout=10)
            requests.post(f"{API_URL}/answerCallbackQuery", json={
                "callback_query_id": callback["id"]
            }, timeout=10)
        except:
            pass
    
    conn.commit()
    conn.close()


def send_day(day, items):
    report_nr = items[0][1]
    lines = [f"<b>Bericht {report_nr} — {day}</b>\n"]
    for row in items:
        pid, rn, dept, dy, task, original, hours = row
        task_safe = _escape(task)
        if original and original.strip() and original != task:
            original_safe = _escape(original)
            lines.append(f"• <i>Original:</i> {original_safe}")
            lines.append(f"  ➜ <b>{task_safe}</b> <i>({hours}h)</i>")
        else:
            lines.append(f"• {task_safe} <i>({hours}h)</i>")
    text = "\n".join(lines)
    
    keyboard = {"inline_keyboard": []}
    for row in items:
        pid, rn, dept, dy, task, original, hours = row
        label = _escape(task[:25]) if task else "?"
        keyboard["inline_keyboard"].append([
            {"text": f"✅ {label}...", "callback_data": f"ok_{pid}"},
            {"text": "❌", "callback_data": f"no_{pid}"},
        ])
    keyboard["inline_keyboard"].append([
        {"text": "✅ ALLE genehmigen", "callback_data": f"dayok_{items[0][0]}"},
        {"text": "🔄 Tag wiederholen", "callback_data": f"redo_{day}"},
    ])
    
    try:
        requests.post(f"{API_URL}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(keyboard)
        }, timeout=10)
        logger.info(f"Sent day: {day} ({len(items)} entries)")
    except Exception as e:
        logger.error(f"Failed to send {day}: {e}")


def send_day_batch(day, items):
    report_nr = items[0][1]
    lines = [f"<b>Ersatz — Bericht {report_nr}, {day}</b>\n"]
    for row in items:
        pid, rn, dept, dy, task, original, hours = row
        lines.append(f"• {_escape(task)} <i>({hours}h)</i>")
    text = "\n".join(lines)
    
    keyboard = {"inline_keyboard": []}
    for row in items:
        pid, rn, dept, dy, task, original, hours = row
        label = _escape(task[:25]) if task else "?"
        keyboard["inline_keyboard"].append([
            {"text": f"✅ {label}...", "callback_data": f"ok_{pid}"},
            {"text": "❌", "callback_data": f"no_{pid}"},
        ])
    keyboard["inline_keyboard"].append([
        {"text": "✅ ALLE genehmigen", "callback_data": f"dayok_{items[0][0]}"},
    ])
    
    try:
        requests.post(f"{API_URL}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(keyboard)
        }, timeout=10)
        logger.info(f"Sent replacements batch: {day} ({len(items)} entries)")
    except Exception as e:
        logger.error(f"Failed to send batch {day}: {e}")


def handle_rejection_batch(report_nr, day, rejected_entries):
    conn = _connect()
    rejected_data = [(task, hours) for _, _, _, task, hours in rejected_entries]
    dept = conn.execute("SELECT department FROM predictions WHERE report_nr=? LIMIT 1", (report_nr,)).fetchone()[0]
    
    for pid, _, _, task, hours in rejected_entries:
        conn.execute("DELETE FROM predictions WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    
    from predict_activities import regenerate_day
    replacements = regenerate_day(report_nr, day, rejected_data)
    
    if replacements:
        conn = _connect()
        for rep in replacements:
            conn.execute(
                "INSERT INTO predictions (report_nr, department, day, task, hours, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                (report_nr, dept, day, rep["task"], rep["hours"])
            )
        conn.commit()
        conn.close()
        logger.info(f"Generated {len(replacements)} replacements for {day}")
    
    return replacements


def run_confirmation(report_nr=None):
    global last_interaction_time, pre_process_triggered, current_report_nr
    logger.info(f"Starting confirmation for report {report_nr}...")
    last_interaction_time = time.time()
    pre_process_triggered = False
    
    if report_nr:
        current_report_nr = report_nr
    
    if not current_report_nr:
        conn = _connect()
        row = conn.execute(
            "SELECT DISTINCT report_nr FROM predictions WHERE status='pending' ORDER BY report_nr LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            current_report_nr = row[0]
    
    if not current_report_nr:
        logger.info("No pending reports found")
        return
    
    logger.info(f"Processing report {current_report_nr}")
    
    try:
        keyboard = {"inline_keyboard": [[
            {"text": "⏭️ BERICHT ÜBERSPRINGEN", "callback_data": f"skip_report_{current_report_nr}"}
        ]]}
        requests.post(f"{API_URL}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": f"Bericht {current_report_nr} — zum Überspringen klicken:",
            "reply_markup": json.dumps(keyboard),
            "parse_mode": "HTML"
        }, timeout=10)
    except:
        pass
    
    clear_update_queue()
    
    all_days = []
    seen_days = set()
    while True:
        day, items = get_next_day(current_report_nr)
        if not items or day in seen_days:
            break
        seen_days.add(day)
        all_days.append((day, items))
        logger.info(f"  Found {day}: {len(items)} entries")
    
    if not all_days:
        logger.info("No pending days found for this report")
        return
    
    for day, items in all_days:
        send_day(day, items)
        time.sleep(1)
    
    for day, items in all_days:
        pids = [item[0] for item in items]
        logger.info(f"Waiting for {day}...")
        actions = wait_for_responses()
        if actions:
            process_actions(actions, pids)
        logger.info(f"{day} complete")
    
    logger.info("Initial pass complete")
    
    while True:
        conn = _connect()
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
                conn = _connect()
                new_rows = conn.execute(
                    "SELECT id, report_nr, department, day, task, original_task, hours FROM predictions WHERE status='pending' AND report_nr=? AND day=? ORDER BY id",
                    (current_report_nr, day)
                ).fetchall()
                conn.close()
                
                if new_rows:
                    send_day_batch(day, new_rows)
                    actions = wait_for_responses()
                    if actions:
                        pids = [r[0] for r in new_rows]
                        process_actions(actions, pids)
        
        time.sleep(1)
    
    conn = _connect()
    conn.execute("DELETE FROM predictions WHERE status != 'approved' AND report_nr=?", (current_report_nr,))
    conn.commit()
    
    final_count = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE status='approved' AND report_nr=? AND task != 'Skipped'",
        (current_report_nr,)
    ).fetchone()[0]
    conn.close()
    
    logger.info(f"Complete! {final_count} approved entries for report {current_report_nr}.")
    send_weekly_summary(current_report_nr)


def send_weekly_summary(report_nr):
    conn = _connect()
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
        text += f"  • {_escape(task)} <i>({hours}h)</i>\n"
        day_total += hours
    
    if current_day:
        text += f"  ─────────\n  <b>{day_total}h</b>\n"
    
    total = sum(h for _, _, h in rows)
    text += f"\n<b>Gesamt: {total}h</b>"
    
    send_message(text)


def notify_empty(report_nr):
    send_message(f"📭 Bericht {report_nr} ist leer (keine Berichte) und wurde übersprungen.")


def notify_backlog_start(report_nr):
    send_message(f"⏳ Keine Antwort seit 10 Minuten. KI beginnt mit Vorverarbeitung des Backlogs ab Bericht {report_nr}...")


if __name__ == "__main__":
    run_confirmation()
