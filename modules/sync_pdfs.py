#!/usr/bin/env python3
"""Sync PDFs from Desktop via SCP."""

import subprocess
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

DESKTOP_IP = "192.168.178.38"
KEY_FILE = Path.home() / ".ssh" / "berichtsheft_key"
REMOTE_DIR = f"adria@{DESKTOP_IP}:/Users/adria/Documents/Berichtsheft/shared/"
DOWNLOAD_DIR = Path(__file__).parent.parent / "downloads"


def sync():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    
    # First, list remote files
    list_result = subprocess.run(
        ["ssh", "-i", str(KEY_FILE), f"adria@{DESKTOP_IP}", "dir", "C:\\Users\\adria\\Documents\\Berichtsheft\\shared\\*.pdf", "/b"],
        capture_output=True, text=True, shell=False
    )
    
    if list_result.returncode != 0:
        logger.info("No PDFs found on desktop")
        return
    
    remote_files = [f.strip() for f in list_result.stdout.split("\n") if f.strip()]
    
    if not remote_files:
        logger.info("No PDFs to sync")
        return
    
    for filename in remote_files:
        local_path = DOWNLOAD_DIR / filename
        if local_path.exists():
            continue
        
        remote_path = f"adria@{DESKTOP_IP}:/Users/adria/Documents/Berichtsheft/shared/{filename}"
        result = subprocess.run(
            ["scp", "-i", str(KEY_FILE), remote_path, str(local_path)],
            capture_output=True, text=True
        )
        
        if result.returncode == 0:
            logger.info(f"Downloaded: {filename}")
        else:
            logger.error(f"Failed: {filename} - {result.stderr}")


if __name__ == "__main__":
    sync()
