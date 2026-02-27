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
    PREDICTION_OFFSET, SUIT_MAPPING, ALL_SUITS, SUIT_DISPLAY, SUIT_NAMES,
    RESTART_TIMEOUT_MINUTES, MAX_GAME_NUMBER, PREDICTION_GAP, MAX_PENDING_PREDICTIONS,  # AJOUTÉ
    AUTO_PREDICTION_ENABLED, AUTO_RESTART_ON_TIMEOUT, AUTO_RESTART_ON_MAX_GAME, BOT_MODE,  # AJOUTÉ
    PROXIMITY_THRESHOLD  # AJOUTÉ
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
logger.info(f"Mode: {BOT_MODE}, Auto-prediction: {AUTO_PREDICTION_ENABLED}")

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

source_channel_ok = False
prediction_channel_ok = False

# ============ VARIABLES GLOBALES ============
transfer_enabled = True

# ============ VARIABLES POUR GESTION DES PRÉDICTIONS ============
prediction_in_progress = False
last_prediction_time = None
last_prediction_number = None
restart_task = None

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

async def reset_restart_timer():
    """Réinitialise le timer de redémarrage automatique"""
    global restart_task, last_prediction_time
    
    if not AUTO_RESTART_ON_TIMEOUT:
        return
        
    last_prediction_time = datetime.now()
    
    if restart_task and not restart_task.done():
        restart_task.cancel()
        try:
            await restart_task
        except asyncio.CancelledError:
            pass
    
    restart_task = asyncio.create_task(restart_after_timeout())

async def restart_after_timeout():
    """Tâche qui redémarre le bot après un timeout d'inactivité"""
    try:
        if not AUTO_RESTART_ON_TIMEOUT:
            logger.info("⏱️ Redémarrage auto sur timeout désactivé")
            return
            
        timeout_seconds = RESTART_TIMEOUT_MINUTES * 60
        logger.info(f"⏱️ Timer de redémarrage démarré ({RESTART_TIMEOUT_MINUTES} minutes)")
        
        await asyncio.sleep(timeout_seconds)
        
        logger.warning(f"⏰ TIMEOUT: Aucune prédiction depuis {RESTART_TIMEOUT_MINUTES} minutes")
        logger.warning("🔄 Redémarrage automatique du bot...")
        
        await client.disconnect()
        
    except asyncio.CancelledError:
        logger.info("⏱️ Timer de redémarrage réinitialisé")
    except Exception as e:
        logger.error(f"Erreur dans le timer de redémarrage: {e}")

async def send_prediction_to_channel(target_game: int, suit: str, base_game: int):
    """Envoie une prédiction au canal de prédiction immédiatement"""
    global prediction_in_progress, last_prediction_time, last_prediction_number
    
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

        prediction_in_progress = True
        last_prediction_time = datetime.now()
        last_prediction_number = target_game
        
        await reset_restart_timer()

        pending_predictions[target_game] = {
            'message_id': msg_id,
            'suit': suit,
            'base_game': base_game,
            'status': '⏳ EN COURS',
            'check_stage': 0,
            'created_at': datetime.now().isoformat()
        }

        logger.info(f"Prédiction active créée: Jeu #{target_game} - {suit} (basé sur #{base_game})")
        logger.info(f"🔒 Nouvelles prédictions BLOQUÉES jusqu'à finalisation de #{target_game}")
        return msg_id

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

async def update_prediction_status(game_number: int, new_status: str, result_group: str = None):
    """Met à jour le statut d'une prédiction et la supprime des actives si terminée"""
    global prediction_in_progress
    
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
        logger.info(f"Prédiction #{game_number} statut mis à jour: {new_status}")

        if new_status in ['🍯✅0️⃣', '🍯✅1️⃣', '🍯✅2️⃣', '🍯✅3️⃣', '😶❌']:
            if game_number in pending_predictions:
                del pending_predictions[game_number]
                logger.info(f"Prédiction #{game_number} terminée et supprimée")
            
            prediction_in_progress = False
            logger.info(f"🔓 Prédiction finalisée! Nouvelles prédictions DÉBLOQUÉES")
            logger.info(f"📋 Prochaine prédiction possible dans +{PREDICTION_GAP} numéros")

        return True

    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """Vérifie si une prédiction est gagnée ou perdue"""
    try:
        normalized_group = normalize_suits(first_group)
        
        logger.info(f"=== VÉRIFICATION RÉSULTAT Jeu #{game_number} ===")
        logger.info(f"Premier groupe analysé: ({first_group})")
        logger.info(f"Prédictions en attente: {list(pending_predictions.keys())}")
        
        predictions_to_check = list(pending_predictions.items())
        found_winner = False
        
        for pred_game, pred in predictions_to_check:
            try:
                if pred_game not in pending_predictions:
                    continue
                
                target_suit = pred['suit']
                check_stage = pred.get('check_stage', 0)
                normalized_target = normalize_suits(target_suit)
                
                expected_game = pred_game + check_stage
                
                logger.info(f"  → Prédiction #{pred_game}: stage={check_stage}, attend #{expected_game}, reçu #{game_number}, couleur={target_suit}")
                
                if game_number != expected_game:
                    logger.info(f"  ⏭️ Numéro ne correspond pas (attendu #{expected_game}), ignoré")
                    continue
                
                suit_count = normalized_group.count(normalized_target)
                has_card = suit_count >= 1
                
                logger.info(f"  🔍 VÉRIFICATION #{pred_game} Stage {check_stage}: {target_suit} trouvé {suit_count} fois (condition: ≥1)")
                
                if has_card:
                    status_map = {0: '🍯✅0️⃣', 1: '🍯✅1️⃣', 2: '🍯✅2️⃣', 3: '🍯✅3️⃣'}
                    new_status = status_map.get(check_stage, '🍯✅0️⃣')
                    
                    await update_prediction_status(pred_game, new_status, first_group)
                    logger.info(f"  🎉 PRÉDICTION #{pred_game} GAGNÉE au stage {check_stage}! {suit_count}x {target_suit} | Statut: {new_status}")
                    found_winner = True
                    
                else:
                    new_stage = check_stage + 1
                    
                    if pred_game not in pending_predictions:
                        continue
                    
                    pending_predictions[pred_game]['check_stage'] = new_stage
                    
                    if new_stage > 3:
                        await update_prediction_status(pred_game, '😶❌', first_group)
                        logger.info(f"  💔 PRÉDICTION #{pred_game} PERDUE après 4 tentatives (N à N+3)")
                        
                        suit = pred['suit']
                        backup_game = pred_game + prediction_offset
                        alternate_suit = get_alternate_suit(suit)
                        await create_prediction(backup_game, alternate_suit, pred_game, is_backup=True)
                    else:
                        stage_names = {1: '1er rattrapage (N+1)', 2: '2ème rattrapage (N+2)', 3: '3ème rattrapage (N+3)'}
                        stage_txt = stage_names.get(new_stage, f'Stage {new_stage}')
                        next_game = pred_game + new_stage
                        logger.info(f"  ⏳ #{pred_game}: Aucune carte {target_suit}, passage au {stage_txt} (prochaine vérif: #{next_game})")
                        
            except Exception as e:
                logger.error(f"  ❌ Erreur traitement prédiction #{pred_game}: {e}")
                import traceback
                logger.error(traceback.format_exc())
                continue
        
        return found_winner
        
    except Exception as e:
        logger.error(f"❌ Erreur globale check_prediction_result: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def create_prediction(target_game: int, suit: str, base_game: int, is_backup: bool = False):
    """Crée une nouvelle prédiction"""
    try:
        if target_game in pending_predictions or target_game in queued_predictions:
            logger.info(f"Prédiction #{target_game} déjà existante, ignorée")
            return False
        
        await send_prediction_to_channel(target_game, suit, base_game)
        return True
    except Exception as e:
        logger.error(f"Erreur création prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def process_new_message(message_text: str, chat_id: int, is_finalized: bool = False):
    """Traite un nouveau message du canal source"""
    global current_game_number, last_transferred_game, last_prediction_number
    
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            logger.warning(f"⚠️ Numéro non trouvé dans: {message_text[:50]}...")
            return
        
        current_game_number = game_number
        
        # VÉRIFICATION REDÉMARRAGE JEU #1440
        if AUTO_RESTART_ON_MAX_GAME and game_number >= MAX_GAME_NUMBER:
            logger.warning(f"🎰 Jeu #{game_number} atteint (limite: {MAX_GAME_NUMBER}) - Redémarrage forcé")
            await client.disconnect()
            return
        
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
        logger.info(f"   🔒 Prédiction en cours: {prediction_in_progress} | Dernière: #{last_prediction_number}")
        logger.info(f"   Mode: {BOT_MODE} | Auto-prediction: {AUTO_PREDICTION_ENABLED}")
        
        # VÉRIFICATION MODE MANUEL
        skip_prediction_creation = False
        if BOT_MODE == "manual" or not AUTO_PREDICTION_ENABLED:
            logger.info(f"   ⛔ MODE MANUEL: Prédictions automatiques désactivées")
            skip_prediction_creation = True
        
        # ========== CRÉATION DE PRÉDICTION ==========
        if not skip_prediction_creation:
            try:
                first_card_suit = extract_first_card_suit(first_group)
                
                if first_card_suit:
                    target_game = game_number + prediction_offset
                    
                    if prediction_in_progress:
                        logger.info(f"   ⛔ BLOQUÉ: Prédiction en cours (attente finalisation)")
                    
                    elif last_prediction_number is not None:
                        gap_needed = PREDICTION_GAP
                        last_base_game = last_prediction_number - prediction_offset
                        games_since_last = game_number - last_base_game
                        
                        if games_since_last < gap_needed:
                            logger.info(f"   ⛔ BLOQUÉ: Gap insuffisant ({games_since_last}/{gap_needed})")
                        else:
                            if target_game not in pending_predictions and len(pending_predictions) < MAX_PENDING_PREDICTIONS:
                                await create_prediction(target_game, first_card_suit, game_number)
                                logger.info(f"   🎯 NOUVELLE PRÉDICTION: #{target_game} - {first_card_suit}")
                            elif target_game in pending_predictions:
                                logger.info(f"   ⏭️ Prédiction #{target_game} existe déjà")
                            else:
                                logger.info(f"   ⏸️ Max prédictions atteint ({MAX_PENDING_PREDICTIONS})")
                    else:
                        if target_game not in pending_predictions and len(pending_predictions) < MAX_PENDING_PREDICTIONS:
                            await create_prediction(target_game, first_card_suit, game_number)
                            logger.info(f"   🎯 PREMIÈRE PRÉDICTION: #{target_game} - {first_card_suit}")
                        elif target_game in pending_predictions:
                            logger.info(f"   ⏭️ Prédiction #{target_game} existe déjà")
                        else:
                            logger.info(f"   ⏸️ Max prédictions atteint ({MAX_PENDING_PREDICTIONS})")
                else:
                    logger.warning(f"   ⚠️ Impossible d'extraire la couleur de: ({first_group})")
            except Exception as e:
                logger.error(f"   ❌ Erreur création prédiction: {e}")
                import traceback
                logger.error(traceback.format_exc())
        
        # ========== VÉRIFICATION ET FINALISATION ==========
        if is_finalized:
            finalized_hash = f"finalized_{game_number}"
            if finalized_hash not in processed_finalized:
                processed_finalized.add(finalized_hash)
                
                if transfer_enabled and ADMIN_ID and ADMIN_ID != 0 and last_transferred_game != game_number:
                    try:
                        transfer_msg = f"📨 **Message finalisé:**\n\n{message_text}"
                        await client.send_message(ADMIN_ID, transfer_msg)
                        last_transferred_game = game_number
                        logger.info(f"   📤 Message transféré à l'admin")
                    except Exception as e:
                        logger.error(f"   ❌ Erreur transfert: {e}")
                
                try:
                    logger.info(f"   ✅ MESSAGE FINALISÉ - Vérification du premier groupe...")
                    await check_prediction_result(game_number, first_group)
                except Exception as e:
                    logger.error(f"   ❌ Erreur vérification: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                
                if len(processed_finalized) > 100:
                    processed_finalized.clear()
        else:
            logger.info(f"   ⏳ Message non finalisé, pas de vérification")
        
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
    """Gère les nouveaux messages - PRÉDICTION IMMÉDIATE"""
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
        mode_status = "🤖 AUTO" if BOT_MODE == "auto" and AUTO_PREDICTION_ENABLED else "👤 MANUEL"
        await event.respond(f"""🤖 **Bot de Prédiction Baccarat - v4.1**

📡 PRÉDICTION #74
🎯 Couleur: ❤️ Cœur
🌪️ Statut: ⏳ EN COURS

**Mode actuel: {mode_status}**

**🆕 NOUVEAUTÉS v4.1:**
• 🔒 Une seule prédiction à la fois (attente finalisation)
• 📏 Gap de +{PREDICTION_GAP} numéros obligatoire entre prédictions
• ⏰ Redémarrage auto après {RESTART_TIMEOUT_MINUTES}min d'inactivité
• 🎰 Redémarrage auto au jeu #{MAX_GAME_NUMBER}

**Condition de victoire:** AU MOINS 1 carte dans le premier groupe

**Système de rattrapage:**
• 🍯✅0️⃣ = Gagné au numéro prédit (N)
• 🍯✅1️⃣ = Gagné au 1er rattrapage (N+1)
• 🍯✅2️⃣ = Gagné au 2ème rattrapage (N+2)
• 🍯✅3️⃣ = Gagné au 3ème rattrapage (N+3)
• 😶❌ = Perdu (après 3 rattrapages)

**Commandes:**
• `/status` - Voir les prédictions et état du système
• `/setoffset <n>` - Changer le décalage
• `/forceunlock` - Débloquer manuellement (admin)
• `/toggle` - Basculer mode auto/manuel (admin)
• `/help` - Aide détaillée""")
    except Exception as e:
        logger.error(f"Erreur cmd_start: {e}")

@client.on(events.NewMessage(pattern='/toggle'))
async def cmd_toggle(event):
    """Bascule entre mode auto et manuel"""
    if event.is_group or event.is_channel:
        return
    
    try:
        if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
            await event.respond("⛔ Réservé admin")
            return
        
        global AUTO_PREDICTION_ENABLED
        
        AUTO_PREDICTION_ENABLED = not AUTO_PREDICTION_ENABLED
        mode = "🤖 AUTO" if AUTO_PREDICTION_ENABLED else "👤 MANUEL"
        
        await event.respond(f"✅ Mode changé: **{mode}**\n\nLes nouvelles prédictions sont maintenant {'activées' if AUTO_PREDICTION_ENABLED else 'désactivées'}.")
        logger.warning(f"🔄 Mode changé par admin: {mode}")
        
    except Exception as e:
        logger.error(f"Erreur toggle: {e}")
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
        
        mode_status = "🤖 AUTO" if BOT_MODE == "auto" and AUTO_PREDICTION_ENABLED else "👤 MANUEL"
        
        status_msg = f"📊 **État du Système v4.1:**\n\n"
        status_msg += f"🎮 Jeu actuel: #{current_game_number}\n"
        status_msg += f"📏 Décalage: +{prediction_offset}\n"
        status_msg += f"🔒 Prédiction en cours: {'OUI' if prediction_in_progress else 'NON'}\n"
        status_msg += f"⚙️ Mode: {mode_status}\n"
        
        if last_prediction_number:
            status_msg += f"🎯 Dernière prédiction: #{last_prediction_number}\n"
        
        if last_prediction_time:
            elapsed = datetime.now() - last_prediction_time
            minutes = elapsed.total_seconds() / 60
            status_msg += f"⏱️ Dernière activité: {minutes:.1f}min ago\n"
            if AUTO_RESTART_ON_TIMEOUT:
                status_msg += f"⏰ Redémarrage auto dans: {max(0, RESTART_TIMEOUT_MINUTES - minutes):.1f}min\n"
        
        status_msg += f"\n🎯 Condition: ≥1 carte dans 1er groupe\n"
        status_msg += f"🔁 Rattrapages: 3 max (N+1, N+2, N+3)\n"
        status_msg += f"📋 Gap requis: +{PREDICTION_GAP} numéros\n"
        status_msg += f"🎰 Max jeu: #{MAX_GAME_NUMBER}\n\n"
        
        if pending_predictions:
            status_msg += f"**🔮 Prédictions Actives ({len(pending_predictions)}):**\n"
            for game_num, pred in sorted(pending_predictions.items()):
                try:
                    suit_name = get_suit_full_name(pred['suit'])
                    stage = pred.get('check_stage', 0)
                    expected_num = game_num + stage
                    
                    if stage == 0:
                        stage_txt = f"Attente #{game_num} (prédit)"
                    elif stage == 1:
                        stage_txt = f"Attente #{expected_num} (1er rattrapage)"
                    elif stage == 2:
                        stage_txt = f"Attente #{expected_num} (2ème rattrapage)"
                    elif stage == 3:
                        stage_txt = f"Attente #{expected_num} (3ème rattrapage)"
                    else:
                        stage_txt = f"Stage {stage}"
                    
                    status_msg += f"• #{game_num}: {pred['suit']} {suit_name}\n  → {stage_txt} | {pred['status']}\n"
                except Exception as e:
                    status_msg += f"• #{game_num}: Erreur affichage\n"
        else:
            status_msg += "**🔮 Aucune prédiction active**\n"
            if not prediction_in_progress:
                status_msg += "\n✅ Système prêt pour nouvelle prédiction"
        
        await event.respond(status_msg)
    except Exception as e:
        logger.error(f"Erreur status: {e}")
        await event.respond("❌ Erreur affichage status")

@client.on(events.NewMessage(pattern='/forceunlock'))
async def cmd_force_unlock(event):
    """Commande admin pour débloquer manuellement le système"""
    if event.is_group or event.is_channel:
        return
    
    try:
        if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
            await event.respond("⛔ Réservé admin")
            return
        
        global prediction_in_progress, pending_predictions
        
        prediction_in_progress = False
        pending_predictions.clear()
        
        await event.respond("""🔓 **SYSTÈME DÉBLOQUÉ MANUELLEMENT**

⚠️ Toutes les prédictions ont été effacées.
Le système est prêt pour une nouvelle prédiction.

État actuel:
• 🔒 Prédiction en cours: NON
• 🔮 Prédictions actives: 0
• ✅ Nouvelles prédictions: AUTORISÉES""")
        
        logger.warning(f"🔓 SYSTÈME DÉBLOQUÉ MANUELLEMENT par admin {event.sender_id}")
        
    except Exception as e:
        logger.error(f"Erreur force unlock: {e}")
        await event.respond("❌ Erreur")

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    
    try:
        mode_status = "🤖 AUTO" if BOT_MODE == "auto" and AUTO_PREDICTION_ENABLED else "👤 MANUEL"
        
        await event.respond(f"""📖 **Aide v4.1 - Système de Prédiction**

**Mode actuel: {mode_status}**

**🆕 GESTION DES PRÉDICTIONS:**
• 🔒 **Une seule prédiction à la fois** - Attendre la finalisation avant nouvelle prédiction
• 📏 **Gap de +{PREDICTION_GAP} numéros** - Après prédiction #N (basée sur #X), prochaine sur #X+{PREDICTION_GAP}
• ⏰ **Redémarrage auto** - Après {RESTART_TIMEOUT_MINUTES} minutes sans activité
• 🎰 **Redémarrage auto** - Au jeu #{MAX_GAME_NUMBER}

**Système de rattrapage:**
• 🍯✅0️⃣ = Trouvé au numéro prédit (N)
• 🍯✅1️⃣ = Trouvé au 1er rattrapage (N+1)
• 🍯✅2️⃣ = Trouvé au 2ème rattrapage (N+2)
• 🍯✅3️⃣ = Trouvé au 3ème rattrapage (N+3)
• 😶❌ = Perdu (après 3 rattrapages)

**Commandes admin:**
• `/status` - État complet du système
• `/toggle` - Basculer mode AUTO/MANUEL
• `/forceunlock` - Débloquer en cas de problème
• `/setoffset <n>` - Changer décalage (défaut: {PREDICTION_OFFSET})

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
async def cmd_stoptransfert(event):
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
        mode_status = "AUTO" if BOT_MODE == "auto" and AUTO_PREDICTION_ENABLED else "MANUEL"
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Bot Baccarat v4.1</title>
            <meta charset="utf-8• `/toggle` - Basculer mode AUTO/MANUEL
• `/forceunlock` - Débloquer en cas de problème
• `/setoffset <n>` - Changer décalage (défaut: {PREDICTION_OFFSET})

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
async def cmd_stoptransfert(event):
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
        mode_status = "AUTO" if BOT_MODE == "auto" and AUTO_PREDICTION_ENABLED else "MANUEL"
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
                .locked {{ color: #ff6b6b; }}
                .unlocked {{ color: #51cf66; }}
                .mode {{ color: #ffd43b; }}
            </style>
        </head>
        <body>
            <h1>📡 Bot Baccarat v4.1</h1>
            <div class="status">
                <div><strong>Jeu:</strong> #{current_game_number}</div>
                <div><strong>Décalage:</strong> +{prediction_offset}</div>
                <div class="mode"><strong>Mode:</strong> {mode_status}</div>
                <div class="{'locked' if prediction_in_progress else 'unlocked'}">
                    <strong>État:</strong> {'🔒 BLOQUÉ (prédiction en cours)' if prediction_in_progress else '🔓 DISPONIBLE'}
                </div>
                <div><strong>Actives:</strong> {len(pending_predictions)}</div>
                <div><strong>Gap requis:</strong> +{PREDICTION_GAP} numéros</div>
                <div><strong>Timeout redémarrage:</strong> {RESTART_TIMEOUT_MINUTES} min</div>
                <div><strong>Max jeu:</strong> #{MAX_GAME_NUMBER}</div>
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
    global source_channel_ok, prediction_channel_ok, restart_task
    
    await reset_restart_timer()
    
    try:
        logger.info("🚀 Démarrage v4.1...")
        logger.info(f"🔒 Mode: {BOT_MODE}")
        logger.info(f"📏 Gap requis: +{PREDICTION_GAP} numéros")
        logger.info(f"⏰ Timeout redémarrage: {RESTART_TIMEOUT_MINUTES} minutes")
        logger.info(f"🎰 Max jeu redémarrage: #{MAX_GAME_NUMBER}")
        
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
                mode_str = "AUTO" if BOT_MODE == "auto" and AUTO_PREDICTION_ENABLED else "MANUEL"
                test_msg = await client.send_message(PREDICTION_CHANNEL_ID, f"🤖 v4.1 connecté! Mode: {mode_str}")
                await asyncio.sleep(1)
                await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
                prediction_channel_ok = True
                logger.info(f"✅ Prédiction: {getattr(pred_entity, 'title', 'N/A')}")
            except Exception as e:
                logger.warning(f"⚠️ Prédiction lecture seule: {e}")
        except Exception as e:
            logger.error(f"❌ Prédiction: {e}")
        
        logger.info(f"⚙️ OFFSET=+{prediction_offset}")
        logger.info("✅ Système opérationnel")
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
            if restart_task and not restart_task.done():
                restart_task.cancel()
                try:
                    await restart_task
                except asyncio.CancelledError:
                    pass
            
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
