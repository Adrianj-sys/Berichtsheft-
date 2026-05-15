#!/usr/bin/env python3
"""V2 Diff Engine: Compare website HTML entries with database, deduplicate, import."""

import logging
import sqlite3
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"


def deduplicate_report(report_nr):
    """Remove duplicate entries — keep the one with lowest id."""
    conn = sqlite3.connect(str(DB_PATH))
    
    dupes = conn.execute("""
        SELECT day, LOWER(task), hours, MIN(id) as keep_id, COUNT(*) as cnt
        FROM predictions 
        WHERE report_nr=? AND status='approved'
        GROUP BY report_nr, day, LOWER(task), hours
        HAVING cnt > 1
    """, (report_nr,)).fetchall()
    
    deleted = 0
    for day, task_lower, hours, keep_id, cnt in dupes:
        conn.execute(
            "DELETE FROM predictions WHERE report_nr=? AND day=? AND LOWER(task)=? AND hours=? AND id != ? AND status='approved'",
            (report_nr, day, task_lower, hours, keep_id)
        )
        deleted += cnt - 1
    
    conn.commit()
    conn.close()
    
    if deleted:
        logger.info(f"Deduplicated {deleted} entries for report {report_nr}")
    return deleted


def get_db_entries(report_nr):
    """Get approved entries from database for a report."""
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT day, task, hours FROM predictions WHERE report_nr=? AND status='approved' AND task!='Skipped'",
        (report_nr,)
    ).fetchall()
    conn.close()
    
    entries = {}
    for day, task, hours in rows:
        if day not in entries:
            entries[day] = []
        entries[day].append({"task": task, "hours": hours})
    return entries


def compare(website_bericht, report_nr):
    """Compare website entries vs database entries."""
    db_entries = get_db_entries(report_nr)
    
    to_import = {}
    to_submit = {}
    matched = {}
    
    for day in ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]:
        web_day = website_bericht["days"].get(day, {})
        web_activities = web_day.get("activities", [])
        db_activities = db_entries.get(day, [])
        
        web_set = set()
        for a in web_activities:
            web_set.add((a["task"].strip().lower(), a["hours"]))
        
        db_set = set()
        for a in db_activities:
            db_set.add((a["task"].strip().lower(), a["hours"]))
        
        import_set = web_set - db_set
        if import_set:
            to_import[day] = [{"task": t, "hours": h} for t, h in import_set]
        
        submit_set = db_set - web_set
        if submit_set:
            to_submit[day] = [{"task": t, "hours": h} for t, h in submit_set]
        
        matched_set = web_set & db_set
        if matched_set:
            matched[day] = [{"task": t, "hours": h} for t, h in matched_set]
    
    return to_import, to_submit, matched


def auto_import_entries(report_nr, to_import, department):
    """Import website entries into database."""
    conn = sqlite3.connect(str(DB_PATH))
    imported = 0
    
    for day, activities in to_import.items():
        for a in activities:
            task = a["task"]
            hours = a["hours"]
            
            existing = conn.execute(
                "SELECT id FROM predictions WHERE report_nr=? AND day=? AND LOWER(task)=LOWER(?) AND hours=?",
                (report_nr, day, task, hours)
            ).fetchone()
            
            if not existing:
                conn.execute(
                    "INSERT INTO predictions (report_nr, department, day, task, original_task, hours, status, source) VALUES (?, ?, ?, ?, ?, ?, 'approved', 'website')",
                    (report_nr, department, day, task, task, hours)
                )
                imported += 1
                logger.info(f"Imported: {day}: {task[:50]} ({hours}h)")
    
    conn.commit()
    conn.close()
    return imported


if __name__ == "__main__":
    from html_parser import parse_weekly_overview
    
    report_nr = 139
    bericht = parse_weekly_overview(report_nr)
    if bericht:
        deduplicate_report(report_nr)
        to_import, to_submit, matched = compare(bericht, report_nr)
        print(f"Import: {sum(len(v) for v in to_import.values())}")
        print(f"Submit: {sum(len(v) for v in to_submit.values())}")
        print(f"Matched: {sum(len(v) for v in matched.values())}")
