"""Run once: Log in manually and save browser state."""
from playwright.sync_api import sync_playwright
from pathlib import Path
import time

AUTH_FILE = Path(__file__).parent.parent / "auth" / "session.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    
    page.goto("https://www.azubiheft.de/Login.aspx")
    
    print("Log in manually in the browser window.")
    print("After login, come back here and press Enter...")
    input()
    
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(AUTH_FILE))
    print(f"Session saved to {AUTH_FILE}")
    browser.close()