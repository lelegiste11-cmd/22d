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
    """
    Extrait la couleur de la première carte du groupe.
    Ex: "Q♦️5♥️A♥️" -> "♦️"
    """
    normalized = normalize_suits(group_str)
    
    # Chercher le premier symbole de couleur dans la chaîne
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

def format_prediction_message(game_number: int, suit: str, status: str = "🤔🤔🤔") -> str:
    """
    Formate le message de prédiction selon le nouveau format:
    🎰 PRÉDICTION #720
    💫 Couleur: ♦️ carreaux
    📊 Statut: 🤔🤔🤔
    """
    suit_name = get_suit_full_name(suit)
    
    if status == "🤔🤔🤔":
        # Message de prédiction initial
        return f"""🎰 PRÉDICTION #{game_number}
💫 Couleur: {suit} {suit_name}
📊 Statut: {status}"""
    else:
        # Message de résultat (avec 🎯 au lieu de 💫)
        return f"""🎰 PRÉDICTION #{game_number}
🎯 Couleur: {suit} {suit_name}
📊 Statut: {status}"""

async def send_prediction_to_channel(target_game: int, suit: str, base_game: int):
    """Envoie une prédiction au canal de prédiction"""
    try:
        prediction_msg = format_prediction_message(target_game, suit, "🤔🤔🤔")
        
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
            'status': '🤔🤔🤔',
            'check_count': 0,
            'created_at': datetime.now().isoformat()
        }

        logger.info(f"Prédiction active créée: Jeu #{target_game} - {suit} (basé sur #{base_game})")
        return msg_id

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

async def update_prediction_status(game_number: int, new_status: str, win_delay: int = 0):
    """
    Met à jour le statut d'une prédiction.
    win_delay: 0 = gagné immédiatement, 1 = gagné au jeu+1, 2 = gagné au jeu+2
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
        
        updated_msg = format_prediction_message(game_number, suit, status_text)

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
                logger.info(f"✅ Prédiction #{game_number} mise à jour: {status_text}")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour dans le canal: {e}")

        pred['status'] = new_status
        logger.info(f"Prédiction #{game_number} statut mis à jour: {new_status}")

        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '❌']:
            del pending_predictions[game_number]
            logger.info(f"Prédiction #{game_number} terminée et supprimée")

        return True

    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """
    Vérifie si une prédiction est gagnée ou perdue.
    Cherche la couleur prédite dans le premier groupe du jeu cible.
    """
    # Vérifier si on a une prédiction pour ce jeu
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        target_suit = pred['suit']
        
        # Vérifier si la couleur prédite est dans le premier groupe
        suits_in_group = get_suits_in_group(first_group)
        normalized_target = normalize_suits(target_suit)
        
        found = False
        for suit in suits_in_group:
            if suit in normalized_target:
                found = True
                break
        
        if found:
            await update_prediction_status(game_number, '✅0️⃣', 0)
            logger.info(f"🎉 Prédiction #{game_number} GAGNÉE immédiatement! ({target_suit} trouvé)")
            return True
        else:
            # Marquer qu'on a vérifié une fois
            pred['check_count'] = 1
            logger.info(f"🔍 Prédiction #{game_number}: {target_suit} non trouvé, attente jeu+1")
    
    # Vérifier le jeu précédent (N-1) pour voir s'il a gagné au délai +1
    prev_game = game_number - 1
    if prev_game in pending_predictions:
        pred = pending_predictions[prev_game]
        if pred.get('check_count', 0) == 1:
            target_suit = pred['suit']
            
            suits_in_group = get_suits_in_group(first_group)
            normalized_target = normalize_suits(target_suit)
            
            found = False
            for suit in suits_in_group:
                if suit in normalized_target:
                    found = True
                    break
            
            if found:
                await update_prediction_status(prev_game, '✅1️⃣', 1)
                logger.info(f"🎉 Prédiction #{prev_game} GAGNÉE au jeu+1! ({target_suit} trouvé)")
                return True
            else:
                pred['check_count'] = 2
                logger.info(f"🔍 Prédiction #{prev_game}: {target_suit} non trouvé au jeu+1, attente jeu+2")
    
    # Vérifier le jeu N-2 pour voir s'il a gagné au délai +2
    prev_prev_game = game_number - 2
    if prev_prev_game in pending_predictions:
        pred = pending_predictions[prev_prev_game]
        if pred.get('check_count', 0) == 2:
            target_suit = pred['suit']
            
            suits_in_group = get_suits_in_group(first_group)
            normalized_target = normalize_suits(target_suit)
            
            found = False
            for suit in suits_in_group:
                if suit in normalized_target:
                    found = True
                    break
            
            if found:
                await update_prediction_status(prev_prev_game, '✅2️⃣', 2)
                logger.info(f"🎉 Prédiction #{prev_prev_game} GAGNÉE au jeu+2! ({target_suit} trouvé)")
                return True
            else:
                # Échec après 3 tentatives
                await update_prediction_status(prev_prev_game, '❌')
                logger.info(f"💔 Prédiction #{prev_prev_game} PERDUE après 3 tentatives")
                
                # Créer une prédiction backup avec la couleur opposée
                backup_game = prev_prev_game + prediction_offset
                alternate_suit = get_alternate_suit(target_suit)
                await create_prediction(backup_game, alternate_suit, prev_prev_game, is_backup=True)
                return False
    
    return None

async def create_prediction(target_game: int, suit: str, base_game: int, is_backup: bool = False):
    """Crée une nouvelle prédiction"""
    if target_game in pending_predictions or target_game in queued_predictions:
        logger.info(f"Prédiction #{target_game} déjà existante, ignorée")
        return False
    
    # Vérifier la distance par rapport au jeu actuel
    distance = target_game - current_game_number
    
    if distance <= PROXIMITY_THRESHOLD and distance > 0:
        # Envoyer immédiatement si on est proche
        await send_prediction_to_channel(target_game, suit, base_game)
    elif distance > 0:
        # Mettre en file d'attente
        queued_predictions[target_game] = {
            'target_game': target_game,
            'suit': suit,
            'base_game': base_game,
            'queued_at': datetime.now().isoformat()
        }
        logger.info(f"📋 Prédiction #{target_game} ({suit}) mise en file d'attente (dans {distance} jeux)")
    else:
        logger.warning(f"⚠️ Prédiction #{target_game} expirée (jeu actuel: {current_game_number}), ignorée")
    
    return True

async def process_new_message(message_text: str, chat_id: int, is_finalized: bool = False):
    """
    Traite un nouveau message du canal source.
    - Si non finalisé: crée les prédictions immédiatement
    - Si finalisé: vérifie les résultats des prédictions existantes
    """
    global current_game_number, last_transferred_game
    
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            return
        
        current_game_number = game_number
        
        # Éviter le traitement double
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
        
        # Transfert du message si activé et finalisé
        if is_finalized and transfer_enabled and ADMIN_ID and ADMIN_ID != 0 and last_transferred_game != game_number:
            try:
                transfer_msg = f"📨 **Message finalisé du canal source:**\n\n{message_text}"
                await client.send_message(ADMIN_ID, transfer_msg)
                last_transferred_game = game_number
                logger.info(f"✅ Message #{game_number} transféré à l'admin")
            except Exception as e:
                logger.error(f"❌ Erreur transfert: {e}")
        
        # Si le message est finalisé, vérifier les résultats des prédictions
        if is_finalized:
            await check_prediction_result(game_number, first_group)
        
        # Traiter les prédictions en file d'attente (toujours, finalisé ou non)
        await process_queued_predictions(game_number)
        
        # Créer une nouvelle prédiction basée sur ce jeu (même si non finalisé)
        # Extraire la couleur de la première carte
        first_card_suit = extract_first_card_suit(first_group)
        
        if first_card_suit:
            target_game = game_number + prediction_offset
            
            # Vérifier si on peut créer la prédiction
            if len(pending_predictions) < MAX_PENDING_PREDICTIONS:
                await create_prediction(target_game, first_card_suit, game_number)
            else:
                logger.info(f"⏸️ Max prédictions atteint ({MAX_PENDING_PREDICTIONS}), attente...")
        else:
            logger.warning(f"⚠️ Jeu #{game_number}: impossible d'extraire la couleur de la première carte")
        
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
            logger.warning(f"⚠️ Prédiction #{target_game} expirée (jeu actuel: {current_game}), supprimée")
            queued_predictions.pop(target_game, None)

# ==================== EVENT HANDLERS ====================

@client.on(events.NewMessage())
async def handle_message(event):
    """Gère les nouveaux messages du canal source"""
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id
        
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"Message reçu du canal source: {message_text[:80]}...")
            
            # Déterminer si le message est finalisé
            is_finalized = is_message_finalized(message_text)
            
            # Traiter le message (créer prédiction si nouveau, vérifier si finalisé)
            await process_new_message(message_text, chat_id, is_finalized)
            
    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

@client.on(events.MessageEdited())
async def handle_edited_message(event):
    """Gère les messages édités (finalisation)"""
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id
        
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"Message édité dans canal source: {message_text[:80]}...")
            
            # Un message édité est potentiellement finalisé
            is_finalized = is_message_finalized(message_text)
            
            if is_finalized:
                logger.info(f"✅ Message finalisé détecté (édition)")
                await process_new_message(message_text, chat_id, is_finalized=True)
            
    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# ==================== COMMANDES ADMIN ====================

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    
    logger.info(f"Commande /start reçue de {event.sender_id}")
    await event.respond("""🤖 **Bot de Prédiction Baccarat - v2.0**

Nouveau système de prédiction basé sur la première carte!

**Commandes:**
• `/status` - Voir les prédictions en cours
• `/setoffset <nombre>` - Changer le décalage de prédiction (défaut: 2)
• `/help` - Aide détaillée
• `/debug` - Informations de débogage
• `/checkchannels` - Vérifier l'accès aux canaux""")

@client.on(events.NewMessage(pattern='/setoffset'))
async def cmd_setoffset(event):
    """Permet à l'admin de changer le paramètre de décalage"""
    if event.is_group or event.is_channel:
        return
    
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Commande réservée à l'administrateur")
        return
    
    global prediction_offset
    
    try:
        # Extraire le nombre de la commande
        text = event.message.message
        parts = text.split()
        
        if len(parts) < 2:
            await event.respond(f"⚠️ Usage: `/setoffset <nombre>`\n\nValeur actuelle: **{prediction_offset}**")
            return
        
        new_offset = int(parts[1])
        
        if new_offset < 1 or new_offset > 20:
            await event.respond("⚠️ Le décalage doit être entre 1 et 20")
            return
        
        prediction_offset = new_offset
        logger.info(f"Paramètre de prédiction changé par admin: offset = {prediction_offset}")
        await event.respond(f"✅ Paramètre de prédiction mis à jour!\n\nNouveau décalage: **{prediction_offset}**\n\nLes prochaines prédictions seront: Jeu actuel + {prediction_offset}")
        
    except ValueError:
        await event.respond("⚠️ Veuillez entrer un nombre valide. Exemple: `/setoffset 3`")
    except Exception as e:
        logger.error(f"Erreur setoffset: {e}")
        await event.respond(f"❌ Erreur: {str(e)}")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    
    logger.info(f"Commande /status reçue de {event.sender_id}")
    
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Commande réservée à l'administrateur")
        return
    
    status_msg = f"📊 **État des prédictions:**\n\n"
    status_msg += f"🎮 Jeu actuel: #{current_game_number}\n"
    status_msg += f"📏 Décalage de prédiction: +{prediction_offset}\n\n"
    
    if pending_predictions:
        status_msg += f"**🔮 Prédictions actives ({len(pending_predictions)}/{MAX_PENDING_PREDICTIONS}):**\n"
        for game_num, pred in sorted(pending_predictions.items()):
            distance = game_num - current_game_number
            suit_name = get_suit_full_name(pred['suit'])
            status_msg += f"• #{game_num}: {pred['suit']} ({suit_name}) - {pred['status']} (dans {distance} jeux)\n"
    else:
        status_msg += "**🔮 Aucune prédiction active**\n"
    
    if queued_predictions:
        status_msg += f"\n**📋 En file d'attente ({len(queued_predictions)}):**\n"
        for game_num, pred in sorted(queued_predictions.items()):
            distance = game_num - current_game_number
            suit_name = get_suit_full_name(pred['suit'])
            status_msg += f"• #{game_num}: {pred['suit']} ({suit_name}) - dans {distance} jeux\n"
    
    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/debug'))
async def cmd_debug(event):
    if event.is_group or event.is_channel:
        return
    
    logger.info(f"Commande /debug reçue de {event.sender_id}")
    
    debug_msg = f"""🔍 **Informations de débogage:**

**Configuration:**
• Source Channel: {SOURCE_CHANNEL_ID}
• Prediction Channel: {PREDICTION_CHANNEL_ID}
• Admin ID: {ADMIN_ID}
• Décalage prédiction: {prediction_offset}

**Accès aux canaux:**
• Canal source: {'✅ OK' if source_channel_ok else '❌ Non accessible'}
• Canal prédiction: {'✅ OK' if prediction_channel_ok else '❌ Non accessible'}

**État:**
• Jeu actuel: #{current_game_number}
• Prédictions actives: {len(pending_predictions)}/{MAX_PENDING_PREDICTIONS}
• En file d'attente: {len(queued_predictions)}
• Jeux récents: {len(recent_games)}
• Port: {PORT}

**Règles actuelles:**
• Prédiction: Jeu actuel + {prediction_offset}
• Basée sur: Première carte du premier groupe
• Max prédictions: {MAX_PENDING_PREDICTIONS}
• Seuil proximité: {PROXIMITY_THRESHOLD} jeux
• Vérification: Attend message finalisé ✅
"""
    await event.respond(debug_msg)

@client.on(events.NewMessage(pattern='/checkchannels'))
async def cmd_checkchannels(event):
    global source_channel_ok, prediction_channel_ok
    
    if event.is_group or event.is_channel:
        return
    
    logger.info(f"Commande /checkchannels reçue de {event.sender_id}")
    await event.respond("🔍 Vérification des accès aux canaux...")
    
    result_msg = "📡 **Résultat de la vérification:**\n\n"
    
    try:
        source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
        source_title = getattr(source_entity, 'title', 'N/A')
        source_channel_ok = True
        result_msg += f"✅ **Canal source** ({SOURCE_CHANNEL_ID}):\n"
        result_msg += f"   Nom: {source_title}\n"
        result_msg += f"   Statut: Accessible\n\n"
    except Exception as e:
        source_channel_ok = False
        result_msg += f"❌ **Canal source** ({SOURCE_CHANNEL_ID}):\n"
        result_msg += f"   Erreur: {str(e)[:100]}\n\n"
    
    try:
        pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
        pred_title = getattr(pred_entity, 'title', 'N/A')
        
        try:
            test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🔍 Test...")
            await asyncio.sleep(1)
            await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
            prediction_channel_ok = True
            result_msg += f"✅ **Canal prédiction** ({PREDICTION_CHANNEL_ID}):\n"
            result_msg += f"   Nom: {pred_title}\n"
            result_msg += f"   Statut: Accessible avec droits d'écriture\n\n"
        except Exception as write_error:
            prediction_channel_ok = False
            result_msg += f"⚠️ **Canal prédiction** ({PREDICTION_CHANNEL_ID}):\n"
            result_msg += f"   Nom: {pred_title}\n"
            result_msg += f"   Erreur écriture: {str(write_error)[:50]}\n\n"
    except Exception as e:
        prediction_channel_ok = False
        result_msg += f"❌ **Canal prédiction** ({PREDICTION_CHANNEL_ID}):\n"
        result_msg += f"   Erreur: {str(e)[:80]}\n\n"
    
    if source_channel_ok and prediction_channel_ok:
        result_msg += "🎉 **Tout est prêt!** Le bot peut fonctionner normalement."
    else:
        result_msg += "⚠️ **Actions requises** pour que le bot fonctionne correctement."
    
    await event.respond(result_msg)

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    
    logger.info(f"Commande /help reçue de {event.sender_id}")
    
    await event.respond(f"""📖 **Aide - Bot de Prédiction v2.0**

**🎯 Nouveau système de prédiction:**
Le bot prédit maintenant la **couleur de la première carte** du premier groupe!

**Fonctionnement:**
1. Surveille le canal source (tous messages)
2. Extrait la première carte du premier groupe (ex: Q♦️5♥️A♥️ → ♦️)
3. Crée une prédiction pour le jeu **actuel + {prediction_offset}**
4. Format: 🎰 PRÉDICTION #N+{prediction_offset} avec la couleur trouvée

**Exemple:**

**Vérification (sur messages finalisés ✅):**
• ✅0️⃣ GAGNÉ = Couleur trouvée au numéro prédit
• ✅1️⃣ GAGNÉ = Couleur trouvée au numéro+1
• ✅2️⃣ GAGNÉ = Couleur trouvée au numéro+2
• ❌ PERDU = Échec après 3 tentatives → Backup auto

**Commandes admin:**
• `/setoffset <n>` - Changer le décalage (défaut: 2)
• `/status` - Voir les prédictions
• `/checkchannels` - Vérifier les canaux
• `/debug` - Infos système
• `/transfert` - Activer transfert messages
• `/stoptransfert` - Désactiver le transfert

**Paramètre actuel:**
Décalage de prédiction: **+{prediction_offset}** jeux
Modifiable avec `/setoffset 3` (par exemple)""")

# ==================== TRANSFERT COMMANDS ====================

transfer_enabled = True

@client.on(events.NewMessage(pattern='/transfert'))
async def cmd_transfert(event):
    if event.is_group or event.is_channel:
        return
    global transfer_enabled
    transfer_enabled = True
    logger.info(f"Transfert activé par {event.sender_id}")
    await event.respond("✅ Transfert des messages finalisés activé!")

@client.on(events.NewMessage(pattern='/activetransfert'))
async def cmd_active_transfert(event):
    if event.is_group or event.is_channel:
        return
    global transfer_enabled
    transfer_enabled = True
    logger.info(f"Transfert réactivé par {event.sender_id}")
    await event.respond("✅ Transfert réactivé avec succès!")

@client.on(events.NewMessage(pattern='/stoptransfert'))
async def cmd_stop_transfert(event):
    if event.is_group or event.is_channel:
        return
    global transfer_enabled
    transfer_enabled = False
    logger.info(f"Transfert désactivé par {event.sender_id}")
    await event.respond("⛔ Transfert des messages désactivé.")

# ==================== WEB SERVER ====================

async def index(request):
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bot Prédiction Baccarat v2.0</title>
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
        <h1>🎯 Bot de Prédiction Baccarat v2.0</h1>
        <p>Prédiction basée sur la première carte du premier groupe</p>
        
        <div class="status">
            <h3>📊 Statut actuel</h3>
            <div class="metric"><strong>Jeu actuel:</strong> #{current_game_number}</div>
            <div class="metric"><strong>Décalage:</strong> +{prediction_offset} jeux</div>
            <div class="metric"><strong>Prédictions actives:</strong> {len(pending_predictions)}/{MAX_PENDING_PREDICTIONS}</div>
            <div class="metric"><strong>En file d'attente:</strong> {len(queued_predictions)}</div>
        </div>
        
        <ul>
            <li><a href="/health">Health Check</a></li>
            <li><a href="/status">Statut (JSON)</a></li>
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
        "version": "2.0",
        "source_channel": SOURCE_CHANNEL_ID,
        "source_channel_ok": source_channel_ok,
        "prediction_channel": PREDICTION_CHANNEL_ID,
        "prediction_channel_ok": prediction_channel_ok,
        "current_game": current_game_number,
        "prediction_offset": prediction_offset,
        "pending_predictions": len(pending_predictions),
        "max_pending": MAX_PENDING_PREDICTIONS,
        "queued_predictions": len(queued_predictions),
        "recent_games": len(recent_games),
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
        logger.info("Démarrage du bot v2.0...")
        await client.start(bot_token=BOT_TOKEN)
        logger.info("Bot Telegram connecté")
        
        session = client.session.save()
        logger.info(f"Session: {session[:50]}...")
        
        me = await client.get_me()
        username = getattr(me, 'username', 'Unknown')
        logger.info(f"Bot opérationnel: @{username}")
        
        # Vérifier les canaux
        try:
            source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
            source_channel_ok = True
            logger.info(f"✅ Canal source: {getattr(source_entity, 'title', 'N/A')}")
        except Exception as e:
            logger.error(f"❌ Canal source inaccessible: {e}")
        
        try:
            pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
            try:
                test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🤖 Bot v2.0 connecté!")
                await asyncio.sleep(1)
                await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
                prediction_channel_ok = True
                logger.info(f"✅ Canal prédiction: {getattr(pred_entity, 'title', 'N/A')}")
            except Exception as e:
                logger.warning(f"⚠️ Canal prédiction sans droits d'écriture: {e}")
        except Exception as e:
            logger.error(f"❌ Canal prédiction inaccessible: {e}")
        
        logger.info(f"Configuration: OFFSET={prediction_offset}, MAX_PREDICTIONS={MAX_PENDING_PREDICTIONS}")
        return True
        
    except Exception as e:
        logger.error(f"Erreur démarrage: {e}")
        return False

async def main():
    try:
        await start_web_server()
        success = await start_bot()
        if not success:
            logger.error("Échec du démarrage")
            return
        logger.info("Bot v2.0 opérationnel - En attente de messages...")
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Erreur main: {e}")
    finally:
        await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot arrêté")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
