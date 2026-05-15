#!/usr/bin/env python3
"""Module: Parse Azubiheft weekly overview HTML into structured Bericht object."""

import re
import logging
import pickle
from pathlib import Path
import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BASE_URL = "https://www.azubiheft.de"
DAY_NAMES = {
    "Montag": "Montag", "Dienstag": "Dienstag", "Mittwoch": "Mittwoch",
    "Mitwoch": "Mittwoch", "Donnerstag": "Donnerstag", "Freitag": "Freitag",
}
SPECIAL_DAYS = ["Feiertag", "Urlaub", "Arbeitsunfähig", "Arbeitsunfaehig"]
TARGET_HOURS = {"Montag": 8.0, "Dienstag": 8.0, "Mittwoch": 8.0, "Donnerstag": 8.0, "Freitag": 5.5}

COOKIE_FILE = Path(__file__).parent.parent / "auth" / "session.pkl"


def _get_session():
    """Create a requests session with saved cookies."""
    if not COOKIE_FILE.exists():
        logger.error("No session cookie file found. Run login first.")
        return None
    
    with open(COOKIE_FILE, 'rb') as f:
        cookies = pickle.load(f)
    
    session = requests.Session()
    session.cookies.update(cookies)
    return session


def parse_hours(text):
    """Convert '02:00' to 2.0."""
    match = re.search(r'(\d{2}):(\d{2})', text)
    if match:
        return int(match.group(1)) + int(match.group(2)) / 60.0
    return 0.0


def parse_weekly_overview(report_nr):
    """Fetch and parse the weekly overview page for a report."""
    session = _get_session()
    if not session:
        return None
    
    url = f"{BASE_URL}/Azubi/Wochenansicht.aspx?NachweisNr={report_nr}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": f"{BASE_URL}/Azubi/Default.aspx"}
    
    try:
        resp = session.get(url, headers=headers, timeout=15)
    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None
    
    if "Login" in resp.url:
        logger.error("Session expired. Re-login needed.")
        return None
    
    soup = BeautifulSoup(resp.text, 'html.parser')
    
    # Find all day blocks
    day_blocks = soup.find_all('div', class_='mo', onclick=re.compile(r'openUrl.*WriteBericht'))
    
    if not day_blocks:
        logger.error(f"No day blocks found for report {report_nr}")
        return None
    
    # Extract header info
    header_text = soup.get_text()
    
    report_nr_match = re.search(r'Ausbildungsnachweis\s*Nr\.\s*(\d+)', header_text)
    kw_match = re.search(r'KW:\s*(\d+)', header_text)
    zeitraum_match = re.search(r'Zeitraum:\s*([^\n]+)', header_text)
    dept_match = re.search(r'Abteilung:\s*([^\n]+)', header_text)
    jahr_match = re.search(r'Ausbildungsjahr:\s*([^\n]+)', header_text)
    betrieb_match = re.search(r'Betrieb:\s*([^\n]+)', header_text)
    
    bericht = {
        "report_nr": int(report_nr_match.group(1)) if report_nr_match else report_nr,
        "week": int(kw_match.group(1)) if kw_match else None,
        "zeitraum": zeitraum_match.group(1).strip() if zeitraum_match else None,
        "department": dept_match.group(1).strip() if dept_match else None,
        "ausbildungsjahr": jahr_match.group(1).strip() if jahr_match else None,
        "betrieb": betrieb_match.group(1).strip() if betrieb_match else None,
        "days": {},
    }
    
    # Parse each day block
    ordered_days = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]
    found_days = set()
    
    for block in day_blocks:
        raw_text = block.get_text(strip=True)
        
        # Extract day name
        day_name = None
        for name in DAY_NAMES:
            if raw_text.startswith(name):
                day_name = DAY_NAMES[name]
                break
        
        if not day_name:
            continue
        
        found_days.add(day_name)
        
        # Extract art (Betrieb/Schule/Feiertag/etc)
        art_match = re.search(r'Art:\s*(\S+)', raw_text)
        art = art_match.group(1) if art_match else "Betrieb"
        
        # Extract abt
        abt_match = re.search(r'Abt\.?:\s*([^\n]+?)(?:\s+Art:|$)', raw_text)
        abt = abt_match.group(1).strip() if abt_match else None
        
        # Parse activities: pattern is "Abt: --- HH:MM task text"
        activities = []
        
        # Find all time+task patterns
        pattern = re.finditer(r'(?:Art:\s*\S+\s*)?Abt:\s*---?\s*(\d{2}:\d{2})\s*(.+?)(?=Art:|Abt:|Summe:|$)', raw_text)
        
        for match in pattern:
            time_str = match.group(1)
            task = match.group(2).strip()
            hours = parse_hours(time_str)
            
            if hours > 0 and task:
                activities.append({"task": task, "hours": hours})
        
        # Deduplicate
        seen = set()
        unique_activities = []
        for a in activities:
            key = (a["task"], a["hours"])
            if key not in seen:
                seen.add(key)
                unique_activities.append(a)
        
        total = sum(a["hours"] for a in unique_activities)
        is_special = any(a["task"] in SPECIAL_DAYS for a in unique_activities)
        
        if is_special:
            status = "special"
        elif total < TARGET_HOURS.get(day_name, 8.0):
            status = "partial"
        elif total > 0:
            status = "present"
        else:
            status = "empty"
        
        bericht["days"][day_name] = {
            "art": art,
            "abt": abt,
            "status": status,
            "total_hours": total,
            "target_hours": TARGET_HOURS.get(day_name, 8.0),
            "activities": unique_activities,
        }
    
    # Fill missing days
    for day_name in ordered_days:
        if day_name not in bericht["days"]:
            bericht["days"][day_name] = {
                "art": None, "abt": None,
                "status": "empty",
                "total_hours": 0.0,
                "target_hours": TARGET_HOURS.get(day_name, 8.0),
                "activities": [],
            }
    
    present = len([d for d in ordered_days if bericht["days"][d]["status"] in ("present", "special")])
    partial = len([d for d in ordered_days if bericht["days"][d]["status"] == "partial"])
    empty = len([d for d in ordered_days if bericht["days"][d]["status"] == "empty"])
    
    logger.info(f"Report {report_nr}: {present} present, {partial} partial, {empty} empty")
    
    return bericht


if __name__ == "__main__":
    import sys
    from pprint import pprint
    
    report_nr = int(sys.argv[1]) if len(sys.argv) > 1 else 139
    result = parse_weekly_overview(report_nr)
    
    if result:
        for day_name in ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]:
            d = result["days"][day_name]
            print(f"  {day_name}: {d['status']} ({d['total_hours']}h / {d['target_hours']}h)")
            for a in d["activities"]:
                print(f"    - {a['task']} ({a['hours']}h)")
