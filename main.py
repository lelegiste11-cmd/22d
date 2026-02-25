"""
Bot Telegram de prédiction Baccarat - Version 4.2 AUTOMATIQUE
Prédiction automatique : le bot continue après chaque finalisation
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
auto_continue = True  # ACTIVÉ: Mode automatique activé

# ============ VARIABLES POUR MODE AUTO ============
last_prediction_suit = None  # Mémorise la dernière couleur prédite
last_base_game = 0          # Mémorise le dernier jeu de base
auto_prediction_pending = False  # Indique si une prédiction auto est en attente

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
                # Sauvegarder les infos avant suppression
                resolved_suit = pred['suit']
                resolved_base = pred['base_game']
                
                del pending_predictions[game_number]
                logger.info(f"Prédiction #{game_number} terminée et supprimée")
                logger.info(f"📋 Prédictions restantes: {len(pending_predictions)}")
                
                # MODE AUTO: Préparer la prochaine prédiction
                if auto_continue:
                    global last_prediction_suit, last_base_game, auto_prediction_pending
                    last_prediction_suit = resolved_suit
                    last_base_game = resolved_base
                    auto_prediction_pending = True
                    logger.info(f"🔄 MODE AUTO: Prédiction terminée, prochaine sera créée automatiquement")

        return True

    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """
    Vérifie si une prédiction est gagnée ou perdue.
    MODE AUTO: Continue automatiquement après chaque résultat.
    """
    global auto_prediction_pending
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
                    logger.info(f"  🔄 MODE AUTO: Préparation de la prochaine prédiction...")
                    
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
                        logger.info(f"  🔄 MODE AUTO: Préparation de la prochaine prédiction malgré la défaite...")
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

async def try_create_auto_prediction(current_game: int):
    """
    Tente de créer une prédiction automatique après une finalisation.
    Utilise la couleur du dernier message source disponible.
    """
    global auto_prediction_pending, last_prediction_suit
    
    if not auto_continue:
        return False
    
    if not auto_prediction_pending:
        return False
    
    if has_active_unresolved_predictions():
        logger.info("   ⏸️ AUTO: Impossible de créer - une prédiction est déjà active")
        return False
    
    # Chercher la couleur dans les jeux récents
    target_game = current_game + prediction_offset
    
    # Essayer de récupérer la couleur du jeu actuel ou récent
    suit_to_use = None
    base_game_to_use = current_game
    
    # D'abord essayer le jeu actuel
    if current_game in recent_games:
        first_group = recent_games[current_game]['first_group']
        suit_to_use = extract_first_card_suit(first_group)
    
    # Sinon prendre la dernière couleur mémorisée ou chercher dans l'historique
    if not suit_to_use and last_prediction_suit:
        # Utiliser la même couleur que la dernière prédiction
        suit_to_use = last_prediction_suit
        logger.info(f"   🔄 AUTO: Réutilisation de la dernière couleur {suit_to_use}")
    
    # Chercher dans les jeux récents si toujours pas de couleur
    if not suit_to_use:
        for game_num in sorted(recent_games.keys(), reverse=True):
            first_group = recent_games[game_num]['first_group']
            suit_to_use = extract_first_card_suit(first_group)
            if suit_to_use:
                base_game_to_use = game_num
                break
    
    if suit_to_use and target_game not in pending_predictions:
        success = await create_prediction(target_game, suit_to_use, base_game_to_use, is_continuation=True)
        if success:
            logger.info(f"   ✅ AUTO-PRÉDICTION CRÉÉE: #{target_game} - {suit_to_use} (basé sur #{base_game_to_use})")
            auto_prediction_pending = False
            return True
        else:
            logger.warning(f"   ⚠️ AUTO: Échec création prédiction #{target_game}")
    
    return False

async def process_new_message(message_text: str, chat_id: int, is_finalized: bool = False):
    """
    Traite un nouveau message du canal source.
    MODE AUTO: Crée automatiquement les prédictions en chaîne.
    """
    global current_game_number, last_transferred_game, auto_prediction_pending
    
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
        
        # Stocker pour usage futur (avant toute logique)
        recent_games[game_number] = {
            'first_group': first_group,
            'timestamp': datetime.now().isoformat()
        }
        
        # ========== MODE AUTO: Création automatique si en attente ==========
        if auto_continue and auto_prediction_pending and not is_finalized:
            logger.info(f"   🔄 MODE AUTO: Tentative création automatique...")
            await try_create_auto_prediction(game_number)
        
        # ========== CRÉATION DE PRÉDICTION (NOUVEAU JEU) ==========
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
                                auto_prediction_pending = False  # Réinitialiser
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
            logger.info(f"   ⏭️ Message finalisé - pas de création de prédiction depuis ce message")
        
        # ========== VÉRIFICATION ET FINALISATION ==========
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
                    
                    # MODE AUTO: Attendre un peu puis créer la prochaine prédiction
                    if auto_continue and auto_prediction_pending:
                        logger.info(f"   ⏳ MODE AUTO: Attente de 2s avant création automatique...")
                        await asyncio.sleep(2)
                        
                        # Utiliser le prochain numéro de jeu pour la prédiction
                        next_game = game_number + 1
                        if next_game in recent_games or game_number in recent_games:
                            await try_create_auto_prediction(next_game)
                        
                except Exception as e:
                    logger.error(f"   ❌ Erreur vérification: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                
                if len(processed_finalized) > 100:
                    processed_finalized.clear()
        
        # Nettoyage de l'historique
        if len(recent_games) > 100:
            oldest = min(recent_games.keys())
            del recent_games[oldest]
            
    except Exception as e:
        logger.error(f"❌ Erreur globale process_new_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# ==================== COMMANDES ADMIN ====================

@client.on(events.NewMessage(pattern='/status'))
async def status_command(event):
    """Commande pour voir le statut du bot"""
    try:
        if event.sender_id != ADMIN_ID:
            return
        
        status_msg = f"""📊 **STATUT DU BOT**

🤖 Mode: {'🟢 AUTOMATIQUE' if auto_continue else '🔴 MANUEL'}
⏳ Prédictions actives: {get_active_prediction_count()}
📋 Liste: {list(pending_predictions.keys())}
🔄 Auto-pending: {auto_prediction_pending}
🎯 Dernière couleur: {last_prediction_suit or 'Aucune'}
📊 Jeux en mémoire: {len(recent_games)}"""
        
        await event.reply(status_msg)
        logger.info(f"Commande /status exécutée par admin")
    except Exception as e:
        logger.error(f"Erreur commande status: {e}")

@client.on(events.NewMessage(pattern='/auto_on'))
async def auto_on_command(event):
    """Active le mode automatique"""
    global auto_continue
    try:
        if event.sender_id != ADMIN_ID:
            return
        
        auto_continue = True
        await event.reply("✅ **Mode AUTOMATIQUE activé**\n\nLe bot créera des prédictions en chaîne automatiquement.")
        logger.info("Mode auto activé par admin")
    except Exception as e:
        logger.error(f"Erreur commande auto_on: {e}")

@client.on(events.NewMessage(pattern='/auto_off'))
async def auto_off_command(event):
    """Désactive le mode automatique"""
    global auto_continue, auto_prediction_pending
    try:
        if event.sender_id != ADMIN_ID:
            return
        
        auto_continue = False
        auto_prediction_pending = False
        await event.reply("🔴 **Mode MANUEL activé**\n\nLe bot s'arrêtera après chaque prédiction.")
        logger.info("Mode auto désactivé par admin")
    except Exception as e:
        logger.error(f"Erreur commande auto_off: {e}")

@client.on(events.NewMessage(pattern='/predict'))
async def predict_command(event):
    """Commande manuelle pour forcer une prédiction"""
    try:
        if event.sender_id != ADMIN_ID:
            return
        
        # Extraire le numéro de jeu et la couleur si fournis
        # Format: /predict 123 ♥ ou juste /predict
        args = event.message.text.split()
        
        if len(args) >= 3:
            # Format: /predict <game_number> <suit>
            try:
                target_game = int(args[1])
                suit = args[2]
                if suit not in ALL_SUITS and suit not in SUIT_DISPLAY.values():
                    await event.reply(f"❌ Couleur invalide. Utilisez: ♥ ♠ ♦ ♣")
                    return
                
                success = await create_prediction(target_game, suit, current_game_number)
                if success:
                    await event.reply(f"✅ Prédiction manuelle créée: #{target_game} - {suit}")
                else:
                    await event.reply("❌ Impossible de créer la prédiction (déjà active ou existe déjà)")
            except ValueError:
                await event.reply("❌ Format invalide. Utilisez: /predict <numéro> <couleur>")
        else:
            # Création automatique basée sur le dernier jeu
            if has_active_unresolved_predictions():
                await event.reply("❌ Une prédiction est déjà active. Attendez la finalisation.")
                return
            
            if current_game_number == 0:
                await event.reply("❌ Aucun jeu reçu encore. Attendez un message source.")
                return
            
            target_game = current_game_number + prediction_offset
            if current_game_number in recent_games:
                first_group = recent_games[current_game_number]['first_group']
                suit = extract_first_card_suit(first_group)
                if suit:
                    success = await create_prediction(target_game, suit, current_game_number)
                    if success:
                        await event.reply(f"✅ Prédiction créée: #{target_game} - {suit}")
                    else:
                        await event.reply("❌ Échec création prédiction")
                else:
                    await event.reply("❌ Impossible d'extraire la couleur du dernier jeu")
            else:
                await event.reply("❌ Données du dernier jeu non disponibles")
                
    except Exception as e:
        logger.error(f"Erreur commande predict: {e}")
        await event.reply(f"❌ Erreur: {str(e)}")

@client.on(events.NewMessage(pattern='/reset'))
async def reset_command(event):
    """Reset toutes les prédictions"""
    global pending_predictions, auto_prediction_pending
    try:
        if event.sender_id != ADMIN_ID:
            return
        
        pending_predictions.clear()
        auto_prediction_pending = False
        await event.reply("🗑️ **Toutes les prédictions ont été reset.**\n\nLe bot est prêt pour une nouvelle série.")
        logger.info("Reset des prédictions par admin")
    except Exception as e:
        logger.error(f"Erreur commande reset: {e}")

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

# ==================== SERVEUR WEB (Keep Alive) ====================

async def handle_health(request):
    return web.Response(text="Bot Baccarat Auto v4.2 is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌐 Serveur web démarré sur le port {PORT}")

# ==================== DÉMARRAGE ====================

async def main():
    global source_channel_ok, prediction_channel_ok
    
    logger.info("🚀 Démarrage du Bot Baccarat v4.2 (Mode Automatique)...")
    
    # Démarrer le serveur web
    await start_web_server()
    
    # Connexion Telegram
    await client.start(bot_token=BOT_TOKEN)
    logger.info("✅ Client Telegram connecté")
    
    # Vérifier les canaux
    try:
        if SOURCE_CHANNEL_ID:
            await client.get_entity(SOURCE_CHANNEL_ID)
            source_channel_ok = True
            logger.info(f"✅ Canal source accessible: {SOURCE_CHANNEL_ID}")
    except Exception as e:
        logger.error(f"❌ Canal source inaccessible: {e}")
    
    try:
        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0:
            await client.get_entity(PREDICTION_CHANNEL_ID)
            prediction_channel_ok = True
            logger.info(f"✅ Canal de prédiction accessible: {PREDICTION_CHANNEL_ID}")
    except Exception as e:
        logger.warning(f"⚠️ Canal de prédiction inaccessible: {e}")
    
    logger.info("🤖 Bot prêt et en écoute (Mode: AUTOMATIQUE)")
    logger.info("Commandes disponibles: /status /auto_on /auto_off /predict /reset")
    
    # Garder le bot en vie
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Arrêt du bot demandé par l'utilisateur")
    except Exception as e:
        logger.error(f"❌ Erreur fatale: {e}")
        import traceback
        logger.error(traceback.format_exc())
