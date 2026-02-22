import os
import asyncio
import re
import logging
import sys
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    PREDICTION_OFFSET, SUIT_MAPPING, ALL_SUITS, SUIT_DISPLAY, SUIT_NAMES
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

logger.info(f"Configuration: SOURCE_CHANNEL={SOURCE_CHANNEL_ID}, PREDICTION_CHANNEL={PREDICTION_CHANNEL_ID}")
logger.info(f"Paramètre de prédiction: OFFSET={PREDICTION_OFFSET}")

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

pending_predictions = {}
queued_predictions = {}
recent_games = {}
processed_messages = set()
processed_finalized = set()
last_transferred_game = None
current_game_number = 0
prediction_offset = PREDICTION_OFFSET

MAX_PENDING_PREDICTIONS = 5
PROXIMITY_THRESHOLD = 2

source_channel_ok = False
prediction_channel_ok = False

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message"""
    match = re.search(r"#N\s*(\d+)\.?", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def extract_parentheses_groups(message: str):
    """Extrait le contenu des parenthèses"""
    return re.findall(r"\(([^)]*)\)", message)

def normalize_suits(group_str: str) -> str:
    """Normalise les symboles de couleur"""
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def get_suits_in_group(group_str: str):
    """Retourne la liste des couleurs présentes dans le groupe"""
    normalized = normalize_suits(group_str)
    return [s for s in ALL_SUITS if s in normalized]

def extract_first_card_suit(group_str: str):
    """Extrait la couleur de la première carte du groupe"""
    normalized = normalize_suits(group_str)
    for char in normalized:
        if char in ALL_SUITS:
            return SUIT_DISPLAY.get(char, char)
    return None

def get_suit_full_name(suit_symbol: str) -> str:
    """Retourne le nom complet de la couleur"""
    return SUIT_NAMES.get(suit_symbol, suit_symbol)

def get_alternate_suit(suit: str) -> str:
    """Retourne la couleur alternative (pour backup)"""
    return SUIT_MAPPING.get(suit, suit)

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé (contient ✅ ou 🔰)"""
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message

def format_prediction_message(game_number: int, suit: str, status: str = "⏳ EN COURS", result_group: str = None) -> str:
    """
    Formate le message de prédiction avec les nouveaux emojis:
    📡 PRÉDICTION #720
    🎯 Couleur: ♦️ carreaux
    📋 Résultat: (Q♦️2♦️5♦️)
    🌪️ Statut: ✅0️⃣ GAGNÉ
    """
    suit_name = get_suit_full_name(suit)
    
    if status == "⏳ EN COURS":
        # Message de prédiction initial
        return f"""📡 PRÉDICTION #{game_number}
🎯 Couleur: {suit} {suit_name}
🌪️ Statut: {status}"""
    else:
        # Message de résultat avec les cartes trouvées
        if result_group:
            return f"""📡 PRÉDICTION #{game_number}
🎯 Couleur: {suit} {suit_name}
📋 Résultat: ({result_group})
🌪️ Statut: {status}"""
        else:
            return f"""📡 PRÉDICTION #{game_number}
🎯 Couleur: {suit} {suit_name}
🌪️ Statut: {status}"""

async def send_prediction_to_channel(target_game: int, suit: str, base_game: int):
    """Envoie une prédiction au canal de prédiction"""
    try:
        prediction_msg = format_prediction_message(target_game, suit, "⏳ EN COURS")
        
        msg_id = 0

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and prediction_channel_ok:
            try:
                pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
                msg_id = pred_msg.id
                logger.info(f"✅ Prédiction envoyée au canal: Jeu #{target_game} - {suit}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi prédiction au canal: {e}")
        else:
            logger.warning(f"⚠️ Canal de prédiction non accessible, prédiction non envoyée")

        pending_predictions[target_game] = {
            'message_id': msg_id,
            'suit': suit,
            'base_game': base_game,
            'status': '⏳ EN COURS',
            'check_count': 0,
            'created_at': datetime.now().isoformat()
        }

        logger.info(f"Prédiction active créée: Jeu #{target_game} - {suit} (basé sur #{base_game})")
        return msg_id

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

async def update_prediction_status(game_number: int, new_status: str, result_group: str = None):
    """
    Met à jour le statut d'une prédiction dans le canal avec le résultat réel
    """
    try:
        if game_number not in pending_predictions:
            return False

        pred = pending_predictions[game_number]
        message_id = pred['message_id']
        suit = pred['suit']
        
        # Formater le statut avec le texte GAGNÉ/PERDU
        if new_status.startswith('✅'):
            status_text = f"{new_status} GAGNÉ"
        elif new_status == '❌':
            status_text = f"{new_status} PERDU"
        else:
            status_text = new_status
        
        # Créer le message avec le résultat réel
        updated_msg = format_prediction_message(game_number, suit, status_text, result_group)

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
                logger.info(f"✅ Prédiction #{game_number} mise à jour: {status_text} | Résultat: ({result_group})")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour dans le canal: {e}")

        pred['status'] = new_status
        logger.info(f"Prédiction #{game_number} statut mis à jour: {new_status}")

        # Supprimer des prédictions actives si terminée
        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '✅3️⃣', '❌']:
            del pending_predictions[game_number]
            logger.info(f"Prédiction #{game_number} terminée et supprimée")

        return True

    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """
    Vérifie si une prédiction est gagnée ou perdue.
    Affiche le résultat réel (premier groupe) dans la mise à jour.
    """
    # Normaliser le groupe pour analyse
    normalized_group = normalize_suits(first_group)
    
    # ========== VÉRIFICATION N (numéro prédit) ==========
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        target_suit = pred['suit']
        normalized_target = normalize_suits(target_suit)
        
        # Compter combien de fois la couleur apparaît dans le premier groupe
        suit_count = normalized_group.count(normalized_target)
        
        logger.info(f"🔍 Vérification #{game_number} (N): {target_suit} trouvé {suit_count} fois dans '{first_group}'")
        
        # Si 3 cartes de la couleur prédite trouvées
        if suit_count >= 3:
            await update_prediction_status(game_number, '✅0️⃣', first_group)
            logger.info(f"🎉 Prédiction #{game_number} GAGNÉE immédiatement! ({suit_count}x {target_suit}) | Résultat: ({first_group})")
            return True
        else:
            # Marquer qu'on a vérifié une fois (N), passer à N+1
            pred['check_count'] = 1
            logger.info(f"⏳ Prédiction #{game_number}: {suit_count}x {target_suit} trouvé, attente vérification N+1...")
    
    # ========== VÉRIFICATION N+1 ==========
    prev_game = game_number - 1
    if prev_game in pending_predictions:
        pred = pending_predictions[prev_game]
        if pred.get('check_count', 0) == 1:
            target_suit = pred['suit']
            normalized_target = normalize_suits(target_suit)
            
            suit_count = normalized_group.count(normalized_target)
            logger.info(f"🔍 Vérification #{prev_game}+1 (N+1): {target_suit} trouvé {suit_count} fois dans '{first_group}'")
            
            if suit_count >= 3:
                await update_prediction_status(prev_game, '✅1️⃣', first_group)
                logger.info(f"🎉 Prédiction #{prev_game} GAGNÉE au N+1! ({suit_count}x {target_suit}) | Résultat: ({first_group})")
                return True
            else:
                pred['check_count'] = 2
                logger.info(f"⏳ Prédiction #{prev_game}: {suit_count}x {target_suit} en N+1, attente N+2...")
    
    # ========== VÉRIFICATION N+2 ==========
    prev_prev_game = game_number - 2
    if prev_prev_game in pending_predictions:
        pred = pending_predictions[prev_prev_game]
        if pred.get('check_count', 0) == 2:
            target_suit = pred['suit']
            normalized_target = normalize_suits(target_suit)
            
            suit_count = normalized_group.count(normalized_target)
            logger.info(f"🔍 Vérification #{prev_prev_game}+2 (N+2): {target_suit} trouvé {suit_count} fois dans '{first_group}'")
            
            if suit_count >= 3:
                await update_prediction_status(prev_prev_game, '✅2️⃣', first_group)
                logger.info(f"🎉 Prédiction #{prev_prev_game} GAGNÉE au N+2! ({suit_count}x {target_suit}) | Résultat: ({first_group})")
                return True
            else:
                pred['check_count'] = 3
                logger.info(f"⏳ Prédiction #{prev_prev_game}: {suit_count}x {target_suit} en N+2, attente N+3...")
    
    # ========== VÉRIFICATION N+3 ==========
    prev_prev_prev_game = game_number - 3
    if prev_prev_prev_game in pending_predictions:
        pred = pending_predictions[prev_prev_prev_game]
        if pred.get('check_count', 0) == 3:
            target_suit = pred['suit']
            normalized_target = normalize_suits(target_suit)
            
            suit_count = normalized_group.count(normalized_target)
            logger.info(f"🔍 Vérification #{prev_prev_prev_game}+3 (N+3): {target_suit} trouvé {suit_count} fois dans '{first_group}'")
            
            if suit_count >= 3:
                await update_prediction_status(prev_prev_prev_game, '✅3️⃣', first_group)
                logger.info(f"🎉 Prédiction #{prev_prev_prev_game} GAGNÉE au N+3! ({suit_count}x {target_suit}) | Résultat: ({first_group})")
                return True
            else:
                # Échec après 4 tentatives
                await update_prediction_status(prev_prev_prev_game, '❌', first_group)
                logger.info(f"💔 Prédiction #{prev_prev_prev_game} PERDUE après 4 vérifications | Résultat: ({first_group})")
                
                # Créer une prédiction backup
                backup_game = prev_prev_prev_game + prediction_offset
                alternate_suit = get_alternate_suit(target_suit)
                await create_prediction(backup_game, alternate_suit, prev_prev_prev_game, is_backup=True)
                return False
    
    return None

async def create_prediction(target_game: int, suit: str, base_game: int, is_backup: bool = False):
    """Crée une nouvelle prédiction"""
    if target_game in pending_predictions or target_game in queued_predictions:
        logger.info(f"Prédiction #{target_game} déjà existante, ignorée")
        return False
    
    distance = target_game - current_game_number
    
    if distance <= PROXIMITY_THRESHOLD and distance > 0:
        await send_prediction_to_channel(target_game, suit, base_game)
    elif distance > 0:
        queued_predictions[target_game] = {
            'target_game': target_game,
            'suit': suit,
            'base_game': base_game,
            'queued_at': datetime.now().isoformat()
        }
        logger.info(f"📋 Prédiction #{target_game} ({suit}) mise en file d'attente (dans {distance} jeux)")
    else:
        logger.warning(f"⚠️ Prédiction #{target_game} expirée, ignorée")
    
    return True

async def process_new_message(message_text: str, chat_id: int, is_finalized: bool = False):
    """
    Traite un nouveau message du canal source.
    - Crée les prédictions immédiatement
    - Vérifie les résultats UNIQUEMENT si le message est finalisé
    """
    global current_game_number, last_transferred_game
    
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            return
        
        current_game_number = game_number
        
        # Éviter le traitement double des messages non finalisés
        message_hash = f"{game_number}_{message_text[:50]}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)
        
        if len(processed_messages) > 200:
            processed_messages.clear()
        
        groups = extract_parentheses_groups(message_text)
        if len(groups) < 1:
            return
        
        first_group = groups[0]
        
        logger.info(f"Jeu #{game_number} traité - Groupe1: {first_group} - Finalisé: {is_finalized}")
        
        # ========== CRÉATION DE PRÉDICTION (TOUJOURS) ==========
        first_card_suit = extract_first_card_suit(first_group)
        
        if first_card_suit:
            target_game = game_number + prediction_offset
            
            if len(pending_predictions) < MAX_PENDING_PREDICTIONS:
                await create_prediction(target_game, first_card_suit, game_number)
            else:
                logger.info(f"⏸️ Max prédictions atteint ({MAX_PENDING_PREDICTIONS}), attente...")
        else:
            logger.warning(f"⚠️ Jeu #{game_number}: impossible d'extraire la couleur de la première carte")
        
        # ========== VÉRIFICATION DES RÉSULTATS (UNIQUEMENT SI FINALISÉ) ==========
        if is_finalized:
            finalized_hash = f"finalized_{game_number}"
            if finalized_hash not in processed_finalized:
                processed_finalized.add(finalized_hash)
                
                # Transfert du message si activé
                if transfer_enabled and ADMIN_ID and ADMIN_ID != 0 and last_transferred_game != game_number:
                    try:
                        transfer_msg = f"📨 **Message finalisé du canal source:**\n\n{message_text}"
                        await client.send_message(ADMIN_ID, transfer_msg)
                        last_transferred_game = game_number
                        logger.info(f"✅ Message #{game_number} transféré à l'admin")
                    except Exception as e:
                        logger.error(f"❌ Erreur transfert: {e}")
                
                # Vérifier les résultats avec le premier groupe
                logger.info(f"✅ Message #{game_number} finalisé - Lancement vérification résultats avec: ({first_group})")
                await check_prediction_result(game_number, first_group)
                
                if len(processed_finalized) > 100:
                    processed_finalized.clear()
        
        # Traiter les prédictions en file d'attente
        await process_queued_predictions(game_number)
        
        # Stocker le jeu pour référence
        recent_games[game_number] = {
            'first_group': first_group,
            'timestamp': datetime.now().isoformat()
        }
        
        if len(recent_games) > 100:
            oldest = min(recent_games.keys())
            del recent_games[oldest]
            
    except Exception as e:
        logger.error(f"Erreur traitement message: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def process_queued_predictions(current_game: int):
    """Traite les prédictions en file d'attente qui sont proches"""
    global current_game_number
    current_game_number = current_game
    
    if len(pending_predictions) >= MAX_PENDING_PREDICTIONS:
        logger.info(f"⏸️ {len(pending_predictions)} prédictions en cours (max {MAX_PENDING_PREDICTIONS})")
        return
    
    sorted_queued = sorted(queued_predictions.keys())
    
    for target_game in sorted_queued:
        if len(pending_predictions) >= MAX_PENDING_PREDICTIONS:
            break
        
        distance = target_game - current_game
        
        if distance <= PROXIMITY_THRESHOLD and distance > 0:
            pred_data = queued_predictions.pop(target_game)
            logger.info(f"🎯 Jeu #{current_game} - Prédiction #{target_game} proche ({distance} jeux), envoi!")
            await send_prediction_to_channel(
                pred_data['target_game'],
                pred_data['suit'],
                pred_data['base_game']
            )
        elif distance <= 0:
            logger.warning(f"⚠️ Prédiction #{target_game} expirée, supprimée")
            queued_predictions.pop(target_game, None)

# ==================== EVENT HANDLERS ====================

@client.on(events.NewMessage())
async def handle_message(event):
    """Gère les nouveaux messages - CRÉE LES PRÉDICTIONS IMMÉDIATEMENT"""
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id
        
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"📨 Message reçu: {message_text[:80]}...")
            
            is_finalized = is_message_finalized(message_text)
            await process_new_message(message_text, chat_id, is_finalized)
            
    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

@client.on(events.MessageEdited())
async def handle_edited_message(event):
    """Gère les messages édités (finalisation) - VÉRIFIE LES RÉSULTATS"""
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id
        
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"✏️ Message édité: {message_text[:80]}...")
            
            is_finalized = is_message_finalized(message_text)
            
            if is_finalized:
                logger.info(f"✅ Message finalisé détecté - Lancement vérification")
                await process_new_message(message_text, chat_id, is_finalized=True)
            else:
                logger.info(f"⏳ Message édité mais pas encore finalisé")
            
    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# ==================== COMMANDES ADMIN ====================

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    
    await event.respond("""🤖 **Bot de Prédiction Baccarat - v2.3**

✨ **Nouveau design:**
📡 PRÉDICTION #720
🎯 Couleur: ♦️ carreaux
📋 Résultat: (Q♦️2♦️5♦️)
🌪️ Statut: ✅0️⃣ GAGNÉ

**Commandes:**
• `/status` - Voir les prédictions
• `/setoffset <n>` - Changer le décalage
• `/help` - Aide détaillée
• `/debug` - Infos système""")

@client.on(events.NewMessage(pattern='/setoffset'))
async def cmd_setoffset(event):
    if event.is_group or event.is_channel:
        return
    
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Commande réservée à l'administrateur")
        return
    
    global prediction_offset
    
    try:
        text = event.message.message
        parts = text.split()
        
        if len(parts) < 2:
            await event.respond(f"⚠️ Usage: `/setoffset <nombre>`\nValeur actuelle: **{prediction_offset}**")
            return
        
        new_offset = int(parts[1])
        
        if new_offset < 1 or new_offset > 20:
            await event.respond("⚠️ Le décalage doit être entre 1 et 20")
            return
        
        prediction_offset = new_offset
        await event.respond(f"✅ Décalage mis à jour: **{prediction_offset}**")
        
    except ValueError:
        await event.respond("⚠️ Entrez un nombre valide. Ex: `/setoffset 3`")
    except Exception as e:
        logger.error(f"Erreur setoffset: {e}")
        await event.respond(f"❌ Erreur: {str(e)}")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Commande réservée à l'administrateur")
        return
    
    status_msg = f"📊 **État des prédictions:**\n\n"
    status_msg += f"🎮 Jeu actuel: #{current_game_number}\n"
    status_msg += f"📏 Décalage: +{prediction_offset}\n\n"
    
    if pending_predictions:
        status_msg += f"**🔮 Prédictions actives ({len(pending_predictions)}/{MAX_PENDING_PREDICTIONS}):**\n"
        for game_num, pred in sorted(pending_predictions.items()):
            distance = game_num - current_game_number
            suit_name = get_suit_full_name(pred['suit'])
            check_info = f"(vérifié {pred['check_count']}x)" if pred['check_count'] > 0 else "(attente)"
            status_msg += f"• #{game_num}: {pred['suit']} {suit_name} - {pred['status']} {check_info}\n"
    else:
        status_msg += "**🔮 Aucune prédiction active**\n"
    
    if queued_predictions:
        status_msg += f"\n**📋 En file d'attente ({len(queued_predictions)}):**\n"
        for game_num, pred in sorted(queued_predictions.items()):
            distance = game_num - current_game_number
            suit_name = get_suit_full_name(pred['suit'])
            status_msg += f"• #{game_num}: {pred['suit']} {suit_name} - dans {distance} jeux\n"
    
    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/debug'))
async def cmd_debug(event):
    if event.is_group or event.is_channel:
        return
    
    debug_msg = f"""🔍 **Informations de débogage:**

**Configuration:**
• Source: {SOURCE_CHANNEL_ID}
• Prédiction: {PREDICTION_CHANNEL_ID}
• Décalage: {prediction_offset}

**État:**
• Jeu actuel: #{current_game_number}
• Actives: {len(pending_predictions)}/{MAX_PENDING_PREDICTIONS}
• File d'attente: {len(queued_predictions)}

**🆕 v2.3 - Design:**
📡 PRÉDICTION #N
🎯 Couleur: [suit] [nom]
📋 Résultat: ([cartes])
🌪️ Statut: [statut]

**Fonctionnement:**
• ✅ Prédictions: **IMMÉDIATES**
• ✅ Résultats: **AVEC CARTES RÉELLES**
• ✅ Vérification: **UNIQUEMENT** sur finalisés
• ✅ Condition: **3 cartes** de la couleur
• ✅ Étapes: N, N+1, N+2, N+3
"""
    await event.respond(debug_msg)

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    
    await event.respond(f"""📖 **Aide - Bot v2.3**

**🎯 Nouveau format:**

**PRÉDICTION INITIALE:**

**📊 Statuts possibles:**
• ⏳ EN COURS = En attente de vérification
• ✅0️⃣ GAGNÉ = Gagné au numéro prédit (N)
• ✅1️⃣ GAGNÉ = Gagné au numéro+1 (N+1)
• ✅2️⃣ GAGNÉ = Gagné au numéro+2 (N+2)
• ✅3️⃣ GAGNÉ = Gagné au numéro+3 (N+3)
• ❌ PERDU = Perdu (pas trouvé après N+3)

**Commandes:**
• `/setoffset <n>` - Décalage actuel: {prediction_offset}
• `/status` - Voir les prédictions
• `/debug` - Infos système""")

# ==================== TRANSFERT COMMANDS ====================

transfer_enabled = True

@client.on(events.NewMessage(pattern='/transfert'))
async def cmd_transfert(event):
    if event.is_group or event.is_channel:
        return
    global transfer_enabled
    transfer_enabled = True
    await event.respond("✅ Transfert activé!")

@client.on(events.NewMessage(pattern='/stoptransfert'))
async def cmd_stop_transfert(event):
    if event.is_group or event.is_channel:
        return
    global transfer_enabled
    transfer_enabled = False
    await event.respond("⛔ Transfert désactivé.")

# ==================== WEB SERVER ====================

async def index(request):
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bot Prédiction Baccarat v2.3</title>
        <meta charset="utf-8">
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background: #1a1a2e; color: #eee; }}
            h1 {{ color: #00d4ff; }}
            .status {{ background: #16213e; padding: 20px; border-radius: 10px; margin: 20px 0; }}
            .metric {{ margin: 10px 0; }}
            a {{ color: #00d4ff; }}
        </style>
    </head>
    <body>
        <h1>📡 Bot de Prédiction Baccarat v2.3</h1>
        <p>Nouveau design avec résultats réels</p>
        
        <div class="status">
            <h3>📊 Statut</h3>
            <div class="metric"><strong>Jeu actuel:</strong> #{current_game_number}</div>
            <div class="metric"><strong>Décalage:</strong> +{prediction_offset}</div>
            <div class="metric"><strong>Prédictions actives:</strong> {len(pending_predictions)}/{MAX_PENDING_PREDICTIONS}</div>
        </div>
        
        <ul>
            <li><a href="/health">Health Check</a></li>
            <li><a href="/status">Statut JSON</a></li>
        </ul>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def status_api(request):
    status_data = {
        "status": "running",
        "version": "2.3",
        "current_game": current_game_number,
        "prediction_offset": prediction_offset,
        "pending_predictions": len(pending_predictions),
        "timestamp": datetime.now().isoformat()
    }
    return web.json_response(status_data)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)
    app.router.add_get('/status', status_api)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Serveur web démarré sur 0.0.0.0:{PORT}")

async def start_bot():
    global source_channel_ok, prediction_channel_ok
    try:
        logger.info("🚀 Démarrage Bot v2.3...")
        logger.info("📡 Nouveau design: PRÉDICTION avec 🎯🌪️")
        await client.start(bot_token=BOT_TOKEN)
        logger.info("✅ Bot connecté")
        
        me = await client.get_me()
        logger.info(f"Bot: @{getattr(me, 'username', 'Unknown')}")
        
        # Vérifier canaux
        try:
            source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
            source_channel_ok = True
            logger.info(f"✅ Source: {getattr(source_entity, 'title', 'N/A')}")
        except Exception as e:
            logger.error(f"❌ Source: {e}")
        
        try:
            pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
            try:
                test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🤖 Bot v2.3 connecté!")
                await asyncio.sleep(1)
                await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
                prediction_channel_ok = True
                logger.info(f"✅ Prédiction: {getattr(pred_entity, 'title', 'N/A')}")
            except Exception as e:
                logger.warning(f"⚠️ Prédiction sans écriture: {e}")
        except Exception as e:
            logger.error(f"❌ Prédiction: {e}")
        
        logger.info(f"⚙️ OFFSET={prediction_offset}, MAX={MAX_PENDING_PREDICTIONS}")
        logger.info("🎯 3 cartes requises | N→N+3 | Design 📡🎯📋🌪️")
        return True
        
    except Exception as e:
        logger.error(f"Erreur: {e}")
        return False

async def main():
    try:
        await start_web_server()
        success = await start_bot()
        if not success:
            return
        logger.info("🤖 Bot v2.3 opérationnel!")
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Erreur: {e}")
    finally:
        await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêt")
    except Exception as e:
        logger.error(f"Fatal: {e}")
