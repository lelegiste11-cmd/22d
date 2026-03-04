"""Configuration du bot Telegram Prediction"""
import os

# Telegram API credentials
API_ID = 29177661
API_HASH = "a8639172fa8d35dbfd8ea46286d349ab"

# Bot Token - À changer via variable d'environnement RENDER
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'VOTRE_BOT_TOKEN_ICI')

# IDs des canaux
SOURCE_CHANNEL_ID = -1002682552255      # Canal source (déclencheur)
PREDICTION_CHANNEL_ID = -1003430118891  # Canal prédictions (sortie)
STATS_CHANNEL_ID = -1003814088712       # Canal stats (vérification)
ADMIN_ID = 1190237801                   # ID admin pour notifications

# Configuration serveur
PORT = int(os.getenv('PORT', 10000))     # Port Render.com

# Configuration des costumes
ALL_SUITS = ['♠', '♥', '♦', '♣', '♠️', '♥️', '♦️', '♣️', '❤️', '❤']

SUIT_DISPLAY = {
    '♠': '♠️',
    '♥': '❤️',
    '♦': '♦️',
    '♣': '♣️',
    '♠️': '♠️',
    '♥️': '❤️',
    '♦️': '♦️',
    '♣️': '♣️',
    '❤️': '❤️',
    '❤': '❤️'
}

SUIT_NAMES = {
    '♠️': 'Pique',
    '❤️': 'Cœur',
    '♦️': 'Carreau',
    '♣️': 'Trèfle',
    '♠': 'Pique',
    '♥': 'Cœur',
    '♦': 'Carreau',
    '♣': 'Trèfle'
}

SUIT_MAPPING = {
    '♠': '♠️',
    '♥': '❤️',
    '♦': '♦️',
    '♣': '♣️'
}

# Configuration prédiction
PREDICTION_OFFSET = 2  # +2 après déclencheur (ex: #7 → prédit #9)
