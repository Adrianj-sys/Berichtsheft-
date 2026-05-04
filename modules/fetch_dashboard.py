#!/usr/bin/env python3
"""Fetch dashboard HTML using saved session cookies."""

import pickle
import requests
from pathlib import Path

AUTH_DIR = Path(__file__).parent.parent / "auth"
SESSION_FILE = AUTH_DIR / "session.pkl"
DASHBOARD_URL = "https://www.azubiheft.de/Azubi/Default.aspx"

# Load saved session cookies
with open(SESSION_FILE, "rb") as f:
    cookies = pickle.load(f)

session = requests.Session()
session.cookies.update(cookies)

# Mimic a real browser
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Referer": "https://www.azubiheft.de/Login.aspx",
}

resp = session.get(DASHBOARD_URL, timeout=15, headers=headers, allow_redirects=True)
print(f"Status: {resp.status_code}")
print(f"Final URL: {resp.url}")

# Check if we're still on login page
if "Login" in resp.text and "txt_Benutzername" in resp.text:
    print("FAILED: Redirected back to login page")
else:
    print("SUCCESS: Dashboard loaded")

with open("debug_dashboard.html", "w") as f:
    f.write(resp.text)
print("Saved to debug_dashboard.html")
