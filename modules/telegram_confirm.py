#!/usr/bin/env python3
"""Module 2.5: Telegram confirmation, quality check, skip week, redo day, custom entry, summary."""

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
            "SELECT id, week, department, day, task, hours FROM predictions WHERE status='pending' AND day=? ORDER BY id",
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
        logger.info(f"Cleared update queue up to {last_id}")


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
        message = update.get("message")
        if message and message.get("text"):
            # This is a text message — handle custom entries
            pass
    
    return results


def wait_for_responses():
    while True:
        actions = check_responses()
        if actions:
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
            logger.info("All approved for this day")
            
        elif action_str.startswith("redo_"):
            day = action_str.replace("redo_", "")
            conn.execute("DELETE FROM predictions WHERE status='pending' AND day=?", (day,))
            conn.execute("UPDATE predictions SET status='rejected' WHERE status='approved' AND day=? AND week=(SELECT MAX(week) FROM predictions)", (day,))
            logger.info(f"Redoing entire day: {day}")
            new_text = original_text + f"\n\n🔄 {day} wird neu generiert..."
            
        elif action_str.startswith("skip_week_"):
            week = int(action_str.replace("skip_week_", ""))
            conn.execute("DELETE FROM predictions WHERE week=? AND status='pending'", (week,))
            conn.execute("INSERT INTO predictions (week, department, day, task, hours, status) VALUES (?, 'Skipped', 'Montag', 'Skipped', 8.0, 'approved')", (week,))
            conn.execute("INSERT INTO predictions (week, department, day, task, hours, status) VALUES (?, 'Skipped', 'Dienstag', 'Skipped', 8.0, 'approved')", (week,))
            conn.execute("INSERT INTO predictions (week, department, day, task, hours, status) VALUES (?, 'Skipped', 'Mittwoch', 'Skipped', 8.0, 'approved')", (week,))
            conn.execute("INSERT INTO predictions (week, department, day, task, hours, status) VALUES (?, 'Skipped', 'Donnerstag', 'Skipped', 8.0, 'approved')", (week,))
            conn.execute("INSERT INTO predictions (week, department, day, task, hours, status) VALUES (?, 'Skipped', 'Freitag', 'Skipped', 5.5, 'approved')", (week,))
            logger.info(f"Week {week} skipped")
            new_text = original_text + f"\n\n⏭️ Woche {week} uebersprungen"
            
        elif action_str.startswith("ok_") or action_str.startswith("no_"):
            action, pid = action_str.split("_", 1)
            pid = int(pid)
            if action == "ok":
                conn.execute("UPDATE predictions SET status='approved' WHERE id=?", (pid,))
                logger.info(f"Approved: {pid}")
                new_text = original_text.replace("✅", "✅ ", 1) + "\n✅ Genehmigt"
            elif action == "no":
                conn.execute("UPDATE predictions SET status='rejected' WHERE id=?", (pid,))
                logger.info(f"Rejected: {pid}")
                new_text = original_text.replace("❌", "❌ ", 1) + "\n❌ Abgelehnt"
        
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
        {"text": "✅ ALLE genehmigen", "callback_data": f"dayok_{items[0][0]}"},
        {"text": "🔄 Tag wiederholen", "callback_data": f"redo_{day}"},
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


def handle_rejection(pid):
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


def run_confirmation():
    logger.info("Starting confirmation...")
    
    # Send skip week button for the first pending week
    conn = sqlite3.connect(str(DB_PATH))
    first_week = conn.execute(
        "SELECT DISTINCT week FROM predictions WHERE status='pending' ORDER BY week LIMIT 1"
    ).fetchone()
    conn.close()
    
    if first_week:
        week = first_week[0]
        keyboard = {"inline_keyboard": [[
            {"text": "⏭️ WOCHE ÜBERSPRINGEN", "callback_data": f"skip_week_{week}"}
        ]]}
        requests.post(f"{API_URL}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": f"Woche {week} — zum Überspringen klicken:",
            "reply_markup": json.dumps(keyboard)
        })
    
    # Custom entry prompt
    conn = sqlite3.connect(str(DB_PATH))
    week_dept = conn.execute(
        "SELECT DISTINCT week, department FROM predictions WHERE status='pending' LIMIT 1"
    ).fetchone()
    conn.close()
    
    if week_dept:
        send_message(f"✏️ Eigener Eintrag? Antworte mit: custom W{week_dept[0]} Montag 3.0 Meine Aufgabe")
    
    clear_update_queue()
    
    # Initial pass
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
            remaining = get_pending_for_day(day)
            if remaining > 0:
                logger.info(f"{day} still has {remaining} pending, resending...")
                continue
        
        logger.info(f"{day} complete")
        time.sleep(1)
    
    logger.info("Initial pass complete")
    
    # Handle rejections
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
    
    # Final cleanup
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM predictions WHERE status != 'approved'")
    conn.commit()
    
    final_count = conn.execute("SELECT COUNT(*) FROM predictions WHERE status='approved'").fetchone()[0]
    conn.close()
    
    logger.info(f"Complete! {final_count} approved entries.")
    send_weekly_summary()


def send_weekly_summary():
    """Send a summary of all approved entries for the most recent week."""
    conn = sqlite3.connect(str(DB_PATH))
    week = conn.execute(
        "SELECT week FROM predictions WHERE status='approved' ORDER BY week DESC LIMIT 1"
    ).fetchone()
    
    if not week:
        conn.close()
        return
    
    week = week[0]
    rows = conn.execute(
        "SELECT day, task, hours FROM predictions WHERE status='approved' AND week=? ORDER BY CASE day WHEN 'Montag' THEN 1 WHEN 'Dienstag' THEN 2 WHEN 'Mittwoch' THEN 3 WHEN 'Donnerstag' THEN 4 WHEN 'Freitag' THEN 5 END",
        (week,)
    ).fetchall()
    conn.close()
    
    text = f"📋 <b>Wochenübersicht — Woche {week}</b>\n\n"
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


# ========== QUALITY CHECK ==========

def run_quality_check(week):
    """Check all approved entries for a week for spelling, grammar, and English translation."""
    logger.info(f"Running quality check on week {week}")
    
    conn = sqlite3.connect(str(DB_PATH))
    entries = conn.execute(
        "SELECT id, day, task FROM predictions WHERE status='approved' AND week=? ORDER BY id",
        (week,)
    ).fetchall()
    conn.close()
    
    if not entries:
        return
    
    corrections = []
    
    for pid, day, task in entries:
        # Simple English detection
        english_words = ["the", "and", "made", "tested", "built", "fixed", "worked", "cleaned"]
        is_english = any(w in task.lower().split() for w in english_words)
        
        if is_english:
            # Translate
            from predict_activities import translate_entry
            corrected = translate_entry(task)
            if corrected and corrected != task:
                corrections.append((pid, week, day, task, corrected, "translation"))
        
        # Spell/grammar check
        from predict_activities import check_spelling
        corrected = check_spelling(task)
        if corrected and corrected != task:
            corrections.append((pid, week, day, task, corrected, "spelling"))
    
    if corrections:
        send_corrections_batch(corrections)


def send_corrections_batch(corrections):
    """Send all corrections in one batch message."""
    text = "🔍 <b>Korrekturvorschläge</b>\n\n"
    keyboard = {"inline_keyboard": []}
    
    for i, (pid, week, day, original, corrected, ctype) in enumerate(corrections):
        label = "Übersetzung" if ctype == "translation" else "Rechtschreibung"
        text += f"<b>{label}</b> — {day}\n"
        text += f"<i>Original:</i> {original}\n"
        text += f"<i>Korrektur:</i> {corrected}\n\n"
        
        keyboard["inline_keyboard"].append([
            {"text": f"✅ {i+1}", "callback_data": f"ok_{pid}"},
            {"text": f"❌ {i+1}", "callback_data": f"no_{pid}"},
        ])
    
    keyboard["inline_keyboard"].append([
        {"text": "✅ ALLE", "callback_data": "corr_accept_all"}
    ])
    
    requests.post(f"{API_URL}/sendMessage", json={
        "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
        "reply_markup": json.dumps(keyboard)
    })


def run_custom_entry(week, day, hours, task, department=None):
    """Add a custom entry from Telegram text."""
    conn = sqlite3.connect(str(DB_PATH))
    if not department:
        dept_row = conn.execute("SELECT department FROM predictions WHERE week=? LIMIT 1", (week,)).fetchone()
        department = dept_row[0] if dept_row else "Unbekannt"
    
    conn.execute(
        "INSERT INTO predictions (week, department, day, task, hours, status) VALUES (?, ?, ?, ?, ?, 'approved')",
        (week, department, day, task, hours)
    )
    conn.commit()
    conn.close()
    logger.info(f"Custom entry added: Week {week}, {day}: {task} ({hours}h)")
    send_message(f"✅ Eigener Eintrag hinzugefügt: {day}: {task} ({hours}h)")


if __name__ == "__main__":
    run_confirmation()
