#!/usr/bin/env python3
"""
V3 Orchestrator: Download -> German correction + flesh-out (real content only) -> flag mismatches -> upload.

Explicitly does NOT:
  - delete/trim entries that go over the daily hour target (no fix_over_limit)
  - invent new activities to fill hour gaps (no ai_fill_gaps)
  - change any hours value

What it DOES do:
  - downloads your real entries from the portal into the local DB
  - corrects German spelling/grammar and expands wording, using ONLY what you
    already wrote as source material (no new facts, tools, or tasks invented)
  - flags entries whose corrected text looks too short for the hours logged,
    using a simple, adjustable word-count heuristic (not an AI judgment call)
  - sends a Telegram notification listing any flagged entries (informational only)
  - uploads everything to the Desktop watcher via the existing SCP trigger,
    regardless of flags, per your instruction to review on the portal side
"""

import logging
import sqlite3
import json
import subprocess
import tempfile
from pathlib import Path

from html_parser import parse_weekly_overview
from predict_activities import ask_ai, init_db
from telegram_confirm import send_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"

DAY_ORDER = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]

# Flagging threshold: minimum words-per-hour before an entry gets flagged for
# manual review. This is a plain word-count heuristic, not an AI decision --
# tune it if it's too sensitive or not sensitive enough.
MIN_WORDS_PER_HOUR = 6
MIN_ABSOLUTE_WORDS = 3


# ---------------------------------------------------------------------------
# 1. Download (unchanged from v2 -- just refreshes the local cache table)
# ---------------------------------------------------------------------------

def download_all_to_db(report_nr):
    """Clear local DB cache for report, then download fresh from website."""
    logger.info(f"Downloading report {report_nr}...")

    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM predictions WHERE report_nr=?", (report_nr,))
    conn.commit()
    conn.close()
    logger.info(f"Cleared local cache for report {report_nr}")

    bericht = parse_weekly_overview(report_nr)
    if not bericht:
        return None

    conn = sqlite3.connect(str(DB_PATH))
    for day_name in DAY_ORDER:
        day_info = bericht["days"].get(day_name, {})
        for a in day_info.get("activities", []):
            conn.execute(
                "INSERT INTO predictions (report_nr, department, day, task, original_task, hours, status, source) "
                "VALUES (?, ?, ?, ?, ?, ?, 'approved', 'website')",
                (report_nr, bericht.get("department", ""), day_name, a["task"], a["task"], a["hours"])
            )
    conn.commit()
    conn.close()

    logger.info(f"Saved website entries for report {report_nr}")
    return bericht


# ---------------------------------------------------------------------------
# 2. Correction + flesh-out -- edits wording only, never hours, never adds
#    entries. The AI is only allowed to elaborate on what's already there.
# ---------------------------------------------------------------------------

def build_correction_prompt(entries):
    """entries: list of (id, task, hours)."""
    lines = []
    for eid, task, hours in entries:
        lines.append(f'  {{"id": {eid}, "text": "{task}", "hours": {hours}}}')
    entries_block = "\n".join(lines)

    return f"""Du bekommst eine Liste von echten Ausbildungsnachweis-Eintraegen (Taetigkeiten, die ein Azubi tatsaechlich durchgefuehrt hat).

AUFGABE fuer JEDEN Eintrag:
1. Korrigiere Rechtschreibung und Grammatik.
2. Formuliere den Text zu einem vollstaendigen, klaren Satz aus -- aber NUR indem du beschreibst, was bereits im Originaltext steht. Du darfst Details ausformulieren (z.B. "Kabel verlegt" -> "Neue Netzwerkkabel im Serverraum verlegt und angeschlossen"), aber NUR wenn diese Details im Kontext plausibel und bereits angedeutet sind.

STRENGE REGELN:
- Erfinde KEINE neuen Aufgaben, Werkzeuge, Orte oder Ergebnisse, die nicht im Originaltext stehen oder direkt daraus folgen.
- Aendere NIEMALS die Bedeutung oder das Thema der Taetigkeit.
- Aendere NIEMALS die Stunden -- die 'hours' sind nur Kontext, du gibst sie nicht zurueck.
- Wenn ein Text zu kurz oder zu vage ist, um ihn ehrlich auszuformulieren (z.B. nur ein Wort, oder keine erkennbare Taetigkeit), korrigiere nur Rechtschreibung/Grammatik und setze "too_short": true.
- Wenn du dir nicht sicher bist, ob eine Ausformulierung noch durch den Originaltext gedeckt ist, bleibe naeher am Original statt mehr zu erfinden.

EINTRAEGE:
{entries_block}

Antworte NUR mit JSON in diesem Format, ein Objekt pro Eintrag, gleiche Reihenfolge:
{{"corrections": [{{"id": <id>, "corrected": "<text>", "too_short": <true/false>}}]}}"""


def correct_and_flesh_out(report_nr):
    """Run German correction/flesh-out over all approved entries for a report.
    Updates `task` in place. `original_task` is left as the raw scraped text.
    Returns the set of ids that came back marked too_short by the model.
    """
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT id, task, hours FROM predictions WHERE report_nr=? AND status='approved' AND task!='Skipped'",
        (report_nr,)
    ).fetchall()
    conn.close()

    if not rows:
        logger.info(f"No entries to correct for report {report_nr}")
        return set()

    prompt = build_correction_prompt(rows)
    result = ask_ai(prompt)

    too_short_ids = set()

    if not result or "corrections" not in result:
        logger.warning(f"Correction step failed for report {report_nr} -- leaving original text as-is")
        return too_short_ids

    conn = sqlite3.connect(str(DB_PATH))
    for c in result["corrections"]:
        eid = c.get("id")
        corrected = c.get("corrected", "").strip()
        if c.get("too_short"):
            too_short_ids.add(eid)
        if eid is not None and corrected:
            conn.execute("UPDATE predictions SET task=? WHERE id=?", (corrected, eid))
    conn.commit()
    conn.close()

    logger.info(f"Corrected {len(result['corrections'])} entries for report {report_nr}")
    return too_short_ids


# ---------------------------------------------------------------------------
# 3. Flagging -- plain heuristic, no AI judgment. Purely informational.
# ---------------------------------------------------------------------------

def flag_mismatches(report_nr, too_short_ids):
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT id, day, task, hours FROM predictions WHERE report_nr=? AND status='approved' AND task!='Skipped'",
        (report_nr,)
    ).fetchall()
    conn.close()

    flagged = []
    for eid, day, task, hours in rows:
        word_count = len((task or "").split())
        reasons = []

        if eid in too_short_ids:
            reasons.append("zu kurz fuer sinnvolle Ausformulierung")

        if hours > 0 and (word_count / hours) < MIN_WORDS_PER_HOUR:
            reasons.append(f"{word_count} Woerter fuer {hours}h (< {MIN_WORDS_PER_HOUR} Woerter/h)")

        if word_count < MIN_ABSOLUTE_WORDS:
            reasons.append(f"nur {word_count} Woerter insgesamt")

        if hours == 0 and task:
            reasons.append("0h aber Text vorhanden")

        if reasons:
            flagged.append({"id": eid, "day": day, "task": task, "hours": hours, "reasons": reasons})

    return flagged


def notify_flags(report_nr, flagged):
    if not flagged:
        send_message(f"✅ Bericht {report_nr}: keine Auffaelligkeiten, hochgeladen.")
        return

    lines = [f"⚠️ <b>Bericht {report_nr}</b> -- {len(flagged)} Eintrag/Eintraege zur Ueberpruefung (trotzdem hochgeladen):\n"]
    for f in flagged:
        lines.append(f"• <b>{f['day']}</b> ({f['hours']}h): {f['task']}")
        lines.append(f"  ↳ {', '.join(f['reasons'])}")
    send_message("\n".join(lines))
    logger.info(f"Sent Telegram flag notice for report {report_nr}: {len(flagged)} entries")


# ---------------------------------------------------------------------------
# 4. Upload -- unchanged SCP trigger to the Desktop watcher
# ---------------------------------------------------------------------------

def trigger_desktop(report_nr):
    """SCP JSON to Desktop shared folder for watcher."""
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT day, task, hours FROM predictions WHERE report_nr=? AND status='approved' AND task!='Skipped'",
        (report_nr,)
    ).fetchall()
    conn.close()

    if not rows:
        return False

    entries = {}
    for day, task, hours in rows:
        entries.setdefault(day, []).append({"task": task, "hours": hours})

    local_json = str(Path(tempfile.gettempdir()) / f"form_fill_{report_nr}.json")
    remote_json = f"C:/Users/adria/Documents/Berichtsheft/shared/form_fill_{report_nr}.json"

    with open(local_json, "w") as f:
        json.dump(entries, f)

    subprocess.run(
        f'scp -i ~/.ssh/berichtsheft_key {local_json} adria@192.168.178.38:"{remote_json}"',
        shell=True
    )

    logger.info(f"Sent trigger for report {report_nr}")
    return True


# ---------------------------------------------------------------------------
# 5. Orchestration
# ---------------------------------------------------------------------------

def process_report_full(report_nr):
    logger.info(f"=== Report {report_nr} ===")

    bericht = download_all_to_db(report_nr)
    if not bericht:
        logger.warning(f"Report {report_nr}: nothing downloaded, skipping")
        return

    too_short_ids = correct_and_flesh_out(report_nr)
    flagged = flag_mismatches(report_nr, too_short_ids)
    notify_flags(report_nr, flagged)
    trigger_desktop(report_nr)

    logger.info(f"=== Report {report_nr} complete ({len(flagged)} flagged) ===")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        process_report_full(int(sys.argv[1]))
    else:
        for rn in range(150, 161):
            try:
                process_report_full(rn)
            except Exception as e:
                logger.error(f"Failed {rn}: {e}")
