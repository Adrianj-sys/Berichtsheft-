#!/usr/bin/env python3
"""Module 3.1: Orchestrator — runs the full pipeline with background pre-processing."""

import logging
import sqlite3
import time
import threading
from pathlib import Path
import fitz
from parse_pdf import parse_pdf, DOWNLOADS_DIR
from predict_activities import predict, init_db
from telegram_confirm import (
    run_confirmation, send_message, notify_empty, notify_backlog_start,
    last_interaction_time, pre_process_triggered, current_report_nr
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"

# Module-level globals for pre-processing
queued_reports = {}
pre_process_lock = threading.Lock()


def get_processed_reports():
    """Get set of report numbers that are fully processed."""
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("""
        SELECT report_nr FROM predictions 
        WHERE status='approved' 
        GROUP BY report_nr 
        HAVING COUNT(DISTINCT day) = 5
    """).fetchall()
    conn.close()
    return {r[0] for r in rows}


def get_unprocessed_pdfs():
    """Find PDFs not yet processed, sorted by total hours descending."""
    processed = get_processed_reports()
    pdfs = sorted(DOWNLOADS_DIR.glob("report_*.pdf"))
    unprocessed = []
    
    for pdf in pdfs:
        bericht = parse_pdf(pdf)
        if bericht is None or bericht.get("empty"):
            continue
        if bericht["report_nr"] not in processed:
            unprocessed.append((pdf, bericht))
    
    def get_total(entry):
        return sum(d["total_hours"] for d in entry[1]["days"].values())
    
    unprocessed.sort(key=get_total, reverse=True)
    return unprocessed


def store_existing_activities(bericht):
    """Auto-approve activities that already exist in the PDF."""
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    stored = 0
    
    for day_name, day_info in bericht["days"].items():
        if day_info["status"] in ["present", "special", "partial"]:
            for activity in day_info["activities"]:
                existing = conn.execute(
                    "SELECT id FROM predictions WHERE report_nr=? AND day=? AND task=? AND hours=? AND status='approved'",
                    (bericht["report_nr"], day_name, activity["task"], activity["hours"])
                ).fetchone()
                
                if not existing:
                    conn.execute(
                        "INSERT INTO predictions (report_nr, week, department, day, task, original_task, hours, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'approved')",
                        (bericht["report_nr"], bericht.get("week", 0), bericht["department"], day_name, activity["task"], activity["task"], activity["hours"])
                    )
                    stored += 1
    
    conn.commit()
    conn.close()
    if stored > 0:
        logger.info(f"Auto-approved {stored} existing activities for report {bericht['report_nr']}")


def pre_process_reports(unprocessed_list, start_from_index):
    """Pre-process upcoming reports in background. Stores with status='queued'."""
    global queued_reports
    
    for i in range(start_from_index, len(unprocessed_list)):
        pdf_path, bericht = unprocessed_list[i]
        report_nr = bericht["report_nr"]
        
        with pre_process_lock:
            if report_nr in queued_reports:
                continue
        
        logger.info(f"Background: Pre-processing report {report_nr}...")
        
        empty_days = [d for d, info in bericht["days"].items() if info["status"] in ("empty", "partial")]
        complete_days = [d for d, info in bericht["days"].items() if info["status"] in ("present", "special")]
        
        store_existing_activities(bericht)
        
        if not empty_days:
            logger.info(f"Report {report_nr} is fully complete. No AI needed.")
            with pre_process_lock:
                queued_reports[report_nr] = {"status": "complete"}
            continue
        
        partial_info = ""
        partial_days_list = [d for d in empty_days if bericht["days"][d]["status"] == "partial"]
        if partial_days_list:
            for d in partial_days_list:
                existing = bericht["days"][d]
                remaining = existing["target_hours"] - existing["total_hours"]
                partial_info += f"\n{d}: Generiere NUR {remaining}h zusaetzlich. Bestehende Eintraege BEHALTEN:\n"
                for a in existing["activities"]:
                    partial_info += f"  BEHALTEN: {a['task']} ({a['hours']}h)\n"
        
        doc = fitz.open(str(pdf_path))
        pdf_text = ""
        for page in doc:
            pdf_text += page.get_text()
        doc.close()
        
        result = predict(pdf_text, skip_days=complete_days,
                         ausbildungsjahr=bericht.get("ausbildungsjahr"),
                         betrieb=bericht.get("betrieb"),
                         partial_days_info=partial_info if partial_info else None)
        
        with pre_process_lock:
            if result:
                queued_reports[report_nr] = {"status": "ready"}
                logger.info(f"Background: Report {report_nr} pre-processed and queued")
            else:
                queued_reports[report_nr] = {"status": "failed"}
    
    logger.info("Background pre-processing complete")
    notify_backlog_start(unprocessed_list[start_from_index][1]["report_nr"])


def process_report(pdf_path, bericht):
    """Run the full pipeline on one report."""
    global current_report_nr, last_interaction_time
    
    report_nr = bericht["report_nr"]
    current_report_nr = report_nr
    
    # Check if already queued from pre-processing
    with pre_process_lock:
        if report_nr in queued_reports:
            q_status = queued_reports[report_nr]["status"]
            del queued_reports[report_nr]
            
            if q_status == "complete":
                logger.info(f"Report {report_nr} already pre-processed (complete).")
                return
            elif q_status == "ready":
                logger.info(f"Report {report_nr} already pre-processed. Sending to Telegram.")
                run_confirmation(report_nr)
                return
    
    empty_days = [d for d, info in bericht["days"].items() if info["status"] in ("empty", "partial")]
    complete_days = [d for d, info in bericht["days"].items() if info["status"] in ("present", "special")]
    
    logger.info(f"Report {report_nr}: {len(complete_days)} complete, {len(empty_days)} need AI")
    
    store_existing_activities(bericht)
    
    if not empty_days:
        logger.info(f"Report {report_nr} is fully complete.")
        return
    
    partial_info = ""
    partial_days_list = [d for d in empty_days if bericht["days"][d]["status"] == "partial"]
    if partial_days_list:
        for d in partial_days_list:
            existing = bericht["days"][d]
            remaining = existing["target_hours"] - existing["total_hours"]
            partial_info += f"\n{d}: Generiere NUR {remaining}h zusaetzlich. Bestehende Eintraege BEHALTEN:\n"
            for a in existing["activities"]:
                partial_info += f"  BEHALTEN: {a['task']} ({a['hours']}h)\n"
    
    doc = fitz.open(str(pdf_path))
    pdf_text = ""
    for page in doc:
        pdf_text += page.get_text()
    doc.close()
    
    result = predict(pdf_text, skip_days=complete_days,
                     ausbildungsjahr=bericht.get("ausbildungsjahr"),
                     betrieb=bericht.get("betrieb"),
                     partial_days_info=partial_info if partial_info else None)
    if result:
        logger.info(f"Predictions ready for report {report_nr}")
        run_confirmation(report_nr)
    else:
        logger.error(f"Prediction failed for report {report_nr}")


def check_idle_and_preprocess(unprocessed_list, current_index):
    """Check if user has been idle for 10 minutes and start pre-processing."""
    global last_interaction_time, pre_process_triggered
    
    while True:
        time.sleep(30)
        if pre_process_triggered:
            continue
        
        idle_time = time.time() - last_interaction_time
        
        if idle_time > 600 and not pre_process_triggered:  # 10 minutes
            pre_process_triggered = True
            next_index = current_index + 1
            if next_index < len(unprocessed_list):
                logger.info(f"User idle for {idle_time:.0f}s. Starting background pre-processing from index {next_index}")
                thread = threading.Thread(target=pre_process_reports, args=(unprocessed_list, next_index))
                thread.daemon = True
                thread.start()


def run():
    logger.info("Orchestrator starting...")
    
    # Handle empty weeks
    pdfs = sorted(DOWNLOADS_DIR.glob("report_*.pdf"))
    for pdf in pdfs:
        bericht = parse_pdf(pdf)
        if bericht and bericht.get("empty"):
            report_nr_from_file = int(pdf.stem.split("_")[1])
            notify_empty(report_nr_from_file)
    
    unprocessed = get_unprocessed_pdfs()
    
    if not unprocessed:
        logger.info("No unprocessed PDFs found")
        return
    
    logger.info(f"Found {len(unprocessed)} unprocessed PDFs. Processing order:")
    for i, (pdf, bericht) in enumerate(unprocessed, 1):
        total = sum(d["total_hours"] for d in bericht["days"].values())
        logger.info(f"  {i}. Report {bericht['report_nr']}: {total}h")
    
    # Start idle monitor thread
    monitor_thread = threading.Thread(target=check_idle_and_preprocess, args=(unprocessed, 0))
    monitor_thread.daemon = True
    monitor_thread.start()
    
    for idx, (pdf_path, bericht) in enumerate(unprocessed):
        logger.info(f"Processing {pdf_path.name}...")
        try:
            process_report(pdf_path, bericht)
            # Update monitor with current index
            global last_interaction_time
            last_interaction_time = time.time()
        except Exception as e:
            logger.error(f"Failed to process {pdf_path.name}: {e}")


if __name__ == "__main__":
    run()
