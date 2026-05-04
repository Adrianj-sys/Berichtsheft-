#!/usr/bin/env python3
"""Module 2.3: Extract text from downloaded PDFs."""

import logging
from pathlib import Path
import fitz  # PyMuPDF

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

DOWNLOADS_DIR = Path(__file__).parent.parent / "downloads"


def extract_text(report_number):
    """Extract full text from a report PDF. Returns string or None."""
    filepath = DOWNLOADS_DIR / f"report_{report_number}.pdf"
    
    if not filepath.exists():
        logger.error(f"File not found: {filepath}")
        return None
    
    try:
        doc = fitz.open(str(filepath))
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        
        logger.info(f"Extracted {len(text)} characters from report {report_number}")
        return text
    
    except Exception as e:
        logger.error(f"Failed to parse {filepath}: {e}")
        return None


def extract_latest():
    """Extract text from the highest-numbered report in downloads."""
    pdfs = sorted(DOWNLOADS_DIR.glob("report_*.pdf"))
    if not pdfs:
        logger.error("No PDFs found in downloads")
        return None, None
    
    latest = pdfs[-1]
    number = int(latest.stem.split("_")[1])
    text = extract_text(number)
    return number, text


if __name__ == "__main__":
    number, text = extract_latest()
    if text:
        print(f"Report {number}: {len(text)} characters")
        print(text[:500])
