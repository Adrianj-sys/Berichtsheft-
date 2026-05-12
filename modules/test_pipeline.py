#!/usr/bin/env python3
"""Quick test of 2.4 + 2.5."""

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))

from predict_activities import predict
from telegram_confirm import run_confirmation

sample_text = """
Ausbildungsnachweis-Nr.: 136
Auszubildende(r): Adrian Collins
Abteilung: Ausbildungszentrum
KW: 14

Montag 8:00
NKG Nebelkammer Schublade Verdrahtung verbessert
NKG Nebelkammer Kühler angeschlossen
NKG Nebelkammer 3D Druckteile gedruckt
Berichtsheft geschrieben

Dienstag 8:00
Bohrmaschine der BNB mit temporärer Zuleitung ausgestattet
Bohrmaschine auf Funktion geprüft
Feste Zuleitung der Bohrmaschine gezogen und angeschlossen
"""

print("=== Step 2.4: Gemini Prediction ===")
result = predict(sample_text)

if result:
    print(f"Predicted activities for week {result.week}")
    print("\n=== Step 2.5: Telegram Confirmation ===")
    run_confirmation()
else:
    print("Prediction failed")
