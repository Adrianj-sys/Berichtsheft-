#!/usr/bin/env python3
"""Module 3.1: Orchestrator — runs the full pipeline for unprocessed PDFs."""

import logging
import sqlite3
from pathlib import Path
import fitz
from parse_pdf import parse_pdf, DOWNLOADS_DIR
from predict_activities import predict
from telegram_confirm import run_confirmation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"


def get_processed_weeks():
    """Get set of week numbers that are fully processed."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week INTEGER,
            department TEXT,
            day TEXT,
            task TEXT,
            hours REAL,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    rows = conn.execute("""
        SELECT week FROM predictions 
        WHERE status='approved' 
        GROUP BY week 
        HAVING COUNT(DISTINCT day) = 5
    """).fetchall()
    conn.close()
    return {r[0] for r in rows}


def get_unprocessed_pdfs():
    """Find PDFs that haven't been fully processed yet."""
    processed_weeks = get_processed_weeks()
    pdfs = sorted(DOWNLOADS_DIR.glob("report_*.pdf"))
    unprocessed = []
    
    for pdf in pdfs:
        bericht = parse_pdf(pdf)
        if bericht is None:
            continue
        if bericht["week"] not in processed_weeks:
            unprocessed.append((pdf, bericht))
    
    return unprocessed


def store_existing_activities(bericht):
    """Auto-approve activities that already exist in the PDF."""
    conn = sqlite3.connect(str(DB_PATH))
    stored = 0
    
    for day_name, day_info in bericht["days"].items():
        if day_info["status"] in ["present", "special", "partial"]:
            for activity in day_info["activities"]:
                existing = conn.execute(
                    "SELECT id FROM predictions WHERE week=? AND day=? AND task=? AND hours=? AND status='approved'",
                    (bericht["week"], day_name, activity["task"], activity["hours"])
                ).fetchone()
                
                if not existing:
                    conn.execute(
                        "INSERT INTO predictions (week, department, day, task, hours, status) VALUES (?, ?, ?, ?, ?, 'approved')",
                        (bericht["week"], bericht["department"], day_name, activity["task"], activity["hours"])
                    )
                    stored += 1
    
    conn.commit()
    conn.close()
    if stored > 0:
        logger.info(f"Auto-approved {stored} existing activities for week {bericht['week']}")


def process_report(pdf_path, bericht):
    """Run the full pipeline on one report."""
    week = bericht["week"]
    report_nr = bericht["report_nr"]
    
    empty_days = [d for d, info in bericht["days"].items() if info["status"] in ("empty", "partial")]
    complete_days = [d for d, info in bericht["days"].items() if info["status"] in ("present", "special")]
    
    logger.info(f"Report {report_nr}, Week {week}: {len(complete_days)} complete, {len(empty_days)} need AI")
    
    # Auto-approve existing activities
    store_existing_activities(bericht)
    
    # If no days need AI, we're done
    if not empty_days:
        logger.info(f"Week {week} is fully complete. No AI needed.")
        return
    
    logger.info(f"Days needing AI: {empty_days}")
    
    # Get PDF text
    doc = fitz.open(str(pdf_path))
    pdf_text = ""
    for page in doc:
        pdf_text += page.get_text()
    doc.close()
    
    # Run prediction, skipping complete days
    result = predict(pdf_text, skip_days=complete_days)
    if result:
        logger.info(f"Predictions ready for week {week}")
        run_confirmation()
    else:
        logger.error(f"Prediction failed for week {week}")


def run():
    logger.info("Orchestrator starting...")
    unprocessed = get_unprocessed_pdfs()
    
    if not unprocessed:
        logger.info("No unprocessed PDFs found")
        return
    
    logger.info(f"Found {len(unprocessed)} unprocessed PDFs")
    
    for pdf_path, bericht in unprocessed:
        logger.info(f"Processing {pdf_path.name}...")
        try:
            process_report(pdf_path, bericht)
        except Exception as e:
            logger.error(f"Failed to process {pdf_path.name}: {e}")


if __name__ == "__main__":
    run()
