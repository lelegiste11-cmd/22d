"""
Configuration du Bot Légiste pour Render.com
"""

import os

# ─── Configuration API Telegram ─────────────────────────────────────────────
API_ID = int(os.environ.get("API_ID", "29177661"))
API_HASH = os.environ.get("API_HASH", "a8639172fa8d35dbfd8ea46286d349ab")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8703080099:AAFUf_rSBF0XxQE-HI78W48d3JGqCgM0DMA")

# ─── Configuration des IDs ───────────────────────────────────────────────────
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6180384006"))
SOURCE_CHANNEL_ID = int(os.environ.get("SOURCE_CHANNEL_ID", "-1003741257466")
PREDICTION_CHANNEL_ID = int(os.environ.get("PREDICTION_CHANNEL_ID", "-1003504929751"))

# ─── Configuration Render.com ────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # https://votre-app.onrender.com

# ─── Paramètres de prédiction ───────────────────────────────────────────────
DEFAULT_OFFSET = 2
FAILURE_OFFSET = 4

# ─── Correspondance des couleurs ───────────────────────────────────────────────
SUIT_OPPOSITE = {"♦": "♣", "♣": "♦", "♥": "♠", "♠": "♥"}
SUIT_EMOJI = {"♦": "♦️", "♣": "♣️", "♥": "❤️", "♠": "♠️"}

def validate_config():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN manquant!")
    if not WEBHOOK_URL:
        print("⚠️ WEBHOOK_URL non défini - le bot ne recevra pas les mises à jour!")
    return True
