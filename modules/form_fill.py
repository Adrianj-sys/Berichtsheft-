#!/usr/bin/env python3
"""Trigger form fill on Desktop from Pi via temp file."""

import sys
import json
import sqlite3
import subprocess
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
DESKTOP = "adria@192.168.178.38"
SSH_KEY = "~/.ssh/berichtsheft_key"
DESKTOP_SCRIPT = "C:\\Users\\adria\\Documents\\Berichtsheft\\Berichtsheft-\\desktop_modules\\form_fill.py"


def get_entries(report_nr):
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT day, task, hours FROM predictions WHERE report_nr=? AND status='approved' AND task!='Skipped' ORDER BY CASE day WHEN 'Montag' THEN 1 WHEN 'Dienstag' THEN 2 WHEN 'Mittwoch' THEN 3 WHEN 'Donnerstag' THEN 4 WHEN 'Freitag' THEN 5 END, id",
        (report_nr,)
    ).fetchall()
    conn.close()
    
    entries = {}
    for day, task, hours in rows:
        if day not in entries:
            entries[day] = []
        entries[day].append({"task": task, "hours": hours})
    
    return entries


def trigger_desktop(report_nr):
    entries = get_entries(report_nr)
    
    if not entries:
        print(f"No approved entries for report {report_nr}")
        return
    
    print(f"Sending {len(entries)} days to Desktop...")
    
    # Write JSON to temp file on Pi
    local_tmp = f"/tmp/form_fill_{report_nr}.json"
    with open(local_tmp, "w") as f:
        json.dump(entries, f)
    
    # SCP the file to Desktop
    remote_tmp = f"C:\\Users\\adria\\Documents\\Berichtsheft\\shared\\form_fill_{report_nr}.json"
    scp_cmd = f"scp -i {SSH_KEY} {local_tmp} {DESKTOP}:{remote_tmp}"
    subprocess.run(scp_cmd, shell=True, capture_output=True)
    
    # Run Desktop script with file path
    ssh_cmd = f'ssh -i {SSH_KEY} {DESKTOP} "python {DESKTOP_SCRIPT} {report_nr} {remote_tmp}"'
    result = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(f"Errors: {result.stderr}")


if __name__ == "__main__":
    report_nr = int(sys.argv[1]) if len(sys.argv) > 1 else 139
    trigger_desktop(report_nr)
