#!/usr/bin/env python3
"""Pull new PDFs from Desktop HTTP server to Pi."""

import logging
import requests
from pathlib import Path
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

DESKTOP_URL = "http://192.168.178.38:8080"
DOWNLOAD_DIR = Path(__file__).parent.parent / "downloads"


def list_remote():
    """Get list of PDFs on desktop."""
    resp = requests.get(DESKTOP_URL, timeout=10)
    soup = BeautifulSoup(resp.text, "html.parser")
    return [a["href"] for a in soup.find_all("a") if a["href"].endswith(".pdf")]


def sync():
    """Download any PDFs not already on Pi."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    remote_files = list_remote()
    
    downloaded = 0
    for filename in remote_files:
        filepath = DOWNLOAD_DIR / filename
        if filepath.exists():
            continue
        
        logger.info(f"Downloading {filename}...")
        resp = requests.get(f"{DESKTOP_URL}/{filename}", timeout=60)
        filepath.write_bytes(resp.content)
        downloaded += 1
        logger.info(f"  Saved: {filename}")
    
    logger.info(f"Synced: {downloaded} new files")


if __name__ == "__main__":
    sync()
