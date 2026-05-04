#!/usr/bin/env python3
"""Module 2.4: Send PDF text to Gemini, get activity predictions."""

import os
import json
import logging
import sqlite3
from pathlib import Path
from datetime import datetime
import google.generativeai as genai
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing import Optional

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

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
        history_text = "\nDeine früheren, korrigierten Vorhersagen (diese sind korrekt):\n"
        for w, dept, day, task, hours in history:
            history_text += f"  Woche {w}, {dept}, {day}: {task} ({hours}h)\n"
    
    prompt = f"""Du bist ein Auszubildender im 3. Lehrjahr. Schreibe Tätigkeiten für den Ausbildungsnachweis.

ABTEILUNG: {department}
WOCHE: {week}

REGELN:
- Montag bis Donnerstag: genau 8 Stunden pro Tag
- Freitag: genau 5.5 Stunden
- Keine Einträge für: Feiertag, Urlaub, Arbeitsunfähig
- "Berichtsheft geschrieben" darf maximal 2 Stunden pro Woche haben
- Tätigkeiten müssen zur Abteilung passen
- Schreibe im gleichen Stil wie die früheren Einträge
- Technische Tätigkeiten bevorzugen

{history_text}

Aktueller Berichtstext (was diese Woche tatsächlich gemacht wurde):
{pdf_text[:3000]}

Erstelle eine JSON-Antwort mit diesem exakten Format:
{{
  "department": "{department}",
  "week": {week},
  "days": [
    {{
      "day": "Montag",
      "activities": [
        {{"task": "Tätigkeit", "hours": 3.0}},
        {{"task": "Tätigkeit", "hours": 2.0}},
        {{"task": "Tätigkeit", "hours": 3.0}}
      ]
    }},
    ...
  ]
}}

Antworte NUR mit dem JSON, keine Erklärung."""
    
    return prompt


def predict(pdf_text):
    """Main function: parse PDF, get history, call Gemini, return structured prediction."""
    
    department, week = parse_pdf_text(pdf_text)
    if not department or not week:
        logger.error("Could not parse department or week from PDF")
        return None
    
    logger.info(f"Predicting for week {week}, department: {department}")
    
    history = get_correction_history()
    logger.info(f"Loaded {len(history)} historical entries")
    
    prompt = build_prompt(pdf_text, department, week, history)
    
    model = genai.GenerativeModel("gemini-2.0-flash")
    
    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        
        # Clean markdown if present
        if text.startswith("```json"):
            text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        
        prediction = WeekPrediction.model_validate_json(text.strip())
        
        # Store in database
        store_predictions(prediction)
        
        return prediction
    
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return None


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


if __name__ == "__main__":
    from parse_pdf import extract_latest
    
    number, text = extract_latest()
    if text:
        result = predict(text)
        if result:
            print(result.model_dump_json(indent=2))
