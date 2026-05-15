#!/usr/bin/env python3
"""V2 Orchestrator: 30-minute cycle — read HTML, diff, stretch, submit."""

import logging
import sqlite3
import json
import subprocess
from pathlib import Path
from html_parser import parse_weekly_overview
from diff_engine import compare, auto_import_entries
from stretch_engine import stretch_entries, apply_stretch
from predict_activities import init_db
from telegram_confirm import send_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
DESKTOP = "adria@192.168.178.38"
SSH_KEY = "~/.ssh/berichtsheft_key"
FORM_FILL_SCRIPT = "C:\\Users\\adria\\Documents\\Berichtsheft\\Berichtsheft-\\desktop_modules\\form_fill.py"


def get_unprocessed_reports():
    """Get reports that don't have all 5 days complete."""
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("""
        SELECT report_nr FROM predictions 
        WHERE status='approved' AND task!='Skipped'
        GROUP BY report_nr 
        HAVING COUNT(DISTINCT day) < 5
        ORDER BY report_nr
    """).fetchall()
    conn.close()
    return [r[0] for r in rows]


def submit_to_website(report_nr):
    """Submit approved entries to website via Desktop form fill."""
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
    remote_json = f"C:\\Users\\adria\\Documents\\Berichtsheft\\shared\\form_fill_{report_nr}.json"
    
    with open(local_json, "w") as f:
        json.dump(entries, f)
    
    subprocess.run(f"scp -i {SSH_KEY} {local_json} {DESKTOP}:{remote_json}", shell=True, capture_output=True)
    result = subprocess.run(
        f'ssh -i {SSH_KEY} {DESKTOP} "python {FORM_FILL_SCRIPT} {report_nr} {remote_json}"',
        shell=True, capture_output=True, text=True
    )
    
    logger.info(f"Form fill result: {result.stdout[:200] if result.stdout else 'no output'}")
    return True


def process_report(report_nr):
    """V2 pipeline: read HTML → compare → import → stretch → submit."""
    logger.info(f"Processing report {report_nr}")
    
    # 1. Read website
    bericht = parse_weekly_overview(report_nr)
    if not bericht:
        return
    
    # 2. Compare with database
    to_import, to_submit, matched = compare(bericht, report_nr)
    logger.info(f"  Import: {sum(len(v) for v in to_import.values())}, Submit: {sum(len(v) for v in to_submit.values())}, Matched: {sum(len(v) for v in matched.values())}")
    
    # 3. Import website entries
    if to_import:
        imported = auto_import_entries(report_nr, to_import, bericht.get("department", ""))
        logger.info(f"  Imported {imported} website entries")
    
    # 4. Stretch insufficient entries
    stretch_results = stretch_entries(bericht)
    for day, info in stretch_results.items():
        if info["stretched"]:
            apply_stretch(report_nr, bericht.get("department", ""), day, info["stretched"])
    
    # 5. Submit to website
    if to_submit or any(info["stretched"] for info in stretch_results.values()):
        logger.info(f"  Submitting to website...")
        submit_to_website(report_nr)
    
    logger.info(f"  Report {report_nr} complete")


def run_cycle():
    """One 30-minute cycle."""
    logger.info("Starting V2 cycle...")
    
    reports = get_unprocessed_reports()
    
    if not reports:
        logger.info("All reports complete")
        return
    
    logger.info(f"Found {len(reports)} incomplete reports")
    
    for report_nr in reports[:10]:
        try:
            process_report(report_nr)
        except Exception as e:
            logger.error(f"Failed report {report_nr}: {e}")


if __name__ == "__main__":
    run_cycle()
