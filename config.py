"""
Configuration du Bot Légiste pour Render.com
Toutes les variables d'environnement sont définies ici
"""

import os

# ─── Configuration API Telegram ─────────────────────────────────────────────
API_ID = int(os.environ.get("API_ID", "29177661"))
API_HASH = os.environ.get("API_HASH", "a8639172fa8d35dbfd8ea46286d349ab")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8703080099:AAFUf_rSBF0XxQE-HI78W48d3JGqCgM0DMA")

# ─── Configuration des IDs ───────────────────────────────────────────────────
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6180384006"))
SOURCE_CHANNEL_ID = int(os.environ.get("SOURCE_CHANNEL_ID", "-1002682552255"))
PREDICTION_CHANNEL_ID = int(os.environ.get("PREDICTION_CHANNEL_ID", "-1003504929751"))

# ─── Configuration Render.com ────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", "10000"))

# ─── Paramètres de prédiction ───────────────────────────────────────────────
DEFAULT_OFFSET = 2   # Offset normal (+2)
FAILURE_OFFSET = 4   # Offset après échec (+4)

# ─── Correspondance des couleurs opposées ────────────────────────────────────
SUIT_OPPOSITE = {
    "♦": "♣",
    "♣": "♦",
    "♥": "♠",
    "♠": "♥",
}

SUIT_EMOJI = {
    "♦": "♦️",
    "♣": "♣️",
    "♥": "❤️",
    "♠": "♠️",
}

# ─── Validation de la configuration ──────────────────────────────────────────
def validate_config():
    """Vérifie que toutes les variables essentielles sont présentes"""
    required_vars = [
        ("API_ID", API_ID),
        ("API_HASH", API_HASH),
        ("BOT_TOKEN", BOT_TOKEN),
        ("ADMIN_ID", ADMIN_ID),
        ("SOURCE_CHANNEL_ID", SOURCE_CHANNEL_ID),
        ("PREDICTION_CHANNEL_ID", PREDICTION_CHANNEL_ID),
    ]
    
    missing = []
    for name, value in required_vars:
        if not value or value == 0:
            missing.append(name)
    
    if missing:
        raise RuntimeError(f"Variables d'environnement manquantes: {', '.join(missing)}")
    
    return True

# Validation au chargement
validate_config()
