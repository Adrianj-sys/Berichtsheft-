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
    """Get set of week numbers that are fully processed (all 5 days have approved entries)."""
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
            logger.info(f"Skipping {pdf.name}: empty week")
            continue
        if bericht["week"] not in processed_weeks:
            unprocessed.append((pdf, bericht))
    
    return unprocessed


def store_existing_activities(bericht):
    """Auto-approve activities that already exist in the PDF. Stores only if not already present."""
    conn = sqlite3.connect(str(DB_PATH))
    stored = 0
    
    for day_name, day_info in bericht["days"].items():
        if day_info["status"] in ["present", "special"]:
            for activity in day_info["activities"]:
                # Check if this exact entry already exists
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
    
    empty_days = [d for d, info in bericht["days"].items() if info["status"] == "empty"]
    present_days = [d for d, info in bericht["days"].items() if info["status"] in ("present", "special")]
    
    logger.info(f"Report {report_nr}, Week {week}: {len(present_days)} present, {len(empty_days)} empty")
    
    # Auto-approve existing activities
    if present_days:
        store_existing_activities(bericht)
    
    # If no empty days, we're done
    if not empty_days:
        logger.info(f"Week {week} is fully complete. No AI needed.")
        return
    
    logger.info(f"Empty days to predict: {empty_days}")
    
    # Get PDF text
    doc = fitz.open(str(pdf_path))
    pdf_text = ""
    for page in doc:
        pdf_text += page.get_text()
    doc.close()
    
    # Add context about already-completed days
    completed_info = ""
    if present_days:
        completed_info = "\n\nBEREITS AUSGEFUELLTE TAGE (diese NICHT neu generieren):\n"
        for d in present_days:
            activities = bericht["days"][d]["activities"]
            for a in activities:
                completed_info += f"  {d}: {a['task']} ({a['hours']}h)\n"
        completed_info += "\nGeneriere NUR fuer diese leeren Tage: " + ", ".join(empty_days)
    
    pdf_text = completed_info + "\n\n" + pdf_text
    
    # Run prediction
    result = predict(pdf_text)
    if result:
        logger.info(f"Predictions ready for week {week}")
        run_confirmation()
    else:
        logger.error(f"Prediction failed for week {week}")
    
    # Clean up: remove any AI-generated entries for days that already had PDF content
    if present_days:
        conn = sqlite3.connect(str(DB_PATH))
        for day_name in present_days:
            original_tasks = [a["task"] for a in bericht["days"][day_name]["activities"]]
            # Delete entries for this day that aren't the originals
            for task in original_tasks:
                conn.execute("""
                    DELETE FROM predictions 
                    WHERE week=? AND day=? AND task=? AND status='approved'
                    AND id NOT IN (
                        SELECT MIN(id) FROM predictions 
                        WHERE week=? AND day=? AND task=? AND status='approved'
                    )
                """, (week, day_name, task, week, day_name, task))
        conn.commit()
        conn.close()


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
