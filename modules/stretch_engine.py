#!/usr/bin/env python3
"""V2 Stretch Engine: Extend existing entries or flag for AI generation."""

import logging
import sqlite3
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
TARGET_HOURS = {"Montag": 8.0, "Dienstag": 8.0, "Mittwoch": 8.0, "Donnerstag": 8.0, "Freitag": 5.5}


def stretch_entries(bericht):
    """For each day with insufficient hours, calculate stretch or generate."""
    results = {}
    
    for day_name, day_info in bericht["days"].items():
        target = TARGET_HOURS.get(day_name, 8.0)
        current = day_info["total_hours"]
        
        if current >= target or day_info["status"] == "special":
            results[day_name] = {"action": "none", "hours_needed": 0, "stretched": [], "still_needed": 0}
            continue
        
        deficit = target - current
        activities = day_info["activities"]
        
        if not activities:
            results[day_name] = {"action": "generate", "hours_needed": deficit, "stretched": [], "still_needed": deficit}
            continue
        
        stretched = []
        remaining = deficit
        
        for a in activities:
            if remaining <= 0:
                break
            max_stretch = a["hours"] * 4
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
    """Update database with stretched hours."""
    conn = sqlite3.connect(str(DB_PATH))
    
    for s in stretched_entries:
        conn.execute(
            "UPDATE predictions SET hours=? WHERE report_nr=? AND day=? AND task=? AND status='approved'",
            (s["stretched_hours"], report_nr, day_name, s["task"])
        )
        logger.info(f"Stretched: {day_name}: {s['task'][:40]}... {s['original_hours']}h → {s['stretched_hours']}h")
    
    conn.commit()
    conn.close()


if __name__ == "__main__":
    from html_parser import parse_weekly_overview
    
    bericht = parse_weekly_overview(139)
    if bericht:
        results = stretch_entries(bericht)
        for day, info in results.items():
            print(f"{day}: {info['action']} — need {info['hours_needed']}h, still need {info['still_needed']}h")
            for s in info.get('stretched', []):
                print(f"  {s['task'][:50]}... {s['original_hours']}h → {s['stretched_hours']}h")
