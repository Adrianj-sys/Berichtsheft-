#!/usr/bin/env python3
"""V2 Orchestrator: Clear DB → Download HTML → Fix over-limit → AI fill → Submit."""

import logging
import sqlite3
import json
import subprocess
from pathlib import Path
from html_parser import parse_weekly_overview
from predict_activities import ask_ai, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
SHARED_DIR = "/home/adrian/Berichtsheft-/shared"

TARGET_HOURS = {"Montag": 8.0, "Dienstag": 8.0, "Mittwoch": 8.0, "Donnerstag": 8.0, "Freitag": 5.5}
DAY_ORDER = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]


def download_all_to_db(report_nr):
    """Clear DB for report, then download fresh from website."""
    logger.info(f"Downloading report {report_nr}...")
    
    # Clear ALL existing entries for this report
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM predictions WHERE report_nr=?", (report_nr,))
    conn.commit()
    conn.close()
    logger.info(f"Cleared database for report {report_nr}")
    
    bericht = parse_weekly_overview(report_nr)
    if not bericht:
        return None
    
    conn = sqlite3.connect(str(DB_PATH))
    
    for day_name in DAY_ORDER:
        day_info = bericht["days"].get(day_name, {})
        for a in day_info.get("activities", []):
            conn.execute(
                "INSERT INTO predictions (report_nr, department, day, task, original_task, hours, status, source) VALUES (?, ?, ?, ?, ?, ?, 'approved', 'website')",
                (report_nr, bericht.get("department", ""), day_name, a["task"], a["task"], a["hours"])
            )
    
    conn.commit()
    conn.close()
    
    logger.info(f"Saved website entries for report {report_nr}")
    return bericht


def fix_over_limit(bericht):
    """Remove entries until each day is exactly at target hours."""
    report_nr = bericht["report_nr"]
    conn = sqlite3.connect(str(DB_PATH))
    fixed = 0
    #needs to be Removed a half hour Reset is way too often it maybe updating it to sm like every 6 hours whenever it runs into a token limit 
    #could possibly have default of one hour 
    for day_name in DAY_ORDER:
        day_info = bericht["days"].get(day_name, {})
        total = day_info.get("total_hours", 0)
        target = TARGET_HOURS.get(day_name, 8.0)
        
        if total > target:
            db_entries = conn.execute(
                "SELECT id, task, hours FROM predictions WHERE report_nr=? AND day=? AND status='approved' ORDER BY id DESC",
                (report_nr, day_name)
            ).fetchall()
            
            current = total
            for entry_id, task, hours in db_entries:
                if current <= target:
                    break
                conn.execute("DELETE FROM predictions WHERE id=?", (entry_id,))
                current -= hours
                fixed += 1
                logger.info(f"  Removed: {day_name}: {task[:40]}... ({hours}h) — now {current}h")
    
    conn.commit()
    conn.close()
    
    if fixed:
        logger.info(f"Fixed {fixed} over-limit entries for report {report_nr}")
    return fixed


def get_surrounding_weeks(report_nr, count=3):
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    
    rows = conn.execute("""
        SELECT report_nr, day, task, hours FROM predictions 
        WHERE status='approved' AND task!='Skipped'
        AND report_nr >= ? AND report_nr <= ? AND report_nr != ?
        ORDER BY report_nr, CASE day WHEN 'Montag' THEN 1 WHEN 'Dienstag' THEN 2 WHEN 'Mittwoch' THEN 3 WHEN 'Donnerstag' THEN 4 WHEN 'Freitag' THEN 5 END
    """, (report_nr - count, report_nr + count, report_nr)).fetchall()
    
    conn.close()
    return rows


def ai_fill_gaps(bericht):
    report_nr = bericht["report_nr"]
    department = bericht.get("department", "")
    
    surrounding = get_surrounding_weeks(report_nr)
    
    context_text = ""
    for rn, day, task, hours in surrounding:
        context_text += f"  Bericht {rn}, {day}: {task} ({hours}h)\n"
    
    days_needing_ai = []
    for day_name in DAY_ORDER:
        day_info = bericht["days"].get(day_name, {})
        current_hours = day_info.get("total_hours", 0)
        target = TARGET_HOURS.get(day_name, 8.0)
        
        if current_hours < target:
            days_needing_ai.append({
                "day": day_name,
                "current": current_hours,
                "needed": target - current_hours,
                "existing": day_info.get("activities", [])
            })
    
    if not days_needing_ai:
        logger.info(f"Report {report_nr} is complete — no AI needed")
        return True
    
    logger.info(f"AI filling gaps for report {report_nr}: {len(days_needing_ai)} days")
    
    prompt = f"""Du bist ein Auszubildender. Fuelle die fehlenden Stunden fuer Bericht {report_nr}.

ABTEILUNG: {department}

UMGEBENDE BERICHTE (als Kontext, lerne den Stil):
{context_text}

FEHLENDE STUNDEN:
"""
    
    for d in days_needing_ai:
        prompt += f"\n{d['day']}: {d['current']}h vorhanden, benoetige {d['needed']}h zusaetzlich\n"
        if d["existing"]:
            prompt += "  Bestehende Eintraege (BEHALTEN):\n"
            for a in d["existing"]:
                prompt += f"    - {a['task']} ({a['hours']}h)\n"
    
    prompt += """
REGELN:
- Generiere NUR die fehlenden Stunden
- Montag-Donnerstag: 8h gesamt, Freitag: 5.5h gesamt
- EINFACHE Sprache, kurze Saetze
- Technische Taetigkeiten bevorzugen
- Jeder Eintrag 0.5h Schritte
- KEINE Feiertag/Urlaub/Schule
- Lerne den Schreibstil aus den umgebenden Berichten

Antworte mit JSON:
{"days": [{"day": "Montag", "activities": [{"task": "...", "hours": 2.0}]}]}"""
    
    result = ask_ai(prompt)
    
    if result:
        conn = sqlite3.connect(str(DB_PATH))
        for day_data in result.get("days", []):
            day_name = day_data["day"]
            for a in day_data.get("activities", []):
                conn.execute(
                    "INSERT INTO predictions (report_nr, department, day, task, hours, status, source) VALUES (?, ?, ?, ?, ?, 'approved', 'ai')",
                    (report_nr, department, day_name, a["task"], a["hours"])
                )
        conn.commit()
        conn.close()
        logger.info(f"AI generated entries for report {report_nr}")
        return True
    
    return False




def trigger_desktop(report_nr):
    """SCP JSON to Desktop shared folder for watcher."""
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT day, task, hours FROM predictions WHERE report_nr=? AND status='approved' AND task!='Skipped'",
        (report_nr,)
    ).fetchall()
    conn.close()
    
    if not rows:
        return False
    
    entries = {}
    for day, task, hours in rows:
        if day not in entries:
            entries[day] = []
        entries[day].append({"task": task, "hours": hours})
    
    local_json = f"/tmp/form_fill_{report_nr}.json"
    remote_json = f"C:/Users/adria/Documents/Berichtsheft/shared/form_fill_{report_nr}.json"
    
    with open(local_json, "w") as f:
        json.dump(entries, f)
    
    subprocess.run(
        f"scp -i ~/.ssh/berichtsheft_key {local_json} adria@192.168.178.38:\"{remote_json}\"",
        shell=True
    )
    
    logger.info(f"Sent trigger for report {report_nr}")
    return True




def process_report_full(report_nr):
    logger.info(f"=== Report {report_nr} ===")
    
    # 1. Download from website
    bericht = download_all_to_db(report_nr)
    if not bericht:
        return
    
    # 2. Fix over-limit hours
    fix_over_limit(bericht)
    
    # 3. Re-read from DATABASE (not website) to get updated state
    conn = sqlite3.connect(str(DB_PATH))
    db_rows = conn.execute(
        "SELECT day, task, hours FROM predictions WHERE report_nr=? AND status='approved'",
        (report_nr,)
    ).fetchall()
    conn.close()
    
    # Rebuild bericht from DB
    for day_name in DAY_ORDER:
        if day_name in bericht["days"]:
            bericht["days"][day_name]["activities"] = []
            bericht["days"][day_name]["total_hours"] = 0
    
    for day, task, hours in db_rows:
        if day in bericht["days"]:
            bericht["days"][day]["activities"].append({"task": task, "hours": hours})
    
    for day_name in DAY_ORDER:
        total = sum(a["hours"] for a in bericht["days"][day_name].get("activities", []))
        bericht["days"][day_name]["total_hours"] = total
    
    # 4. AI fill gaps
    ai_fill_gaps(bericht)
    
    # 5. Trigger Desktop
    trigger_desktop(report_nr)
    
    logger.info(f"=== Report {report_nr} complete ===")





if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        process_report_full(int(sys.argv[1]))
    else:
        for rn in range(60, 143):
            try:
                process_report_full(rn)
            except Exception as e:
                logger.error(f"Failed {rn}: {e}")

