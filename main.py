import os
import asyncio
import re
import logging
import sys
from datetime import datetime
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_MAPPING, ALL_SUITS, SUIT_DISPLAY, SUIT_NAMES, PREDICTION_OFFSET
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
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

logger.info(f"Configuration: SOURCE_CHANNEL={SOURCE_CHANNEL_ID}, PREDICTION_CHANNEL={PREDICTION_CHANNEL_ID}, PORT={PORT}")

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

active_prediction = None
recent_games = {}
processed_messages = set()
current_game_number = 0
waiting_for_finalization = False
source_channel_ok = False
prediction_channel_ok = False
cycle_count = 1

# ID du canal de statistiques (à configurer)
STATS_CHANNEL_ID = int(os.getenv('STATS_CHANNEL_ID') or '0')

def extract_game_number(message: str):
    match = re.search(r"#N\s*(\d+)\.?", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def extract_first_parenthesis_group(message: str) -> str:
    """Extrait UNIQUEMENT la première parenthèse du message"""
    match = re.search(r"\(([^)]*)\)", message)
    if match:
        return match.group(1)
    return ""

def normalize_suits(text: str) -> str:
    normalized = text.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def get_first_card_suit(first_group: str) -> str:
    """Extrait la couleur de la première carte du premier groupe (pour prédiction)"""
    normalized = normalize_suits(first_group)
    match = re.search(r"[0-9AJQKajqk]+\s*([♠♥♦♣])", normalized)
    if match:
        suit = match.group(1)
        return SUIT_DISPLAY.get(suit, suit)
    for suit in ALL_SUITS:
        if suit in normalized:
            return SUIT_DISPLAY.get(suit, suit)
    return None

def has_suit_in_first_parenthesis(message_text: str, target_suit: str) -> bool:
    """Vérifie si la couleur cible est présente dans la PREMIÈRE parenthèse uniquement"""
    first_parenthesis = extract_first_parenthesis_group(message_text)
    if not first_parenthesis:
        return False
    
    normalized = normalize_suits(first_parenthesis)
    target_normalized = normalize_suits(target_suit)
    
    for suit in ALL_SUITS:
        if suit in target_normalized and suit in normalized:
            return True
    return False

def get_suit_full_name(suit: str) -> str:
    return SUIT_NAMES.get(suit, suit)

def is_message_finalized(message: str) -> bool:
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message

async def reset_bot_state():
    """Réinitialise complètement l'état du bot pour un nouveau cycle"""
    global active_prediction, recent_games, processed_messages, current_game_number, waiting_for_finalization, cycle_count
    
    logger.info(f"🔄 RÉINITIALISATION COMPLÈTE - Fin du cycle {cycle_count}")
    
    if active_prediction:
        try:
            target_game = active_prediction['target_game']
            suit = active_prediction['suit']
            suit_name = get_suit_full_name(suit)
            message_id = active_prediction['message_id']
            
            updated_msg = f"📡 PRÉDICTION #{target_game}\n🎯 Couleur: {suit} {suit_name}\n🌪️ Statut: ⏹️ CYCLE TERMINÉ"
            
            if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0 and prediction_channel_ok:
                try:
                    await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
                    logger.info(f"⏹️ Prédiction #{target_game} marquée comme terminée (cycle fin)")
                except Exception as e:
                    logger.error(f"Erreur mise à jour fin cycle: {e}")
        except Exception as e:
            logger.error(f"Erreur lors de l'annulation de la prédiction: {e}")
    
    active_prediction = None
    recent_games = {}
    processed_messages = set()
    current_game_number = 0
    waiting_for_finalization = False
    cycle_count += 1
    
    logger.info(f"✅ Nouveau cycle {cycle_count} démarré - Prêt pour les prédictions")
    
    try:
        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and prediction_channel_ok:
            await client.send_message(
                PREDICTION_CHANNEL_ID,
                f"🔄 **NOUVEAU CYCLE #{cycle_count}**\n\nLe jeu 1440 a été atteint.\nLe bot redémarre pour un nouveau cycle de 1-1440.\n\n✅ Prêt pour de nouvelles prédictions!"
            )
    except Exception as e:
        logger.error(f"Erreur envoi notification nouveau cycle: {e}")

async def send_prediction(game_number: int, suit: str):
    global active_prediction, waiting_for_finalization
    
    try:
        target_game = game_number + PREDICTION_OFFSET
        suit_name = get_suit_full_name(suit)
        
        prediction_msg = f"📡 PRÉDICTION #{target_game}\n🎯 Couleur: {suit} {suit_name}\n🌪️ Statut: ⏳ EN COURS"

        msg_id = 0
        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and prediction_channel_ok:
            try:
                pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
                msg_id = pred_msg.id
                logger.info(f"✅ Prédiction envoyée: Jeu #{target_game} - {suit}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi prédiction: {e}")
                return None
        else:
            logger.warning("⚠️ Canal de prédiction non accessible")
            return None

        active_prediction = {
            'source_game': game_number,
            'target_game': target_game,
            'suit': suit,
            'message_id': msg_id,
            'status': '⏳',
            'check_count': 0,
            'created_at': datetime.now().isoformat()
        }
        waiting_for_finalization = True
        
        logger.info(f"🎯 Prédiction active: Jeu #{target_game} - {suit} (basé sur #{game_number})")
        return msg_id

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

async def update_prediction_status(target_game: int, new_status: str, check_count: int = 0):
    global active_prediction, waiting_for_finalization
    
    try:
        if not active_prediction or active_prediction['target_game'] != target_game:
            return False

        suit = active_prediction['suit']
        suit_name = get_suit_full_name(suit)
        message_id = active_prediction['message_id']
        
        if new_status == 'success':
            if check_count == 0:
                status_emoji = '🍯✅0️⃣'
            elif check_count == 1:
                status_emoji = '🍯✅1️⃣'
            elif check_count == 2:
                status_emoji = '🍯✅2️⃣'
            elif check_count == 3:
                status_emoji = '🍯✅3️⃣'
            else:
                status_emoji = '🍯✅'
        else:
            status_emoji = '😶❌'

        updated_msg = f"📡 PRÉDICTION #{target_game}\n🎯 Couleur: {suit} {suit_name}\n🌪️ Statut: {status_emoji}"

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
                logger.info(f"✅ Prédiction #{target_game} mise à jour: {status_emoji}")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour: {e}")

        active_prediction['status'] = status_emoji
        
        if new_status == 'success' or (new_status == 'failed' and check_count >= 3):
            logger.info(f"🏁 Prédiction #{target_game} terminée ({status_emoji}), prêt pour nouvelle prédiction")
            active_prediction = None
            waiting_for_finalization = False

        return True

    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        return False

async def fetch_stats_message(target_game: int) -> str:
    """Récupère le message de statistiques pour le numéro cible"""
    if not STATS_CHANNEL_ID or STATS_CHANNEL_ID == 0:
        logger.warning(f"⚠️ Canal de statistiques non configuré")
        return None
    
    try:
        # Chercher le message dans le canal de statistiques
        async for message in client.iter_messages(STATS_CHANNEL_ID, limit=50):
            if message.message:
                game_num = extract_game_number(message.message)
                if game_num == target_game:
                    logger.info(f"📊 Message stats trouvé pour jeu #{target_game}")
                    return message.message
        
        logger.warning(f"⚠️ Message stats non trouvé pour jeu #{target_game}")
        return None
        
    except Exception as e:
        logger.error(f"❌ Erreur récupération stats: {e}")
        return None

async def check_prediction_result(target_game: int, check_count: int = 0):
    """
    Vérifie le résultat en lisant le canal de statistiques
    Retourne: True (trouvé), False (pas trouvé), None (erreur)
    """
    global active_prediction
    
    if not active_prediction:
        return None
    
    target_suit = active_prediction['suit']
    
    # Récupérer le message des statistiques
    stats_message = await fetch_stats_message(target_game)
    
    if not stats_message:
        logger.warning(f"⚠️ Pas de stats disponibles pour #{target_game}")
        return None
    
    # Vérifier dans la première parenthèse des statistiques
    found_suit = has_suit_in_first_parenthesis(stats_message, target_suit)
    
    logger.info(f"🔍 Vérification Stats #{target_game}: cible={target_suit}, trouvé={found_suit}")
    logger.info(f"📊 Message stats: {stats_message[:100]}...")
    
    return found_suit

async def process_stats_message(message_text: str, chat_id: int):
    """Traite un message du canal de statistiques pour vérification"""
    global active_prediction, waiting_for_finalization
    
    if not active_prediction or not waiting_for_finalization:
        return
    
    game_number = extract_game_number(message_text)
    if game_number is None:
        return
    
    target_game = active_prediction['target_game']
    target_suit = active_prediction['suit']
    
    # Vérifier si ce message correspond au jeu cible ou aux suivants
    if game_number < target_game or game_number > target_game + 3:
        return
    
    logger.info(f"📊 Stats reçu pour jeu #{game_number} (cible: #{target_game})")
    
    # Vérifier dans la première parenthèse
    found_suit = has_suit_in_first_parenthesis(message_text, target_suit)
    
    if game_number == target_game:
        if found_suit:
            await update_prediction_status(target_game, 'success', 0)
            logger.info(f"🎉 Prédiction #{target_game} réussie! Trouvé dans stats")
        else:
            active_prediction['check_count'] = 1
            logger.info(f"⏳ #{target_game}: pas trouvé dans stats, attente +1")
    
    elif game_number == target_game + 1:
        if found_suit:
            await update_prediction_status(target_game, 'success', 1)
            logger.info(f"🎉 #{target_game} réussie au +1!")
        else:
            active_prediction['check_count'] = 2
            logger.info(f"⏳ #{target_game}: pas trouvé au +1, attente +2")
    
    elif game_number == target_game + 2:
        if found_suit:
            await update_prediction_status(target_game, 'success', 2)
            logger.info(f"🎉 #{target_game} réussie au +2!")
        else:
            active_prediction['check_count'] = 3
            logger.info(f"⏳ #{target_game}: pas trouvé au +2, attente +3")
    
    elif game_number == target_game + 3:
        if found_suit:
            await update_prediction_status(target_game, 'success', 3)
            logger.info(f"🎉 #{target_game} réussie au +3!")
        else:
            await update_prediction_status(target_game, 'failed', 4)
            logger.info(f"😞 #{target_game} échouée après 4 vérifications")

async def process_source_message(message_text: str, chat_id: int):
    """Traite un message du canal source (pour prédiction uniquement)"""
    global current_game_number, waiting_for_finalization, active_prediction, cycle_count
    
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            return

        # Détection du numéro 1440
        if game_number == 1440:
            logger.info(f"🚨 NUMÉRO 1440 DÉTECTÉ (Cycle {cycle_count}) - Redémarrage!")
            await reset_bot_state()
            return

        current_game_number = game_number

        message_hash = f"{game_number}_{message_text[:50]}_{cycle_count}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)
        if len(processed_messages) > 200:
            processed_messages.clear()

        first_parenthesis = extract_first_parenthesis_group(message_text)
        if not first_parenthesis:
            logger.warning(f"⚠️ Jeu #{game_number}: aucune parenthèse trouvée")
            return

        logger.info(f"Source #{game_number} (Cycle {cycle_count}) - 1ère parenthèse: {first_parenthesis}")

        # PRÉDICTION uniquement (pas de vérification sur canal source)
        if not waiting_for_finalization and active_prediction is None:
            first_suit = get_first_card_suit(first_parenthesis)
            
            if first_suit:
                target_game = game_number + PREDICTION_OFFSET
                
                if target_game > 1440:
                    logger.info(f"⚠️ Prédiction #{target_game} dépasserait 1440, ignorée")
                    return
                
                if not recent_games.get(target_game, {}).get('predicted', False):
                    await send_prediction(game_number, first_suit)
                    recent_games[target_game] = {'predicted': True, 'suit': first_suit}
                    
                    if len(recent_games) > 100:
                        oldest = min(recent_games.keys())
                        del recent_games[oldest]

        recent_games[game_number] = {
            'first_parenthesis': first_parenthesis,
            'timestamp': datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"Erreur traitement message source: {e}")
        import traceback
        logger.error(traceback.format_exc())

@client.on(events.NewMessage())
async def handle_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id

        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id

        # Canal source - pour prédiction
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"📥 Source: {message_text[:80]}...")
            await process_source_message(message_text, chat_id)
        
        # Canal de statistiques - pour vérification
        elif STATS_CHANNEL_ID and chat_id == STATS_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"📊 Stats: {message_text[:80]}...")
            await process_stats_message(message_text, chat_id)

    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

@client.on(events.MessageEdited())
async def handle_edited_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id

        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id

        # Vérifier les messages édités dans le canal de statistiques
        if STATS_CHANNEL_ID and chat_id == STATS_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"📊 Stats édité: {message_text[:80]}...")
            
            # Vérifier si finalisé avant de traiter
            if is_message_finalized(message_text):
                await process_stats_message(message_text, chat_id)

    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    stats_info = f"\n📊 Canal stats: {STATS_CHANNEL_ID}" if STATS_CHANNEL_ID else "\n⚠️ Canal stats: NON CONFIGURÉ"
    await event.respond(f"🤖 **Bot de Prédiction Baccarat**{stats_info}\n\nCycle: #{cycle_count}\n\nCe bot prédit la couleur de la première carte.\n\n**Commandes:**\n• `/status` - Voir la prédiction en cours\n• `/setoffset <nombre>` - Changer l'offset\n• `/setstats <id>` - Configurer canal statistiques\n• `/debug` - Informations de débogage\n• `/reset` - Forcer redémarrage cycle")

@client.on(events.NewMessage(pattern='/setstats'))
async def cmd_setstats(event):
    """Configure l'ID du canal de statistiques"""
    global STATS_CHANNEL_ID
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return
    try:
        parts = event.message.text.split()
        if len(parts) < 2:
            await event.respond("Usage: `/setstats <channel_id>`\nExemple: `/setstats -1001234567890`")
            return
        
        new_stats_id = int(parts[1])
        STATS_CHANNEL_ID = new_stats_id
        await event.respond(f"✅ Canal de statistiques configuré: `{new_stats_id}`")
        logger.info(f"Canal stats modifié par admin: {new_stats_id}")
    except ValueError:
        await event.respond("❌ ID invalide. Exemple: `-1001234567890`")

@client.on(events.NewMessage(pattern='/setoffset'))
async def cmd_setoffset(event):
    global PREDICTION_OFFSET
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return
    try:
        parts = event.message.text.split()
        if len(parts) < 2:
            await event.respond("Usage: `/setoffset <nombre>`\nExemple: `/setoffset 3`")
            return
        new_offset = int(parts[1])
        if new_offset < 1:
            await event.respond("L'offset doit être ≥ 1")
            return
        PREDICTION_OFFSET = new_offset
        await event.respond(f"✅ Offset modifié: **+{new_offset}**")
        logger.info(f"Offset modifié par admin: {new_offset}")
    except ValueError:
        await event.respond("❌ Entrez un nombre valide")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return
    
    stats_status = f"📊 Stats: {STATS_CHANNEL_ID}" if STATS_CHANNEL_ID else "⚠️ Stats: NON CONFIGURÉ"
    status_msg = f"📊 **État (Cycle #{cycle_count}):**\n\n🎮 Jeu actuel: #{current_game_number}\n📏 Offset: +{PREDICTION_OFFSET}\n{stats_status}\n\n"
    
    if active_prediction:
        pred = active_prediction
        distance = pred['target_game'] - current_game_number
        status_msg += f"**🔮 Prédiction active:**\n• Jeu cible: #{pred['target_game']}\n• Couleur: {pred['suit']} {get_suit_full_name(pred['suit'])}\n• Statut: {pred['status']}\n• Vérifications: {pred['check_count']}/4"
    else:
        status_msg += "**🔮 Aucune prédiction active**\n✅ Prêt pour nouvelle prédiction"
    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/debug'))
async def cmd_debug(event):
    if event.is_group or event.is_channel:
        return
    stats_info = f"📊 Stats: {STATS_CHANNEL_ID}" if STATS_CHANNEL_ID else "⚠️ Stats: NON CONFIGURÉ"
    debug_msg = f"🔍 **Debug (Cycle #{cycle_count}):**\n\n**Config:**\n• Source: {SOURCE_CHANNEL_ID}\n• Prediction: {PREDICTION_CHANNEL_ID}\n• Stats: {STATS_CHANNEL_ID}\n• Admin: {ADMIN_ID}\n• Offset: {PREDICTION_OFFSET}\n\n**État:**\n• {stats_info}\n• Source OK: {'✅' if source_channel_ok else '❌'}\n• Prediction OK: {'✅' if prediction_channel_ok else '❌'}\n• Jeu: #{current_game_number}\n• Prédiction: {'Oui' if active_prediction else 'Non'}"
    await event.respond(debug_msg)

@client.on(events.NewMessage(pattern='/reset'))
async def cmd_reset(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return
    
    logger.info(f"🔄 Redémarrage manuel demandé par admin (Cycle {cycle_count})")
    await event.respond(f"🔄 Redémarrage du cycle #{cycle_count}...")
    await reset_bot_state()
    await event.respond(f"✅ Cycle redémarré! Nouveau cycle: #{cycle_count}")

@client.on(events.NewMessage(pattern='/checkchannels'))
async def cmd_checkchannels(event):
    global source_channel_ok, prediction_channel_ok
    if event.is_group or event.is_channel:
        return
    await event.respond("🔍 Vérification des canaux...")
    result_msg = "📡 **Résultat:**\n\n"
    
    # Vérifier canal source
    try:
        source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
        source_title = getattr(source_entity, 'title', 'N/A')
        source_channel_ok = True
        result_msg += f"✅ **Source** ({SOURCE_CHANNEL_ID}): {source_title}\n\n"
    except Exception as e:
        source_channel_ok = False
        result_msg += f"❌ **Source** ({SOURCE_CHANNEL_ID}): {str(e)[:100]}\n\n"
    
    # Vérifier canal prédiction
    try:
        pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
        pred_title = getattr(pred_entity, 'title', 'N/A')
        test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🔍 Test...")
        await asyncio.sleep(1)
        await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
        prediction_channel_ok = True
        result_msg += f"✅ **Prédiction** ({PREDICTION_CHANNEL_ID}): {pred_title}\n\n"
    except Exception as e:
        prediction_channel_ok = False
        result_msg += f"❌ **Prédiction** ({PREDICTION_CHANNEL_ID}): {str(e)[:100]}\n\n"
    
    # Vérifier canal stats
    if STATS_CHANNEL_ID:
        try:
            stats_entity = await client.get_entity(STATS_CHANNEL_ID)
            stats_title = getattr(stats_entity, 'title', 'N/A')
            result_msg += f"✅ **Stats** ({STATS_CHANNEL_ID}): {stats_title}"
        except Exception as e:
            result_msg += f"❌ **Stats** ({STATS_CHANNEL_ID}): {str(e)[:100]}"
    else:
        result_msg += "⚠️ **Stats**: Non configuré"
    
    await event.respond(result_msg)

async def index(request):
    html = f"""<!DOCTYPE html>
<html>
<head><title>Bot Prédiction Baccarat</title><meta charset="utf-8"></head>
<body>
<h1>🎯 Bot de Prédiction Baccarat</h1>
<p>Statut: En ligne ✅</p>
<p><strong>Cycle:</strong> #{cycle_count}</p>
<p><strong>Jeu actuel:</strong> #{current_game_number}</p>
<p><strong>Stats:</strong> {STATS_CHANNEL_ID if STATS_CHANNEL_ID else 'Non config'}</p>
<ul>
<li><a href="/health">Health Check</a></li>
<li><a href="/status">Statut JSON</a></li>
</ul>
</body>
</html>"""
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def status_api(request):
    status_data = {
        'status': 'running',
        'cycle': cycle_count,
        'source_channel': SOURCE_CHANNEL_ID,
        'stats_channel': STATS_CHANNEL_ID,
        'prediction_channel': PREDICTION_CHANNEL_ID,
        'current_game': current_game_number,
        'offset': PREDICTION_OFFSET,
        'active_prediction': active_prediction is not None,
        'timestamp': datetime.now().isoformat()
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
    global source_channel_ok, prediction_channel_ok, cycle_count
    try:
        logger.info(f"Démarrage du bot - Cycle #{cycle_count}")
        await client.start(bot_token=BOT_TOKEN)
        logger.info("Bot Telegram connecté")
        session = client.session.save()
        logger.info(f"Session: {session[:50]}...")
        me = await client.get_me()
        username = getattr(me, 'username', 'Unknown')
        logger.info(f"Bot opérationnel: @{username}")
        
        try:
            source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
            source_channel_ok = True
            logger.info(f"✅ Canal source: {getattr(source_entity, 'title', 'N/A')}")
        except Exception as e:
            logger.error(f"❌ Canal source: {e}")
        
        try:
            pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
            test_msg = await client.send_message(PREDICTION_CHANNEL_ID, f"🤖 Bot connecté! (Cycle #{cycle_count})")
            await asyncio.sleep(1)
            await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
            prediction_channel_ok = True
            logger.info(f"✅ Canal prédiction: {getattr(pred_entity, 'title', 'N/A')}")
        except Exception as e:
            logger.error(f"❌ Canal prédiction: {e}")
        
        if STATS_CHANNEL_ID:
            try:
                stats_entity = await client.get_entity(STATS_CHANNEL_ID)
                logger.info(f"✅ Canal stats configuré: {getattr(stats_entity, 'title', 'N/A')}")
            except Exception as e:
                logger.warning(f"⚠️ Canal stats non accessible: {e}")
        else:
            logger.warning(f"⚠️ Canal stats non configuré! Utilisez /setstats")
        
        logger.info(f"📏 Offset: +{PREDICTION_OFFSET} | Cycle: 1-1440")
        return True
    except Exception as e:
        logger.error(f"Erreur démarrage: {e}")
        return False

async def main():
    try:
        await start_web_server()
        success = await start_bot()
        if not success:
            return
        logger.info("Bot opérationnel - En attente...")
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Erreur: {e}")
    finally:
        await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot arrêté")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
