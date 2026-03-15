"""
Configuration du bot Telegram de prédiction Baccarat
"""

# ==========================================
# IDENTIFIANTS TELEGRAM (HARDCODÉS)
# ==========================================

# API Telegram (obtenu sur https://my.telegram.org)
API_ID = 29177661
API_HASH = "a8639172fa8d35dbfd8ea46286d349ab"

# Bot Token (@BotFather)
BOT_TOKEN = "8703080099:AAFUf_rSBF0XxQE-HI78W48d3JGqCgM0DMA"

# ID de l'administrateur (pour commandes privées)
ADMIN_ID = 6180384006

# ID des canaux Telegram
SOURCE_CHANNEL_ID = -1002682552255      # Canal source Baccarat
PREDICTION_CHANNEL_ID = -1003504929751  # Canal de prédiction

# Port pour le serveur web (Render.com utilise 10000 par défaut)
PORT = 10000

# ==========================================
# MAPPING DES COULEURS
# ==========================================

# Mapping pour l'opposé des couleurs
# ♣️ <-> ♠️ (Trèfle <-> Pique)
# ❤️ <-> ♦️ (Cœur <-> Carreau)
SUIT_MAPPING = {
    '♠️': '♣️',
    '♠': '♣️',
    '❤️': '♦️',
    '❤': '♦️',
    '♥️': '♦️',
    '♥': '♦️',
    '♣️': '♠️',
    '♣': '♠️',
    '♦️': '❤️',
    '♦': '❤️'
}

# Toutes les couleurs possibles
ALL_SUITS = ['♠', '♥', '♦', '♣']

# Affichage standardisé des couleurs
SUIT_DISPLAY = {
    '♠': '♠️',
    '♥': '❤️',
    '♦': '♦️',
    '♣': '♣️'
}

# Noms des couleurs pour l'affichage
SUIT_NAMES = {
    '♠️': 'Pique',
    '♠': 'Pique',
    '❤️': 'Cœur',
    '❤': 'Cœur',
    '♥️': 'Cœur',
    '♥': 'Cœur',
    '♦️': 'Carreau',
    '♦': 'Carreau',
    '♣️': 'Trèfle',
    '♣': 'Trèfle'
}
