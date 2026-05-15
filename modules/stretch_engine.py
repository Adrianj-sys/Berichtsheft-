#!/usr/bin/env python3
"""V2 Stretch Engine: Extend entries capped at daily target."""

import logging
import sqlite3
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
TARGET_HOURS = {"Montag": 8.0, "Dienstag": 8.0, "Mittwoch": 8.0, "Donnerstag": 8.0, "Freitag": 5.5}


def stretch_entries(bericht, to_submit):
    results = {}
    
    for day_name in ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]:
        day_info = bericht["days"].get(day_name, {})
        target = TARGET_HOURS.get(day_name, 8.0)
        current = day_info.get("total_hours", 0)
        
        if current >= target or day_info.get("status") == "special":
            results[day_name] = {"action": "none", "hours_needed": 0, "stretched": [], "still_needed": 0}
            continue
        
        deficit = target - current
        submit_activities = to_submit.get(day_name, [])
        
        if not submit_activities and deficit > 0:
            results[day_name] = {"action": "generate", "hours_needed": deficit, "stretched": [], "still_needed": deficit}
            continue
        
        stretched = []
        remaining = deficit
        
        for a in submit_activities:
            if remaining <= 0:
                break
            max_stretch = min(a["hours"] * 4, target)  # Cap at daily target
            can_add = max_stretch - a["hours"]
            if can_add > 0:
                add = min(can_add, remaining)
                new_hours = round((a["hours"] + add) * 2) / 2
                stretched.append({"task": a["task"], "original_hours": a["hours"], "stretched_hours": new_hours})
                remaining -= (new_hours - a["hours"])
        
        still_needed = max(0, round(remaining * 2) / 2)
        
        results[day_name] = {
            "action": "stretch" if stretched else "generate",
            "hours_needed": deficit,
            "stretched": stretched,
            "still_needed": still_needed,
        }
    
    return results


def apply_stretch(report_nr, department, day_name, stretched_entries):
    conn = sqlite3.connect(str(DB_PATH))
    
    for s in stretched_entries:
        conn.execute(
            "UPDATE predictions SET hours=? WHERE report_nr=? AND day=? AND LOWER(task)=LOWER(?) AND status='approved'",
            (s["stretched_hours"], report_nr, day_name, s["task"])
        )
        logger.info(f"Stretched: {day_name}: {s['task'][:40]}... {s['original_hours']}h → {s['stretched_hours']}h")
    
    conn.commit()
    conn.close()
