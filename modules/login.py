#!/usr/bin/env python3
"""Module 2.1: Website login via requests (ASP.NET WebForms)."""

import os
import logging
import pickle
from pathlib import Path
from bs4 import BeautifulSoup
import requests
from dotenv import load_dotenv

load_dotenv()

LOGIN_URL = "https://www.azubiheft.de/Login.aspx"
DASHBOARD_URL = "https://www.azubiheft.de/Azubi/Default.aspx"
AUTH_DIR = Path(__file__).parent.parent / "auth"
SESSION_FILE = AUTH_DIR / "session.pkl"
USERNAME = os.getenv("WEBSITE_USERNAME")
PASSWORD = os.getenv("WEBSITE_PASSWORD")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def login():
    """Log in and save session cookies."""

    if not USERNAME or not PASSWORD:
        logger.error("Missing credentials.")
        return False

    AUTH_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    
    try:
        # Step 1: Get login page and parse hidden fields
        logger.info("Fetching login page...")
        resp = session.get(LOGIN_URL, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Extract ASP.NET hidden fields
        viewstate = soup.find("input", {"name": "__VIEWSTATE"})["value"]
        viewstategenerator = soup.find("input", {"name": "__VIEWSTATEGENERATOR"})["value"]
        eventvalidation = soup.find("input", {"name": "__EVENTVALIDATION"})["value"]
        
        logger.info("Got hidden form fields")
        
        # Step 2: POST login
        payload = {
            "__VIEWSTATE": viewstate,
            "__VIEWSTATEGENERATOR": viewstategenerator,
            "__EVENTVALIDATION": eventvalidation,
            "ctl00$ContentPlaceHolder1$txt_Benutzername": USERNAME,
            "ctl00$ContentPlaceHolder1$txt_Passwort": PASSWORD,
            "ctl00$ContentPlaceHolder1$cmd_Login": "Anmelden",
        }
        
        logger.info("Sending login request...")
        resp = session.post(LOGIN_URL, data=payload, timeout=15)
        
        # Step 3: Check if login succeeded
        if "Default.aspx" in resp.url or "Azubi" in resp.url:
            logger.info("Login successful!")
            
            # Save session cookies
            with open(SESSION_FILE, "wb") as f:
                pickle.dump(session.cookies, f)
            logger.info(f"Session saved to {SESSION_FILE}")
            return True
        else:
            logger.error(f"Login failed. Redirected to: {resp.url}")
            with open("debug_login_failed.html", "w") as f:
                f.write(resp.text)
            return False

    except Exception as e:
        logger.error(f"Error: {e}")
        return False


if __name__ == "__main__":
    success = login()
    if success:
        print("Login successful, session saved.")
    else:
        print("Login failed. Check logs.")
