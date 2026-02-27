"""
Configuration du bot Telegram de prédiction Baccarat v4.1
Mode: Automatique avec contrôle total facile
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
# MODE FONCTIONNEMENT (NOUVEAU)
# ============================================

# Mode principal: "auto" ou "manual"
# "auto" = Prédictions automatiques activées (défaut)
# "manual" = Prédictions désactivées, contrôle manuel uniquement
BOT_MODE = os.getenv('BOT_MODE', 'auto')

# Activation/Désactivation des fonctionnalités (True/False)
# Modifiez ces valeurs pour contrôler le comportement sans toucher au code principal
AUTO_PREDICTION_ENABLED = True      # Prédictions automatiques (True/False)
AUTO_RESTART_ON_TIMEOUT = True      # Redémarrage après inactivité (True/False)
AUTO_RESTART_ON_MAX_GAME = True     # Redémarrage au jeu #1440 (True/False)
ADMIN_NOTIFICATIONS = True          # Notifications vers l'admin (True/False)

# ============================================
# PARAMETRES DE REDÉMARRAGE (MODIFIÉ)
# ============================================

# Timeout de redémarrage (minutes d'inactivité)
# MODIFIÉ: 10 minutes au lieu de 12
RESTART_TIMEOUT_MINUTES = 10

# Numéro de jeu maximum avant redémarrage forcé
# NOUVEAU: Redémarrage auto lorsque le bot détecte le jeu #1440
MAX_GAME_NUMBER = 1440

# ============================================
# PARAMETRES DE PREDICTION
# ============================================

# Décalage de prédiction (défaut: 2) - Nombre de jeux à ajouter pour la prédiction
# Ex: Si N=718 et PREDICTION_OFFSET=2 → Prédiction pour #720
PREDICTION_OFFSET = int(os.getenv('PREDICTION_OFFSET') or '2')

# Timeout de prédiction (défaut: 10) - Nombre de jeux avant expiration d'une prédiction
# Si le jeu actuel dépasse predicted_num + 10, la prédiction expire
PREDICTION_TIMEOUT = int(os.getenv('PREDICTION_TIMEOUT') or '10')

# Écart minimum entre deux prédictions (en numéros de jeu)
PREDICTION_GAP = 2

# Nombre maximum de prédictions simultanées
MAX_PENDING_PREDICTIONS = 5

# Seuil de proximité pour éviter les doublons
PROXIMITY_THRESHOLD = 2

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

# ============================================
# VALIDATION CONFIGURATION
# ============================================

def validate_config():
    """Valide la configuration au démarrage"""
    errors = []
    
    if API_ID == 0:
        errors.append("API_ID manquant")
    if not API_HASH:
        errors.append("API_HASH manquant")
    if not BOT_TOKEN:
        errors.append("BOT_TOKEN manquant")
    if ADMIN_ID == 0:
        errors.append("ADMIN_ID manquant")
    if SOURCE_CHANNEL_ID == 0:
        errors.append("SOURCE_CHANNEL_ID manquant")
    if PREDICTION_CHANNEL_ID == 0:
        errors.append("PREDICTION_CHANNEL_ID manquant")
    
    if errors:
        raise ValueError(f"Configuration invalide: {', '.join(errors)}")
    
    return True

# Validation au chargement si exécuté directement
if __name__ == "__main__":
    validate_config()
    print("✅ Configuration valide")
    print(f"   Mode: {BOT_MODE}")
    print(f"   Timeout redémarrage: {RESTART_TIMEOUT_MINUTES} min")
    print(f"   Max jeu: {MAX_GAME_NUMBER}")
    print(f"   Prédictions auto: {AUTO_PREDICTION_ENABLED}")
