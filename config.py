"""
Configuration du bot Telegram de prédiction Baccarat
"""
import os

def parse_channel_id(env_var: str, default: str) -> int:
    """Parse et normalise l'ID du canal"""
    value = os.getenv(env_var) or default
    channel_id = int(value)
    # Les IDs de canaux doivent être négatifs pour les supergroupes
    if channel_id > 0 and len(str(channel_id)) >= 10:
        channel_id = -channel_id
    return channel_id

# ============================================
# CONFIGURATION DES CANAUX
# ============================================

# Canal source (où le bot lit les messages)
SOURCE_CHANNEL_ID = parse_channel_id('SOURCE_CHANNEL_ID', '-1002682552255')

# Canal de prédiction (où le bot envoie les prédictions)
PREDICTION_CHANNEL_ID = parse_channel_id('PREDICTION_CHANNEL_ID', '-1003664468884')

# ============================================
# CREDENTIALS TELEGRAM
# ============================================

# API Telegram (obtenu sur https://my.telegram.org)
API_ID = int(os.getenv('API_ID') or '29177661')
API_HASH = os.getenv('API_HASH') or 'a8639172fa8d35dbfd8ea46286d349ab'

# Token du bot (@BotFather) - MODIFIÉ LE 2026-02-24
BOT_TOKEN = os.getenv('BOT_TOKEN') or '8458163781:AAEtBcYICeVg_m3XAEQUnSwEX3G_06DA-YI'

# ID de l'administrateur (pour les commandes privées)
ADMIN_ID = int(os.getenv('ADMIN_ID') or '6180384006')

# ============================================
# CONFIGURATION SERVEUR
# ============================================

# Port pour Render.com (10000 par défaut)
PORT = int(os.getenv('PORT') or '10000')

# ============================================
# PARAMETRES DE PREDICTION
# ============================================

# Décalage de prédiction (défaut: 2) - Nombre de jeux à ajouter pour la prédiction
# Ex: Si N=718 et PREDICTION_OFFSET=2 → Prédiction pour #720
PREDICTION_OFFSET = int(os.getenv('PREDICTION_OFFSET') or '2')

# Timeout de prédiction (défaut: 10) - Nombre de jeux avant expiration d'une prédiction
# Si le jeu actuel dépasse predicted_num + 10, la prédiction expire
PREDICTION_TIMEOUT = int(os.getenv('PREDICTION_TIMEOUT') or '10')

# ============================================
# MAPPING DES COULEURS
# ============================================

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

ALL_SUITS = ['♠', '♥', '♦', '♣']
SUIT_DISPLAY = {
    '♠': '♠️',
    '♥': '❤️',
    '♦': '♦️',
    '♣': '♣️'
}

# Noms complets des couleurs pour l'affichage
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
