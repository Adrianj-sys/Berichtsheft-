#!/usr/bin/env python3
"""Module 2.4: Send PDF text to Gemini, get activity predictions."""

import time
import os
import json
import logging
import sqlite3
from pathlib import Path
from google import genai
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"


class Activity(BaseModel):
    task: str
    hours: float

class Day(BaseModel):
    day: str
    activities: list[Activity]

class WeekPrediction(BaseModel):
    department: str
    week: int
    days: list[Day]


def get_correction_history():
    """Load all past approved predictions from database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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
    rows = conn.execute(
        "SELECT week, department, day, task, hours FROM predictions WHERE status='approved' ORDER BY week, day"
    ).fetchall()
    conn.close()
    return rows


def parse_pdf_text(text):
    """Extract department and week from PDF text."""
    department = None
    week = None
    for line in text.split("\n"):
        if "Abteilung:" in line:
            department = line.split("Abteilung:")[-1].strip()
        if "KW:" in line:
            try:
                week = int(line.split("KW:")[-1].strip().split()[0])
            except:
                pass
    return department, week


def build_prompt(pdf_text, department, week, history):
    """Build the Gemini prompt with rules and history."""
    history_text = ""
    if history:
        history_text = "\nDeine frueheren, korrigierten Vorhersagen (diese sind korrekt):\n"
        for w, dept, day, task, hours in history:
            history_text += f"  Woche {w}, {dept}, {day}: {task} ({hours}h)\n"
    
    prompt = f"""Du bist ein Auszubildender im 3. Lehrjahr. Schreibe Taetigkeiten fuer den Ausbildungsnachweis.

ABTEILUNG: {department}
WOCHE: {week}

REGELN:
- Montag bis Donnerstag: genau 8 Stunden pro Tag
- Freitag: genau 5.5 Stunden
- Keine Eintraege fuer: Feiertag, Urlaub, Arbeitsunfaehig
- "Berichtsheft geschrieben" darf maximal 2 Stunden pro Woche haben
- Taetigkeiten muessen zur Abteilung passen
- Schreibe im gleichen Stil wie die frueheren Eintraege
- Technische Taetigkeiten bevorzugen

{history_text}

Aktueller Berichtstext (was diese Woche tatsaechlich gemacht wurde):
{pdf_text[:3000]}

Erstelle eine JSON-Antwort mit diesem exakten Format:
{{
  "department": "{department}",
  "week": {week},
  "days": [
    {{
      "day": "Montag",
      "activities": [
        {{"task": "Taetigkeit", "hours": 3.0}},
        {{"task": "Taetigkeit", "hours": 2.0}},
        {{"task": "Taetigkeit", "hours": 3.0}}
      ]
    }}
  ]
}}

Antworte NUR mit dem JSON, keine Erklaerung."""
    return prompt


def store_predictions(prediction):
    """Save predictions to database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    for day in prediction.days:
        for activity in day.activities:
            conn.execute(
                "INSERT INTO predictions (week, department, day, task, hours, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                (prediction.week, prediction.department, day.day, activity.task, activity.hours)
            )
    conn.commit()
    conn.close()
    logger.info(f"Stored {sum(len(d.activities) for d in prediction.days)} predictions")


def predict(pdf_text):
    """Main function: parse PDF, get history, call Gemini with retry, return structured prediction."""
    department, week = parse_pdf_text(pdf_text)
    if not department or not week:
        logger.error("Could not parse department or week from PDF")
        return None
    
    logger.info(f"Predicting for week {week}, department: {department}")
    history = get_correction_history()
    logger.info(f"Loaded {len(history)} historical entries")
    
    prompt = build_prompt(pdf_text, department, week, history)
    
    # Retry logic with exponential backoff
    max_retries = 5
    retry_delay = 10  # start with 10 seconds
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            text = response.text.strip()
            
            # Clean markdown if present
            if text.startswith("```json"):
                text = text[7:]
            if text.endswith("```"):
                text = text[:-3]
            
            prediction = WeekPrediction.model_validate_json(text.strip())
            store_predictions(prediction)
            logger.info("Successfully stored predictions")
            return prediction
        
        except Exception as e:
            error_str = str(e)
            if "429" in error_str:
                logger.warning(f"Rate limited (attempt {attempt+1}/{max_retries}). Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                retry_delay *= 2  # double the wait time for the next retry
            else:
                logger.error(f"Gemini API error: {e}")
                return None
    
    logger.error(f"Max retries reached")
    return None


if __name__ == "__main__":
    sample = """
    Abteilung: Ausbildungszentrum
    KW: 14
    """
    result = predict(sample)
    if result:
        print(result.model_dump_json(indent=2))
