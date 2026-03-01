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

def extract_game_number(message: str):
    match = re.search(r"#N\s*(\d+)\.?", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def extract_parentheses_groups(message: str):
    return re.findall(r"\(([^)]*)\)", message)

def normalize_suits(text: str) -> str:
    normalized = text.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def get_first_card_suit(first_group: str) -> str:
    normalized = normalize_suits(first_group)
    match = re.search(r"[0-9AJQKajqk]+\s*([♠♥♦♣])", normalized)
    if match:
        suit = match.group(1)
        return SUIT_DISPLAY.get(suit, suit)
    for suit in ALL_SUITS:
        if suit in normalized:
            return SUIT_DISPLAY.get(suit, suit)
    return None

def get_suit_full_name(suit: str) -> str:
    return SUIT_NAMES.get(suit, suit)

def is_message_finalized(message: str) -> bool:
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    normalized = normalize_suits(group_str)
    target_normalized = normalize_suits(target_suit)
    for suit in ALL_SUITS:
        if suit in target_normalized and suit in normalized:
            return True
    return False
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
        
        if new_status == 'success' or new_status == 'failed':
            logger.info(f"🏁 Prédiction #{target_game} terminée ({status_emoji}), prêt pour nouvelle prédiction")
            active_prediction = None
            waiting_for_finalization = False

        return True

    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        return False
async def check_prediction_result(game_number: int, first_group: str):
    global active_prediction
    
    if not active_prediction:
        return None
    
    target_game = active_prediction['target_game']
    target_suit = active_prediction['suit']
    
    if game_number == target_game:
        if has_suit_in_group(first_group, target_suit):
            await update_prediction_status(target_game, 'success', 0)
            logger.info(f"🎉 Prédiction #{target_game} réussie immédiatement!")
            return True
        else:
            active_prediction['check_count'] = 1
            logger.info(f"⏳ Prédiction #{target_game}: couleur non trouvée, attente jeu +1")
            return False
    
    elif game_number > target_game and game_number <= target_game + 3:
        check_count = game_number - target_game
        
        if has_suit_in_group(first_group, target_suit):
            await update_prediction_status(target_game, 'success', check_count)
            logger.info(f"🎉 Prédiction #{target_game} réussie au jeu +{check_count}!")
            return True
        else:
            active_prediction['check_count'] = check_count + 1
            
            if check_count >= 3:
                await update_prediction_status(target_game, 'failed')
                logger.info(f"😞 Prédiction #{target_game} échouée après 4 vérifications")
                return False
            else:
                logger.info(f"⏳ Prédiction #{target_game}: pas trouvé, attente jeu +{check_count + 1}")
                return False
    
    return None

async def process_message(message_text: str, chat_id: int, is_finalized: bool = False):
    global current_game_number, waiting_for_finalization, active_prediction
    
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            return

        current_game_number = game_number

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
        logger.info(f"Jeu #{game_number} reçu - Groupe1: {first_group}")

        if waiting_for_finalization and is_finalized:
            result = await check_prediction_result(game_number, first_group)
            if result is not None:
                return

        if not waiting_for_finalization and active_prediction is None:
            first_suit = get_first_card_suit(first_group)
            
            if first_suit:
                target_game = game_number + PREDICTION_OFFSET
                if not recent_games.get(target_game, {}).get('predicted', False):
                    await send_prediction(game_number, first_suit)
                    recent_games[target_game] = {'predicted': True, 'suit': first_suit}
                    
                    if len(recent_games) > 100:
                        oldest = min(recent_games.keys())
                        del recent_games[oldest]

        recent_games[game_number] = {
            'first_group': first_group,
            'timestamp': datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"Erreur traitement message: {e}")
        import traceback
        logger.error(traceback.format_exc())

@client.on(events.NewMessage())
async def handle_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id

        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"Message du canal source: {message_text[:80]}...")
            await process_message(message_text, chat_id, is_finalized=False)

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

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"Message édité: {message_text[:80]}...")
            is_finalized = is_message_finalized(message_text)
            await process_message(message_text, chat_id, is_finalized=is_finalized)

    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    await event.respond("🤖 **Bot de Prédiction Baccarat**\n\nCe bot prédit la couleur de la première carte.\n\n**Commandes:**\n• `/status` - Voir la prédiction en cours\n• `/setoffset <nombre>` - Changer l'offset (défaut: 2)\n• `/debug` - Informations de débogage\n• `/checkchannels` - Vérifier l'accès aux canaux")

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
    status_msg = f"📊 **État:**\n\n🎮 Jeu actuel: #{current_game_number}\n📏 Offset: +{PREDICTION_OFFSET}\n\n"
    if active_prediction:
        pred = active_prediction
        distance = pred['target_game'] - current_game_number
        status_msg += f"**🔮 Prédiction active:**\n• Jeu cible: #{pred['target_game']}\n• Couleur: {pred['suit']} {get_suit_full_name(pred['suit'])}\n• Statut: {pred['status']}\n• Distance: {distance} jeux\n• Vérifications: {pred['check_count']}/4"
    else:
        status_msg += "**🔮 Aucune prédiction active**\n✅ Prêt pour nouvelle prédiction"
    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/debug'))
async def cmd_debug(event):
    if event.is_group or event.is_channel:
        return
    debug_msg = f"🔍 **Debug:**\n\n**Config:**\n• Source: {SOURCE_CHANNEL_ID}\n• Prediction: {PREDICTION_CHANNEL_ID}\n• Admin: {ADMIN_ID}\n• Offset: {PREDICTION_OFFSET}\n• Port: {PORT}\n\n**État:**\n• Source OK: {'✅' if source_channel_ok else '❌'}\n• Prediction OK: {'✅' if prediction_channel_ok else '❌'}\n• Jeu actuel: #{current_game_number}\n• Prédiction active: {'Oui' if active_prediction else 'Non'}\n• Attente finalisation: {'Oui' if waiting_for_finalization else 'Non'}"
    await event.respond(debug_msg)

@client.on(events.NewMessage(pattern='/checkchannels'))
async def cmd_checkchannels(event):
    global source_channel_ok, prediction_channel_ok
    if event.is_group or event.is_channel:
        return
    await event.respond("🔍 Vérification des canaux...")
    result_msg = "📡 **Résultat:**\n\n"
    try:
        source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
        source_title = getattr(source_entity, 'title', 'N/A')
        source_channel_ok = True
        result_msg += f"✅ **Source** ({SOURCE_CHANNEL_ID}): {source_title}\n\n"
    except Exception as e:
        source_channel_ok = False
        result_msg += f"❌ **Source** ({SOURCE_CHANNEL_ID}): {str(e)[:100]}\n\n"
    try:
        pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
        pred_title = getattr(pred_entity, 'title', 'N/A')
        test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🔍 Test...")
        await asyncio.sleep(1)
        await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
        prediction_channel_ok = True
        result_msg += f"✅ **Prédiction** ({PREDICTION_CHANNEL_ID}): {pred_title}"
    except Exception as e:
        prediction_channel_ok = False
        result_msg += f"❌ **Prédiction** ({PREDICTION_CHANNEL_ID}): {str(e)[:100]}"
    await event.respond(result_msg)

async def index(request):
    html = f"""<!DOCTYPE html>
<html>
<head><title>Bot Prédiction Baccarat</title><meta charset="utf-8"></head>
<body>
<h1>🎯 Bot de Prédiction Baccarat</h1>
<p>Statut: En ligne ✅</p>
<p><strong>Jeu actuel:</strong> #{current_game_number}</p>
<p><strong>Offset:</strong> +{PREDICTION_OFFSET}</p>
<p><strong>Prédiction active:</strong> {'Oui' if active_prediction else 'Non'}</p>
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
        'source_channel': SOURCE_CHANNEL_ID,
        'source_channel_ok': source_channel_ok,
        'prediction_channel': PREDICTION_CHANNEL_ID,
        'prediction_channel_ok': prediction_channel_ok,
        'current_game': current_game_number,
        'offset': PREDICTION_OFFSET,
        'active_prediction': active_prediction is not None,
        'waiting_for_finalization': waiting_for_finalization,
        'recent_games': len(recent_games),
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
    global source_channel_ok, prediction_channel_ok
    try:
        logger.info("Démarrage du bot...")
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
            test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🤖 Bot connecté!")
            await asyncio.sleep(1)
            await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
            prediction_channel_ok = True
            logger.info(f"✅ Canal prédiction: {getattr(pred_entity, 'title', 'N/A')}")
        except Exception as e:
            logger.error(f"❌ Canal prédiction: {e}")
        logger.info(f"📏 Offset: +{PREDICTION_OFFSET} | Une prédiction à la fois")
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
