"""
Configuration du bot Telegram de prédiction Baccarat
"""
import os

def parse_channel_id(env_var: str, default: str) -> int:
    value = os.getenv(env_var) or default
    channel_id = int(value)
    if channel_id > 0 and len(str(channel_id)) >= 10:
        channel_id = -channel_id
    return channel_id

# Identifiants des canaux
SOURCE_CHANNEL_ID = parse_channel_id('SOURCE_CHANNEL_ID', '-1002682552255')
PREDICTION_CHANNEL_ID = parse_channel_id('PREDICTION_CHANNEL_ID', '-1003664468884')

# Identifiant de l'administrateur
ADMIN_ID = int(os.getenv('ADMIN_ID') or '1190237801')

# Credentials Telegram API
API_ID = int(os.getenv('API_ID') or '29177661')
API_HASH = os.getenv('API_HASH') or 'a8639172fa8d35dbfd8ea46286d349ab'
BOT_TOKEN = os.getenv('BOT_TOKEN') or '8458163781:AAG7n_qncj-XSlylwtKS--p9axDLsTO7r7M'

# Port pour le serveur web (Render.com utilise 10000)
PORT = int(os.getenv('PORT') or '10000')

# Paramètre 'a' pour la prédiction (nombre entier naturel, défaut = 2)
PREDICTION_OFFSET = int(os.getenv('PREDICTION_OFFSET') or '2')

# Mapping des couleurs
SUIT_MAPPING = {
    '♠️': '❤️',
    '♠': '❤️',
    '❤️': '♠️',
    '❤': '♠️',
    '♥️': '♠️',
    '♥': '♠️',
    '♣️': '♦️',
    '♣': '♦️',
    '♦️': '♣️',
    '♦': '♣️'
}

# Liste des couleurs disponibles
ALL_SUITS = ['♠', '♥', '♦', '♣']

# Affichage des couleurs avec emoji
SUIT_DISPLAY = {
    '♠': '♠️',
    '♥': '❤️',
    '♦': '♦️',
    '♣': '♣️'
}

# Noms complets des couleurs
SUIT_NAMES = {
    '♠️': 'Pique',
    '♠': 'Pique',
    '❤️': 'Cœur',
    '❤': 'Cœur',
    '♥️': 'Cœur',
    '♥': 'Cœur',
    '♦️': 'carreaux',
    '♦': 'carreaux',
    '♣️': 'trèfle',
    '♣': 'trèfle'
}
