#!/usr/bin/env python3
"""Module 2.2: PDF downloader - clicks icon, extracts DownloadBR code, downloads via requests.
Usage: python download_pdf.py           # latest report
       python download_pdf.py 136       # single report
       python download_pdf.py 1 141     # range (1 to 141)
"""

import os
import re
import sys
import time
import logging
import pickle
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://www.azubiheft.de"
LOGIN_URL = f"{BASE_URL}/Login.aspx"
OVERVIEW_URL = f"{BASE_URL}/Azubi/Ausbildungsnachweise.aspx"
DOWNLOAD_DIR = Path("C:/Users/adria/Documents/Berichtsheft/shared")
COOKIE_FILE = Path(__file__).parent.parent / "auth" / "session.pkl"

USERNAME = os.getenv("WEBSITE_USERNAME")
PASSWORD = os.getenv("WEBSITE_PASSWORD")


def login_if_needed():
    if COOKIE_FILE.exists():
        return True
    
    session = requests.Session()
    resp = session.get(LOGIN_URL, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")
    
    payload = {
        "__VIEWSTATE": soup.find("input", {"name": "__VIEWSTATE"})["value"],
        "__VIEWSTATEGENERATOR": soup.find("input", {"name": "__VIEWSTATEGENERATOR"})["value"],
        "__EVENTVALIDATION": soup.find("input", {"name": "__EVENTVALIDATION"})["value"],
        "ctl00$ContentPlaceHolder1$txt_Benutzername": USERNAME,
        "ctl00$ContentPlaceHolder1$txt_Passwort": PASSWORD,
        "ctl00$ContentPlaceHolder1$cmd_Login": "Anmelden",
    }
    
    resp = session.post(LOGIN_URL, data=payload, timeout=15)
    
    if "Default.aspx" in resp.url:
        logger.info("Login successful")
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(COOKIE_FILE, "wb") as f:
            pickle.dump(session.cookies, f)
        return True
    
    logger.error("Login failed")
    return False


def get_latest_report_number():
    with open(COOKIE_FILE, "rb") as f:
        cookies = pickle.load(f)
    
    session = requests.Session()
    session.cookies.update(cookies)
    resp = session.get(OVERVIEW_URL, timeout=15)
    numbers = re.findall(r"NachweisNr=(\d+)", resp.text)
    
    return max(int(n) for n in numbers) if numbers else None


def get_report_numbers():
    if len(sys.argv) == 1:
        latest = get_latest_report_number()
        return [latest] if latest else []
    elif len(sys.argv) == 2:
        return [int(sys.argv[1])]
    elif len(sys.argv) == 3:
        start, end = int(sys.argv[1]), int(sys.argv[2])
        return list(range(start, end + 1))
    else:
        return []


def download_report(report_number, page):
    url = f"{BASE_URL}/Azubi/Wochenansicht.aspx?NachweisNr={report_number}"
    logger.info(f"  Opening report {report_number}...")
    page.goto(url, wait_until="networkidle", timeout=15000)
    
    filepath = DOWNLOAD_DIR / f"report_{report_number}.pdf"
    if filepath.exists():
        logger.info(f"  Already exists, skipping")
        return True
    
    # Click the PDF icon to trigger CallAjax
    page.wait_for_selector("#spanPDF", state="visible", timeout=10000)
    page.eval_on_selector("#spanPDF", "el => el.click()")
    page.wait_for_timeout(3000)
    
    # Extract the download code from the page
    html = page.content()
    codes = re.findall(r'DownloadBR\.ashx\?Code=([^"\']+)', html)
    
    if codes:
        code = codes[0]
        download_url = f"{BASE_URL}/Azubi/DownloadBR.ashx?Code={code}"
        logger.info(f"  Downloading...")
        
        # Use requests with cookies to download
        session = requests.Session()
        with open(COOKIE_FILE, "rb") as f:
            cookies = pickle.load(f)
        session.cookies.update(cookies)
        
        resp = session.get(download_url, timeout=30)
        filepath.write_bytes(resp.content)
        logger.info(f"  Saved: {filepath.name} ({len(resp.content)} bytes)")
        return True
    
    logger.error(f"  No download code found")
    return False


def main():
    if not login_if_needed():
        return
    
    numbers = get_report_numbers()
    if not numbers:
        logger.error("No reports to download")
        return
    
    logger.info(f"Downloading {len(numbers)} report(s): {numbers[0]} to {numbers[-1]}")
    
    with open(COOKIE_FILE, "rb") as f:
        cookies = pickle.load(f)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        
        page.goto(BASE_URL, wait_until="domcontentloaded")
        for cookie in cookies:
            page.context.add_cookies([{
                "name": cookie.name, "value": cookie.value,
                "domain": ".azubiheft.de", "path": "/"
            }])
        
        success = 0
        for num in numbers:
            if download_report(num, page):
                success += 1
        
        logger.info(f"Done: {success}/{len(numbers)} downloaded")
        browser.close()


if __name__ == "__main__":
    main()
