"""
Bot Telegram de prédiction Baccarat - Version 4.1
Prédiction manuelle : le bot s'arrête après chaque finalisation
"""
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

logger.info(f"Configuration: SOURCE_CHANNEL={SOURCE_CHANNEL_ID}, PREDICTION_CHANNEL_ID={PREDICTION_CHANNEL_ID}")
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

MAX_PENDING_PREDICTIONS = 1
PROXIMITY_THRESHOLD = 2

source_channel_ok = False
prediction_channel_ok = False

# ============ VARIABLES GLOBALES ============
transfer_enabled = True
auto_continue = False  # NOUVEAU: Désactivé par défaut - pas de continuation auto

def has_active_unresolved_predictions() -> bool:
    """
    Vérifie s'il y a des prédictions actives non finalisées.
    Retourne True si une prédiction est en cours (⏳ EN COURS).
    """
    for game_num, pred in pending_predictions.items():
        if not pred.get('resolved', False):
            return True
    return False

def get_active_prediction_count() -> int:
    """Retourne le nombre de prédictions actives non résolues"""
    count = 0
    for game_num, pred in pending_predictions.items():
        if not pred.get('resolved', False):
            count += 1
    return count

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message"""
    try:
        match = re.search(r"#N\s*(\d+)\.?", message, re.IGNORECASE)
        if match:
            return int(match.group(1))
    except Exception as e:
        logger.error(f"Erreur extraction numéro: {e}")
    return None

def extract_parentheses_groups(message: str):
    """Extrait le contenu des parenthèses"""
    try:
        return re.findall(r"\(([^)]*)\)", message)
    except Exception as e:
        logger.error(f"Erreur extraction groupes: {e}")
        return []

def normalize_suits(group_str: str) -> str:
    """Normalise les symboles de couleur"""
    try:
        normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
        normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
        return normalized
    except Exception as e:
        logger.error(f"Erreur normalisation: {e}")
        return group_str

def extract_first_card_suit(group_str: str):
    """Extrait la couleur de la première carte du groupe"""
    try:
        normalized = normalize_suits(group_str)
        for char in normalized:
            if char in ALL_SUITS:
                return SUIT_DISPLAY.get(char, char)
    except Exception as e:
        logger.error(f"Erreur extraction carte: {e}")
    return None

def get_suit_full_name(suit_symbol: str) -> str:
    """Retourne le nom complet de la couleur"""
    return SUIT_NAMES.get(suit_symbol, suit_symbol)

def get_alternate_suit(suit: str) -> str:
    """Retourne la couleur alternative (pour backup)"""
    return SUIT_MAPPING.get(suit, suit)

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé (contient ✅ ou 🔰)"""
    try:
        if '⏰' in message:
            return False
        return '✅' in message or '🔰' in message
    except Exception as e:
        logger.error(f"Erreur vérification finalisation: {e}")
        return False

def format_prediction_message(game_number: int, suit: str, status: str = "⏳ EN COURS", result_group: str = None) -> str:
    """Formate le message de prédiction"""
    try:
        suit_name = get_suit_full_name(suit)
        
        if status == "⏳ EN COURS":
            return f"""📡 PRÉDICTION #{game_number}
🎯 Couleur: {suit} {suit_name}
🌪️ Statut: {status}"""
        
        return f"""📡 PRÉDICTION #{game_number}
🎯 Couleur: {suit} {suit_name}
🌪️ Statut: {status}"""
    except Exception as e:
        logger.error(f"Erreur format message: {e}")
        return f"Erreur formatage #{game_number}"

async def send_prediction_to_channel(target_game: int, suit: str, base_game: int):
    """Envoie une prédiction au canal de prédiction immédiatement"""
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
            'last_checked_game': 0,
            'created_at': datetime.now().isoformat(),
            'resolved': False
        }

        logger.info(f"Prédiction active créée: Jeu #{target_game} - {suit} (basé sur #{base_game})")
        return msg_id

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

async def update_prediction_status(game_number: int, new_status: str, result_group: str = None):
    """Met à jour le statut d'une prédiction et la supprime des actives si terminée"""
    try:
        if game_number not in pending_predictions:
            logger.warning(f"⚠️ Prédiction #{game_number} non trouvée pour mise à jour")
            return False

        pred = pending_predictions[game_number]
        message_id = pred['message_id']
        suit = pred['suit']
        
        updated_msg = format_prediction_message(game_number, suit, new_status, result_group)

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
                logger.info(f"✅ Prédiction #{game_number} mise à jour: {new_status}")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour dans le canal: {e}")
        else:
            logger.warning(f"⚠️ Canal non accessible, statut mis à jour en mémoire uniquement")

        pred['status'] = new_status
        pred['resolved'] = True
        logger.info(f"Prédiction #{game_number} statut mis à jour: {new_status}")

        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '✅3️⃣', '❌']:
            if game_number in pending_predictions:
                del pending_predictions[game_number]
                logger.info(f"Prédiction #{game_number} terminée et supprimée")
                logger.info(f"📋 Prédictions restantes: {len(pending_predictions)}")
                logger.info(f"⏹️ BOT EN ATTENTE: Aucune prédiction active - utilisez /predict pour manuel ou attendez message source")

        return True

    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """
    Vérifie si une prédiction est gagnée ou perdue.
    SUPPRESSION de la continuation automatique - le bot s'arrête après chaque résultat.
    """
    try:
        normalized_group = normalize_suits(first_group)
        
        logger.info(f"=== VÉRIFICATION RÉSULTAT ===")
        logger.info(f"Message finalisé reçu: Jeu #{game_number}")
        logger.info(f"Premier groupe analysé: ({first_group})")
        logger.info(f"Prédictions en attente: {list(pending_predictions.keys())}")
        
        predictions_to_check = list(pending_predictions.items())
        
        for pred_game, pred in predictions_to_check:
            try:
                if pred_game not in pending_predictions:
                    continue
                
                if pred.get('resolved', False):
                    logger.info(f"  ⏭️ Prédiction #{pred_game} déjà résolue, ignorée")
                    continue
                    
                target_suit = pred['suit']
                check_count = pred.get('check_count', 0)
                normalized_target = normalize_suits(target_suit)
                
                expected_game = pred_game + check_count
                
                logger.info(f"  → Prédiction #{pred_game}: étape {check_count}, attend #{expected_game}, reçu #{game_number}")
                
                if game_number != expected_game:
                    continue
                
                suit_count = normalized_group.count(normalized_target)
                has_card = suit_count >= 1
                
                logger.info(f"  🔍 VÉRIFICATION #{pred_game}: {target_suit} trouvé {suit_count} fois (condition: ≥1)")
                
                if has_card:
                    # GAGNÉ !
                    status_map = {0: '✅0️⃣', 1: '✅1️⃣', 2: '✅2️⃣', 3: '✅3️⃣'}
                    new_status = status_map.get(check_count, '✅0️⃣')
                    
                    await update_prediction_status(pred_game, new_status, first_group)
                    logger.info(f"  🎉 PRÉDICTION #{pred_game} GAGNÉE! {suit_count}x {target_suit} trouvé | Statut: {new_status}")
                    logger.info(f"  ⏹️ ARRÊT: Le bot attend la prochaine instruction (pas de continuation auto)")
                    
                    # SUPPRESSION: Pas de création automatique après victoire
                    
                else:
                    # PAS trouvé, passer à l'étape suivante
                    new_check_count = check_count + 1
                    
                    if pred_game not in pending_predictions:
                        continue
                        
                    pending_predictions[pred_game]['check_count'] = new_check_count
                    pending_predictions[pred_game]['last_checked_game'] = game_number
                    
                    if new_check_count > 3:
                        # Échec définitif
                        await update_prediction_status(pred_game, '❌', first_group)
                        logger.info(f"  💔 PRÉDICTION #{pred_game} PERDUE après 3 rattrapages")
                        logger.info(f"  ⏹️ ARRÊT: Le bot attend la prochaine instruction (pas de continuation auto)")
                        
                        # SUPPRESSION: Pas de création automatique après défaite
                    else:
                        # Passer au rattrapage suivant
                        rattrapage_txt = {1: '1er', 2: '2ème', 3: '3ème'}.get(new_check_count, f'{new_check_count}ème')
                        logger.info(f"  ⏳ #{pred_game}: Aucune carte {target_suit}, passage au {rattrapage_txt} rattrapage (vérifiera #{pred_game + new_check_count})")
                        
            except Exception as e:
                logger.error(f"  ❌ Erreur traitement prédiction #{pred_game}: {e}")
                import traceback
                logger.error(traceback.format_exc())
                continue
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Erreur globale check_prediction_result: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def create_prediction(target_game: int, suit: str, base_game: int, is_backup: bool = False, is_continuation: bool = False):
    """Crée une nouvelle prédiction UNIQUEMENT si aucune n'est active"""
    try:
        if has_active_unresolved_predictions():
            logger.warning(f"🚫 Impossible de créer prédiction #{target_game}: une prédiction est déjà active")
            logger.info(f"   📋 Prédictions actives: {[g for g, p in pending_predictions.items() if not p.get('resolved', False)]}")
            return False
        
        if target_game in pending_predictions or target_game in queued_predictions:
            logger.info(f"Prédiction #{target_game} déjà existante, ignorée")
            return False
        
        await send_prediction_to_channel(target_game, suit, base_game)
        
        if is_continuation:
            logger.info(f"🔄 Prédiction de continuation créée: #{target_game} après résultat de #{base_game}")
        
        return True
    except Exception as e:
        logger.error(f"Erreur création prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def process_new_message(message_text: str, chat_id: int, is_finalized: bool = False):
    """
    Traite un nouveau message du canal source.
    - CRÉE les prédictions UNIQUEMENT si aucune n'est active ET si c'est un nouveau message (pas une finalisation)
    - VÉRIFIE et FINALISE les résultats UNIQUEMENT si finalisé
    """
    global current_game_number, last_transferred_game
    
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            logger.warning(f"⚠️ Numéro non trouvé dans: {message_text[:50]}...")
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
            logger.warning(f"⚠️ Aucun groupe trouvé dans: {message_text[:50]}...")
            return
        
        first_group = groups[0]
        
        logger.info(f"=" * 60)
        logger.info(f"📨 TRAITEMENT Jeu #{game_number} | Finalisé: {is_finalized}")
        logger.info(f"   Premier groupe: ({first_group})")
        logger.info(f"   🔍 Prédictions actives non résolues: {get_active_prediction_count()}")
        
        # ========== CRÉATION DE PRÉDICTION (UNIQUEMENT SI AUCUNE ACTIVE ET NON FINALISÉ) ==========
        # IMPORTANT: On ne crée une prédiction que sur un message NON finalisé (nouveau jeu)
        # et uniquement si aucune prédiction n'est déjà active
        if not is_finalized:
            try:
                if has_active_unresolved_predictions():
                    logger.info(f"   ⏸️ PRÉDICTION BLOQUÉE: Une prédiction est déjà active et non finalisée")
                    logger.info(f"   📋 En attente: {list(pending_predictions.keys())}")
                else:
                    # Aucune prédiction active - on peut en créer une nouvelle
                    first_card_suit = extract_first_card_suit(first_group)
                    
                    if first_card_suit:
                        target_game = game_number + prediction_offset
                        
                        if target_game not in pending_predictions and target_game not in queued_predictions:
                            success = await create_prediction(target_game, first_card_suit, game_number)
                            if success:
                                logger.info(f"   🎯 NOUVELLE PRÉDICTION: #{target_game} - {first_card_suit} (basé sur #{game_number})")
                                logger.info(f"   ✅ Prédiction créée car aucune autre n'était en attente")
                            else:
                                logger.warning(f"   ⚠️ Échec création prédiction #{target_game}")
                        elif target_game in pending_predictions:
                            logger.info(f"   ⏭️ Prédiction #{target_game} existe déjà")
                    else:
                        logger.warning(f"   ⚠️ Impossible d'extraire la couleur de: ({first_group})")
                        
            except Exception as e:
                logger.error(f"   ❌ Erreur création prédiction: {e}")
                import traceback
                logger.error(traceback.format_exc())
        else:
            logger.info(f"   ⏭️ Message finalisé - pas de création de prédiction (attente de finalisation d'abord)")
        
        # ========== VÉRIFICATION ET FINALISATION (UNIQUEMENT SI FINALISÉ) ==========
        if is_finalized:
            finalized_hash = f"finalized_{game_number}"
            if finalized_hash not in processed_finalized:
                processed_finalized.add(finalized_hash)
                
                # Transfert du message si activé
                if transfer_enabled and ADMIN_ID and ADMIN_ID != 0 and last_transferred_game != game_number:
                    try:
                        transfer_msg = f"📨 **Message finalisé:**\n\n{message_text}"
                        await client.send_message(ADMIN_ID, transfer_msg)
                        last_transferred_game = game_number
                        logger.info(f"   📤 Message transféré à l'admin")
                    except Exception as e:
                        logger.error(f"   ❌ Erreur transfert: {e}")
                
                # Vérifier et finaliser les résultats
                try:
                    logger.info(f"   ✅ MESSAGE FINALISÉ - Vérification du premier groupe...")
                    await check_prediction_result(game_number, first_group)
                except Exception as e:
                    logger.error(f"   ❌ Erreur vérification: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                
                if len(processed_finalized) > 100:
                    processed_finalized.clear()
        
        # Stocker le jeu pour référence
        try:
            recent_games[game_number] = {
                'first_group': first_group,
                'timestamp': datetime.now().isoformat()
            }
            
            if len(recent_games) > 100:
                oldest = min(recent_games.keys())
                del recent_games[oldest]
        except Exception as e:
            logger.error(f"   ❌ Erreur stockage jeu: {e}")
            
    except Exception as e:
        logger.error(f"❌ Erreur globale process_new_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# ==================== EVENT HANDLERS ====================

@client.on(events.NewMessage())
async def handle_message(event):
    """Gère les nouveaux messages"""
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id
        
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"📥 Message reçu: {message_text[:80]}...")
            
            is_finalized = is_message_finalized(message_text)
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
            logger.info(f"✏️ Message édité: {message_text[:80]}...")
            
            is_finalized = is_message_finalized(message_text)
            
            if is_finalized:
                logger.info(f"✅ Finalisé - Vérification")
                await process_new_message(message_text, chat_id, is_finalized=True)
            else:
                logger.info(f"⏳ Pas encore finalisé")
            
    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# ==================== COMMANDES ADMIN ====================

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    
    try:
        await event.respond("""🤖 **Bot de Prédiction Baccarat - v4.1**

📡 PRÉDICTION #N
🎯 Couleur: [suit] [nom]
🌪️ Statut: ⏳ EN COURS

**NOUVEAUTÉ v4.1 - Prédiction Manuelle:**
• Une seule prédiction active à la fois
• Le bot s'ARRÊTE après chaque finalisation
• Utilisez `/predict` pour forcer une nouvelle prédiction
• Ou attendez un nouveau message du canal source

**Condition de victoire: AU MOINS 1 carte dans le premier groupe**

**Système de rattrapage:**
• ✅0️⃣ = Gagné au numéro prédit (N)
• ✅1️⃣ = Gagné au 1er rattrapage (N+1)
• ✅2️⃣ = Gagné au 2ème rattrapage (N+2)
• ✅3️⃣ = Gagné au 3ème rattrapage (N+3)
• ❌ = Perdu (après 3 rattrapages)

**Commandes:**
• `/predict` - Forcer une nouvelle prédiction manuelle
• `/status` - Voir les prédictions
• `/setoffset <n>` - Changer le décalage
• `/help` - Aide détaillée""")
    except Exception as e:
        logger.error(f"Erreur cmd_start: {e}")

@client.on(events.NewMessage(pattern='/predict'))
async def cmd_predict(event):
    """Commande manuelle pour forcer une prédiction"""
    if event.is_group or event.is_channel:
        return
    
    try:
        if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
            await event.respond("⛔ Réservé admin")
            return
        
        # Vérifier si une prédiction est déjà active
        if has_active_unresolved_predictions():
            active_games = [g for g, p in pending_predictions.items() if not p.get('resolved', False)]
            await event.respond(f"⛔ Impossible: prédiction(s) active(s) en cours: {active_games}\nAttendez la finalisation ou utilisez /forceclear")
            return
        
        # Créer une prédiction basée sur le dernier jeu connu
        if current_game_number == 0:
            await event.respond("❌ Aucun jeu connu. Attendez un message du canal source d'abord.")
            return
        
        # Récupérer le dernier groupe connu
        last_game = recent_games.get(current_game_number, {})
        first_group = last_game.get('first_group', '')
        
        if not first_group:
            await event.respond(f"❌ Pas d'information sur le jeu #{current_game_number}. Attendez un message.")
            return
        
        first_card_suit = extract_first_card_suit(first_group)
        if not first_card_suit:
            await event.respond(f"❌ Impossible d'extraire la couleur du dernier groupe: ({first_group})")
            return
        
        target_game = current_game_number + prediction_offset
        
        if target_game in pending_predictions:
            await event.respond(f"⛔ Prédiction #{target_game} existe déjà")
            return
        
        success = await create_prediction(target_game, first_card_suit, current_game_number)
        if success:
            await event.respond(f"""✅ **PRÉDICTION MANUELLE CRÉÉE**

📡 PRÉDICTION #{target_game}
🎯 Couleur: {first_card_suit} {get_suit_full_name(first_card_suit)}
🌪️ Statut: ⏳ EN COURS

Basé sur le jeu #{current_game_number}""")
        else:
            await event.respond("❌ Échec création prédiction. Vérifiez les logs.")
            
    except Exception as e:
        logger.error(f"Erreur cmd_predict: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await event.respond(f"❌ Erreur: {str(e)}")

@client.on(events.NewMessage(pattern='/forceclear'))
async def cmd_forceclear(event):
    """Force la suppression de toutes les prédictions (en cas de blocage)"""
    if event.is_group or event.is_channel:
        return
    
    try:
        if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
            await event.respond("⛔ Réservé admin")
            return
        
        global pending_predictions
        count = len(pending_predictions)
        pending_predictions.clear()
        await event.respond(f"🧹 **FORCÉ:** {count} prédiction(s) supprimée(s). Le bot peut maintenant créer une nouvelle prédiction.")
        
    except Exception as e:
        logger.error(f"Erreur forceclear: {e}")
        await event.respond("❌ Erreur")

@client.on(events.NewMessage(pattern='/setoffset'))
async def cmd_setoffset(event):
    if event.is_group or event.is_channel:
        return
    
    try:
        if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
            await event.respond("⛔ Réservé admin")
            return
        
        global prediction_offset
        
        text = event.message.message
        parts = text.split()
        
        if len(parts) < 2:
            await event.respond(f"Usage: `/setoffset <n>`\nActuel: **{prediction_offset}**")
            return
        
        new_offset = int(parts[1])
        
        if new_offset < 1 or new_offset > 50:
            await event.respond("Décalage: 1-50")
            return
        
        prediction_offset = new_offset
        await event.respond(f"✅ Décalage: **+{prediction_offset}**")
        
    except ValueError:
        await event.respond("Entrez un nombre valide")
    except Exception as e:
        logger.error(f"Erreur setoffset: {e}")
        await event.respond(f"❌ Erreur")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    
    try:
        if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
            await event.respond("⛔ Réservé admin")
            return
        
        active_count = get_active_prediction_count()
        
        status_msg = f"📊 **État v4.1:**\n\n"
        status_msg += f"🎮 Dernier jeu: #{current_game_number}\n"
        status_msg += f"📏 Décalage: +{prediction_offset}\n"
        status_msg += f"🎯 Condition: ≥1 carte dans 1er groupe\n"
        status_msg += f"🔁 Rattrapages: 3 maximum (N+1, N+2, N+3)\n"
        status_msg += f"🔒 Mode: Manuel (arrêt après chaque résultat)\n\n"
        
        if pending_predictions:
            status_msg += f"**🔮 Active ({active_count}):**\n"
            for game_num, pred in sorted(pending_predictions.items()):
                try:
                    suit_name = get_suit_full_name(pred['suit'])
                    etape = pred.get('check_count', 0)
                    if etape == 0:
                        etape_txt = "N (prédit)"
                    elif etape == 1:
                        etape_txt = "1er rattrapage (N+1)"
                    elif etape == 2:
                        etape_txt = "2ème rattrapage (N+2)"
                    elif etape == 3:
                        etape_txt = "3ème rattrapage (N+3)"
                    else:
                        etape_txt = f"Étape {etape}"
                    resolved = "✓ Résolue" if pred.get('resolved', False) else "⏳ EN COURS"
                    status_msg += f"• #{game_num}: {pred['suit']} {suit_name}\n  → {etape_txt} | {resolved}\n"
                except Exception as e:
                    status_msg += f"• #{game_num}: Erreur affichage\n"
        else:
            status_msg += "**🔮 Aucune prédiction active**\n"
            status_msg += "✅ Prêt pour nouvelle prédiction\n"
            status_msg += "💡 Utilisez `/predict` pour manuel ou attendez message source\n"
        
        await event.respond(status_msg)
    except Exception as e:
        logger.error(f"Erreur status: {e}")
        await event.respond("❌ Erreur affichage status")

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    
    try:
        await event.respond(f"""📖 **Aide v4.1 - Mode Manuel**

**Format:**
📡 PRÉDICTION #N
🎯 Couleur: [suit] [nom]
🌪️ Statut: [statut]

**Fonctionnement v4.1:**
1. Le bot crée UNE SEULE prédiction à la fois
2. Il attend que cette prédiction soit finalisée (✅ ou ❌)
3. **S'ARRÊTE** - ne crée pas de nouvelle prédiction automatiquement
4. Pour continuer:
   • `/predict` - Crée manuellement une prédiction sur le dernier jeu connu
   • Ou attendez un nouveau message non finalisé du canal source

**Déroulement:**
• Prédiction créée pour le jeu #N
• Attente de la finalisation de #N dans le canal source
• Vérification: ≥1 carte de la couleur prédite ?
• Si OUI → ✅X et **ARRÊT**
• Si NON → rattrapage sur #N+1, #N+2, #N+3
• Si toujours NON après 3 rattrapages → ❌ et **ARRÊT**

**Commandes spéciales:**
• `/predict` - Force une prédiction manuelle
• `/forceclear` - Supprime toutes les prédictions (si bloqué)
• `/status` - Voir l'état actuel

**Décalage actuel:** +{prediction_offset}""")
    except Exception as e:
        logger.error(f"Erreur help: {e}")

# ==================== TRANSFERT COMMANDS ====================

@client.on(events.NewMessage(pattern='/transfert'))
async def cmd_transfert(event):
    if event.is_group or event.is_channel:
        return
    try:
        global transfer_enabled
        transfer_enabled = True
        await event.respond("✅ Transfert ON")
    except Exception as e:
        logger.error(f"Erreur transfert: {e}")

@client.on(events.NewMessage(pattern='/stoptransfert'))
async def cmd_stop_transfert(event):
    if event.is_group or event.is_channel:
        return
    try:
        global transfer_enabled
        transfer_enabled = False
        await event.respond("⛔ Transfert OFF")
    except Exception as e:
        logger.error(f"Erreur stop transfert: {e}")

# ==================== WEB SERVER ====================

async def index(request):
    try:
        active_count = get_active_prediction_count()
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Bot Baccarat v4.1</title>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial; margin: 40px; background: #1a1a2e; color: #eee; }}
                h1 {{ color: #00d4ff; }}
                .status {{ background: #16213e; padding: 20px; border-radius: 10px; margin: 20px 0; }}
                .feature {{ color: #00ff88; font-weight: bold; }}
                .warning {{ color: #ff4444; font-weight: bold; }}
            </style>
        </head>
        <body>
            <h1>📡 Bot Baccarat v4.1</h1>
            <div class="status">
                <div><strong>Dernier jeu:</strong> #{current_game_number}</div>
                <div><strong>Décalage:</strong> +{prediction_offset}</div>
                <div><strong>Actives:</strong> {active_count}</div>
                <div><strong>Règle:</strong> ≥1 carte, 3 rattrapages max</div>
                <div class="warning">⏹️ MODE MANUEL: Arrêt après chaque résultat</div>
                <div class="feature">💡 Utilisez /predict pour continuer</div>
            </div>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html', status=200)
    except Exception as e:
        logger.error(f"Erreur index: {e}")
        return web.Response(text="Error", status=500)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    try:
        app = web.Application()
        app.router.add_get('/', index)
        app.router.add_get('/health', health_check)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logger.info(f"Web server: 0.0.0.0:{PORT}")
    except Exception as e:
        logger.error(f"Erreur web server: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def start_bot():
    global source_channel_ok, prediction_channel_ok
    try:
        logger.info("🚀 Démarrage v4.1...")
        logger.info("🎯 Condition: ≥1 carte dans le premier groupe")
        logger.info("🔒 MODE MANUEL: Une seule prédiction, arrêt après résultat")
        logger.info("⏹️ PAS DE CONTINUATION AUTOMATIQUE")
        await client.start(bot_token=BOT_TOKEN)
        logger.info("✅ Bot connecté")
        
        me = await client.get_me()
        logger.info(f"Bot: @{getattr(me, 'username', 'Unknown')}")
        
        try:
            source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
            source_channel_ok = True
            logger.info(f"✅ Source: {getattr(source_entity, 'title', 'N/A')}")
        except Exception as e:
            logger.error(f"❌ Source: {e}")
        
        try:
            pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
            try:
                test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🤖 v4.1 connecté! Mode manuel - Le bot s'arrête après chaque résultat. Utilisez /predict pour continuer.")
                await asyncio.sleep(1)
                await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
                prediction_channel_ok = True
                logger.info(f"✅ Prédiction: {getattr(pred_entity, 'title', 'N/A')}")
            except Exception as e:
                logger.warning(f"⚠️ Prédiction lecture seule: {e}")
        except Exception as e:
            logger.error(f"❌ Prédiction: {e}")
        
        logger.info(f"⚙️ OFFSET=+{prediction_offset}")
        logger.info("🔁 Rattrapages: N+1, N+2, N+3 (3 max)")
        logger.info("⏹️ ARRÊT: Pas de création auto après résultat")
        logger.info("💡 COMMANDE: /predict pour manuel")
        return True
        
    except Exception as e:
        logger.error(f"Erreur start_bot: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def main():
    """Boucle principale avec reconnexion automatique"""
    restart_delay = 10
    
    while True:
        try:
            await start_web_server()
            success = await start_bot()
            
            if not success:
                logger.error(f"Échec démarrage, nouvelle tentative dans {restart_delay}s...")
                await asyncio.sleep(restart_delay)
                continue
            
            logger.info("🤖 Bot opérationnel! En attente de messages...")
            await client.run_until_disconnected()
            logger.warning("⚠️ Client déconnecté, reconnexion...")
            
        except KeyboardInterrupt:
            logger.info("🛑 Arrêt demandé par l'utilisateur")
            break
            
        except Exception as e:
            logger.error(f"💥 Erreur fatale: {e}")
            import traceback
            logger.error(traceback.format_exc())
            logger.info(f"🔄 Redémarrage dans {restart_delay} secondes...")
            
        finally:
            try:
                await client.disconnect()
            except:
                pass
                
        await asyncio.sleep(restart_delay)
    
    logger.info("👋 Bot arrêté définitivement")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêt")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        import traceback
        logger.error(traceback.format_exc())
