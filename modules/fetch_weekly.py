#!/usr/bin/env python3
"""Fetch a weekly report page to find the download button."""

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
    "Referer": "https://www.azubiheft.de/Azubi/Ausbildungsnachweise.aspx",
}

# Fetch the latest report
url = "https://www.azubiheft.de/Azubi/Wochenansicht.aspx?NachweisNr=141"
resp = session.get(url, headers=headers, timeout=15)

print(f"Status: {resp.status_code}")

with open("debug_weekly.html", "w") as f:
    f.write(resp.text)

# Search for download link
import re
downloads = re.findall(r'DownloadBR\.ashx\?Code=([^"\']+)', resp.text)
print(f"Download links: {len(downloads)}")
for d in downloads:
    print(f"  Code: {d}")

# Also check for any ashx link
ashx_links = re.findall(r'["\']([^"\']*\.ashx[^"\']*)["\']', resp.text)
print(f"ASHX links: {len(ashx_links)}")
for a in ashx_links:
    print(f"  {a}")
