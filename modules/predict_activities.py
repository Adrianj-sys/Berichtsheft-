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

MODEL = "gemini-3.1-flash-lite-preview"

TARGET_HOURS = {"Montag": 8.0, "Dienstag": 8.0, "Mittwoch": 8.0, "Donnerstag": 8.0, "Freitag": 5.5}


class Activity(BaseModel):
    task: str
    hours: float
    original: str = ""

class Day(BaseModel):
    day: str
    activities: list[Activity]

class WeekPrediction(BaseModel):
    department: str
    report_nr: int
    days: list[Day]


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_nr INTEGER,
            week INTEGER,
            department TEXT,
            day TEXT,
            task TEXT,
            original_task TEXT,
            hours REAL,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def get_correction_history():
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    approved = conn.execute(
        "SELECT report_nr, department, day, task, hours FROM predictions WHERE status='approved' ORDER BY report_nr, day"
    ).fetchall()
    conn.close()
    return approved


def parse_pdf_text(text):
    department = None
    report_nr = None
    for line in text.split("\n"):
        if "Abteilung:" in line:
            department = line.split("Abteilung:")[-1].strip()
        if "Ausbildungsnachweis-Nr.:" in line:
            try:
                report_nr = int(line.split(":")[-1].strip())
            except:
                pass
    return department, report_nr


def is_skipped_department(department):
    if not department:
        return False
    return "berufsschule" in department.lower() or "berufschule" in department.lower()


def build_prompt(pdf_text, department, report_nr, approved, ausbildungsjahr=None, betrieb=None, partial_days_info=None):
    approved_text = ""
    if approved:
        approved_text = "\nDeine frueheren, korrigierten Vorhersagen (diese sind korrekt):\n"
        for rn, dept, day, task, hours in approved:
            approved_text += f"  Bericht {rn}, {dept}, {day}: {task} ({hours}h)\n"
    
    prompt = f"""Du bist ein Auszubildender im {ausbildungsjahr or '3. Lehrjahr'} bei {betrieb or 'einem Industriebetrieb'} in Weissenhorn, Deutschland. Schreibe Taetigkeiten fuer den Ausbildungsnachweis.

ABTEILUNG: {department}
HINWEIS: Die Abteilung bleibt jede Woche gleich. Alle Taetigkeiten muessen zu dieser Abteilung passen.
BERICHT: {report_nr}

REGELN:
- Du MUSST fuer JEDEN Tag (Montag, Dienstag, Mittwoch, Donnerstag, Freitag) Eintraege erstellen
- Kein Tag darf fehlen
- Montag bis Donnerstag: genau 8 Stunden pro Tag
- Freitag: genau 5.5 Stunden
- KEINE Eintraege fuer: Feiertag, Urlaub, Arbeitsunfaehig, Schule, Berufsschule
- Lies NICHT die Art-Angaben (Betrieb/Schule/Feiertag). Ignoriere diese komplett.
- Generiere NIEMALS nicht-technische Eintraege wie Schule, Feiertag oder Urlaub.
- "Berichtsheft geschrieben" darf maximal 2 Stunden pro Woche haben
- Taetigkeiten muessen zur Abteilung {department} passen
- Schreibe im gleichen Stil wie die frueheren Eintraege
- Technische Taetigkeiten bevorzugen
- JEDER Eintrag muss EINZIGARTIG sein. Keine zwei gleichen oder aehnlichen Taetigkeiten.
- Verwende EINFACHE, klare Sprache. Keine komplizierten Fachwoerter. Schreibe wie ein Azubi im 3. Lehrjahr.
- Kurze, direkte Saetze. Zum Beispiel "Kabel verlegt" statt "Durchfuehrung der Kabelverlegung".
- Jeder Eintrag muss mindestens 0.5 Stunden haben. Alle Stunden in 0.5er Schritten (0.5, 1.0, 1.5, 2.0...).
- Zu lange Eintraege duerfen in mehrere kleinere aufgeteilt werden.
- Verlaengere bestehende Eintraege an teilweise gefuellten Tagen bevor du neue Eintraege erstellst.
- Wenn ein Tag mehr Stunden hat als erlaubt, verteile die ueberschuessigen Stunden auf umliegende leere Tage.
- Korrigiere automatisch Rechtschreibfehler in den PDF-Texten.
- Uebersetze englische Eintraege ins Deutsche.
- Formatiere alle Eintraege einheitlich: Grossschreibung am Satzanfang, keine Sonderzeichen.
- Wenn du einen Text korrigierst oder uebersetzt, gib BEIDES an: den Originaltext im Feld 'original' und den korrigierten Text im Feld 'task'.
- Bei neuen Eintraegen lasse das Feld 'original' leer.

{approved_text}"""

    if partial_days_info:
        prompt += f"""

TEILWEISE AUSGEFUELLTE TAGE:
{partial_days_info}

WICHTIG: Generiere NUR die FEHLENDEN Stunden fuer diese Tage. Die bestehenden Eintraege werden BEHALTEN."""

    prompt += f"""

Aktueller Berichtstext:
{pdf_text[:3000]}

Erstelle eine JSON-Antwort mit diesem exakten Format:
{{
  "department": "{department}",
  "report_nr": {report_nr},
  "days": [
    {{
      "day": "Montag",
      "activities": [
        {{"task": "Taetigkeit", "hours": 3.0, "original": ""}},
        {{"task": "Taetigkeit", "hours": 2.0, "original": "Originaltext falls korrigiert"}}
      ]
    }}
  ]
}}

Antworte NUR mit dem JSON, keine Erklaerung."""
    return prompt


def store_predictions(prediction, skip_days=None, existing_entries=None):
    if skip_days is None:
        skip_days = []
    if existing_entries is None:
        existing_entries = {}
    
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    
    conn.execute("DELETE FROM predictions WHERE report_nr=? AND status='pending'", (prediction.report_nr,))
    
    stored = 0
    for day in prediction.days:
        if day.day in skip_days:
            logger.info(f"  Skipping {day.day} (already complete)")
            continue
        for activity in day.activities:
            original = getattr(activity, 'original', '') or ''
            if not original and existing_entries and day.day in existing_entries:
                for old_task, old_hours in existing_entries[day.day]:
                    if activity.hours == old_hours:
                        original = old_task
                        break
            
            conn.execute(
                "INSERT INTO predictions (report_nr, department, day, task, original_task, hours, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                (prediction.report_nr, prediction.department, day.day, activity.task, original, activity.hours)
            )
            stored += 1
    
    conn.commit()
    conn.close()
    logger.info(f"Stored {stored} predictions (skipped {len(skip_days)} days)")


def predict(pdf_text, skip_days=None, ausbildungsjahr=None, betrieb=None, partial_days_info=None, partial_days=None, existing_entries=None):
    if skip_days is None:
        skip_days = []
    if partial_days is None:
        partial_days = []
    if existing_entries is None:
        existing_entries = {}
    
    department, report_nr = parse_pdf_text(pdf_text)
    if not department or not report_nr:
        logger.error("Could not parse department or report_nr from PDF")
        return None
    
    if is_skipped_department(department):
        logger.info(f"Skipping report {report_nr}: department '{department}' is ignored")
        return None
    
    logger.info(f"Predicting for report {report_nr}, department: {department}")
    if skip_days:
        logger.info(f"  Will skip: {skip_days}")
    
    approved = get_correction_history()
    logger.info(f"Loaded {len(approved)} approved entries")
    
    prompt = build_prompt(pdf_text, department, report_nr, approved, ausbildungsjahr, betrieb, partial_days_info)
    
    max_retries = 5
    retry_delay = 10
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt
            )
            text = response.text.strip()
            
            if text.startswith("```json"):
                text = text[7:]
            if text.endswith("```"):
                text = text[:-3]
            
            prediction = WeekPrediction.model_validate_json(text.strip())
            
            # Enforce partial day hour limits
            if partial_days:
                for day in prediction.days:
                    if day.day in partial_days:
                        target = TARGET_HOURS.get(day.day, 8.0)
                        kept = []
                        running_total = 0
                        for activity in day.activities:
                            if running_total + activity.hours <= target:
                                kept.append(activity)
                                running_total += activity.hours
                        day.activities = kept
                        logger.info(f"  Trimmed {day.day} to {running_total}h (target: {target}h)")
            
            store_predictions(prediction, skip_days, existing_entries)
            logger.info("Successfully stored predictions")
            return prediction
        
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "503" in error_str:
                logger.warning(f"API error (attempt {attempt+1}/{max_retries}). Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                logger.error(f"Gemini API error: {e}")
                return None
    
    logger.error("Max retries reached")
    return None


def regenerate_day(report_nr, day, rejected_entries):
    if not rejected_entries:
        return []
    
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    approved = conn.execute(
        "SELECT day, task, hours FROM predictions WHERE status='approved' AND report_nr=? ORDER BY day",
        (report_nr,)
    ).fetchall()
    pending = conn.execute(
        "SELECT task FROM predictions WHERE status='pending' AND report_nr=? AND day=?",
        (report_nr, day)
    ).fetchall()
    conn.close()
    
    approved_text = "\n".join([f"  {d}: {t} ({h}h)" for d, t, h in approved])
    pending_text = "\n".join([f"  (pending) {t}" for (t,) in pending])
    
    rejected_text = ""
    total_hours = 0
    for task, hours in rejected_entries:
        rejected_text += f"  ERSETZEN: {task} ({hours}h)\n"
        total_hours += hours
    
    prompt = f"""Ersetze ALLE abgelehnten Eintraege fuer Bericht {report_nr}, Tag {day}.

GESAMTSTUNDEN ZU ERSETZEN: {total_hours}h

BEREITS GENEHMIGT:
{approved_text}

AKTUELL VORGESCHLAGEN (pending):
{pending_text}

ABGELEHNTE EINTRAEGE:
{rejected_text}

Generiere Ersatz-Eintraege mit GENAU {total_hours}h Gesamtzeit. Jeder Eintrag mindestens 0.5h, in 0.5er Schritten.
Verwende EINFACHE Sprache. Kurze, direkte Saetze.

Antworte NUR mit JSON:
{{"replacements": [{{"task": "Neue Taetigkeit", "hours": 2.0}}]}}"""
    
    max_retries = 5
    retry_delay = 10
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt
            )
            text = response.text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.endswith("```"):
                text = text[:-3]
            
            data = json.loads(text.strip())
            return data.get("replacements", [])
        
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "503" in error_str:
                logger.warning(f"API error (attempt {attempt+1}/{max_retries}). Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                logger.error(f"Day regeneration failed: {e}")
                return []
    
    logger.error("Max retries reached")
    return []


def ask_ai(prompt):
    """Send a raw prompt to Gemini and return parsed JSON."""
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt
        )
        text = response.text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        return json.loads(text.strip())
    except Exception as e:
        logger.error(f"AI ask failed: {e}")
        return None


if __name__ == "__main__":
    sample = """
    Abteilung: Ausbildungszentrum
    Ausbildungsnachweis-Nr.: 139
    """
    result = predict(sample)
    if result:
        print(result.model_dump_json(indent=2))
