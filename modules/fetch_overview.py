#!/usr/bin/env python3
"""Fetch the reports overview page."""

import pickle
import requests
from pathlib import Path

AUTH_DIR = Path(__file__).parent.parent / "auth"
SESSION_FILE = AUTH_DIR / "session.pkl"

with open(SESSION_FILE, "rb") as f:
    cookies = pickle.load(f)

session = requests.Session()
session.cookies.update(cookies)

headers = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.azubiheft.de/Azubi/Default.aspx",
}

url = "https://www.azubiheft.de/Azubi/Ausbildungsnachweise.aspx"
resp = session.get(url, headers=headers, timeout=15)

print(f"Status: {resp.status_code}")

with open("debug_overview.html", "w") as f:
    f.write(resp.text)

# Search for download links and report links
import re
downloads = re.findall(r'DownloadBR\.ashx\?Code=([^"\']+)', resp.text)
print(f"Download links: {len(downloads)}")

# Also look for links to individual reports
reports = re.findall(r'href="([^"]*Bericht[^"]*)"', resp.text, re.IGNORECASE)
print(f"Report links: {len(reports)}")
for r in reports[:5]:
    print(f"  {r}")

print("Saved to debug_overview.html")
