#!/usr/bin/env python3
"""Module 2.3: Parse PDF into structured Bericht object using fixed positions."""

import re
import logging
from pathlib import Path
import fitz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

DOWNLOADS_DIR = Path(__file__).parent.parent / "downloads"

DAY_NAMES = {
    "Montag": "Montag",
    "Dienstag": "Dienstag",
    "Mittwoch": "Mittwoch",
    "Mitwoch": "Mittwoch",
    "Donnerstag": "Donnerstag",
    "Freitag": "Freitag",
}

SPECIAL_DAYS = ["Feiertag", "Urlaub", "Arbeitsunfähig", "Arbeitsunfaehig"]
TARGET_HOURS = {"Montag": 8.0, "Dienstag": 8.0, "Mittwoch": 8.0, "Donnerstag": 8.0, "Freitag": 5.5}


def parse_hours(text):
    text = text.strip()
    if ":" in text:
        h, m = text.split(":")
        return int(h) + int(m) / 60.0
    return 0.0


def is_date_line(line):
    return bool(re.match(r'\d{1,2}\.\s+(Jan|Feb|Mrz|Mär|Apr|Mai|Jun|Jul|Aug|Sep|Okt|Nov|Dez)\.?\s+\d{4}', line.strip()))


def parse_pdf(filepath):
    if not filepath.exists():
        logger.error(f"File not found: {filepath}")
        return None
    
    doc = fitz.open(str(filepath))
    text = ""
    for page in doc:
        page_text = page.get_text()
        if "keine Berichte" in page_text:
            logger.info(f"Empty week: {filepath.name}")
            doc.close()
            return None
        text += page_text
    doc.close()
    
    lines = text.split("\n")
    
    # --- Parse header ---
    header = {}
    for line in lines:
        if "Ausbildungsnachweis-Nr.:" in line:
            header["report_nr"] = int(line.split(":")[-1].strip())
        elif "KW:" in line:
            header["week"] = int(line.split("KW:")[-1].strip())
        elif "Zeitraum:" in line:
            header["zeitraum"] = line.split("Zeitraum:")[-1].strip()
        elif "Abteilung:" in line:
            header["department"] = line.split("Abteilung:")[-1].strip()
        if len(header) >= 4:
            break
    
    # --- Find date lines ---
    date_indices = [i for i, line in enumerate(lines) if is_date_line(line)]
    
    # --- Parse days ---
    days = {}
    for i in date_indices:
        if i + 4 >= len(lines):
            break
        
        raw_day = lines[i + 1].strip()
        day_name = DAY_NAMES.get(raw_day, raw_day)
        art = lines[i + 2].split(":", 1)[-1].strip()
        abt = lines[i + 3].split(":", 1)[-1].strip()
        
        activities = []
        j = i + 4
        
        while j < len(lines):
            line = lines[j].strip()
            
            if line.startswith("Gesamt:") or "Gesamtstunden:" in line or "--:--" in line or is_date_line(line):
                break
            
            if line:
                task = line
                j += 1
                if j < len(lines):
                    hours = parse_hours(lines[j])
                    if hours > 0:
                        activities.append({"task": task, "hours": hours})
            j += 1
        
        if day_name in ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]:
            days[day_name] = {"art": art, "abt": abt, "activities": activities}
    
    # --- Build Bericht object ---
    ordered_days = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]
    bericht = {
        "report_nr": header.get("report_nr"),
        "week": header.get("week"),
        "zeitraum": header.get("zeitraum"),
        "department": header.get("department"),
        "days": {},
    }
    
    for day_name in ordered_days:
        if day_name in days:
            day_data = days[day_name]
            total = sum(a["hours"] for a in day_data["activities"])
            is_special = any(a["task"] in SPECIAL_DAYS for a in day_data["activities"])
            
            if is_special:
                status = "special"
            elif total < TARGET_HOURS[day_name]:
                status = "partial"
            else:
                status = "present"
            
            bericht["days"][day_name] = {
                "art": day_data["art"],
                "abt": day_data["abt"],
                "status": status,
                "total_hours": total,
                "target_hours": TARGET_HOURS[day_name],
                "activities": day_data["activities"],
            }
        else:
            bericht["days"][day_name] = {
                "art": None,
                "abt": None,
                "status": "empty",
                "total_hours": 0.0,
                "target_hours": TARGET_HOURS[day_name],
                "activities": [],
            }
    
    # Print summary
    present = [d for d in ordered_days if bericht["days"][d]["status"] in ("present", "special")]
    partial = [d for d in ordered_days if bericht["days"][d]["status"] == "partial"]
    empty = [d for d in ordered_days if bericht["days"][d]["status"] == "empty"]
    
    logger.info(f"Report {header.get('report_nr')}, Week {header.get('week')}: "
                f"{len(present)} present, {len(partial)} partial, {len(empty)} empty")
    
    return bericht


if __name__ == "__main__":
    import sys
    from pprint import pprint
    
    if len(sys.argv) > 1:
        filepath = DOWNLOADS_DIR / f"report_{sys.argv[1]}.pdf"
    else:
        pdfs = sorted(DOWNLOADS_DIR.glob("report_*.pdf"))
        if not pdfs:
            print("No PDFs found")
            sys.exit(1)
        filepath = pdfs[-1]
    
    result = parse_pdf(filepath)
    if result:
        for day_name in ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]:
            d = result["days"][day_name]
            print(f"  {day_name}: {d['status']} ({d['total_hours']}h / {d['target_hours']}h)")
