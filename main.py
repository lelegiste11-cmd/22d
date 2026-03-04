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

# Configuration du logging
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

logger.info(f"Configuration initiale: SOURCE={SOURCE_CHANNEL_ID}, PREDICTION={PREDICTION_CHANNEL_ID}")

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# --- Variables Globales ---
active_prediction = None
recent_games = {}
processed_messages = set()
current_game_number = 0
waiting_for_finalization = False
source_channel_ok = False
prediction_channel_ok = False

# --- Fonctions Utilitaires ---

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

# --- Gestion des Prédictions ---

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
                logger.info(f"✅ Prédiction envoyée: Jeu #{target_game} sur canal {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi prédiction: {e}")
                return None
        else:
            logger.warning("⚠️ Canal de prédiction non configuré ou inaccessible")
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
        return msg_id

    except Exception as e:
        logger.error(f"Erreur send_prediction: {e}")
        return None

async def update_prediction_status(target_game: int, new_status: str, check_count: int = 0):
    global active_prediction, waiting_for_finalization
    
    try:
        if not active_prediction or active_prediction['target_game'] != target_game:
            return False

        suit = active_prediction['suit']
        suit_name = get_suit_full_name(suit)
        message_id = active_prediction['message_id']
        
        status_emoji = '😶❌'
        if new_status == 'success':
            emojis = ['🍯✅0️⃣', '🍯✅1️⃣', '🍯✅2️⃣', '🍯✅3️⃣']
            status_emoji = emojis[check_count] if check_count < len(emojis) else '🍯✅'

        updated_msg = f"📡 PRÉDICTION #{target_game}\n🎯 Couleur: {suit} {suit_name}\n🌪️ Statut: {status_emoji}"

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour message: {e}")

        if new_status in ['success', 'failed']:
            active_prediction = None
            waiting_for_finalization = False

        return True
    except Exception as e:
        logger.error(f"Erreur update_prediction_status: {e}")
        return False

async def check_prediction_result(game_number: int, first_group: str):
    global active_prediction
    if not active_prediction: return None
    
    target_game = active_prediction['target_game']
    target_suit = active_prediction['suit']
    
    if game_number >= target_game and game_number <= target_game + 3:
        check_count = game_number - target_game
        if has_suit_in_group(first_group, target_suit):
            await update_prediction_status(target_game, 'success', check_count)
            return True
        elif check_count >= 3:
            await update_prediction_status(target_game, 'failed')
            return False
    return None

# --- Traitement des Messages ---

async def process_message(message_text: str, chat_id: int, is_finalized: bool = False):
    global current_game_number, waiting_for_finalization, active_prediction
    try:
        game_number = extract_game_number(message_text)
        if game_number is None: return

        current_game_number = game_number
        message_hash = f"{game_number}_{message_text[:50]}"
        if message_hash in processed_messages: return
        processed_messages.add(message_hash)

        groups = extract_parentheses_groups(message_text)
        if len(groups) < 1: return
        first_group = groups[0]

        if waiting_for_finalization and is_finalized:
            await check_prediction_result(game_number, first_group)

        if not waiting_for_finalization and active_prediction is None:
            first_suit = get_first_card_suit(first_group)
            if first_suit:
                await send_prediction(game_number, first_suit)

    except Exception as e:
        logger.error(f"Erreur process_message: {e}")

# --- Handlers Telegram ---

@client.on(events.NewMessage())
async def handle_message(event):
    if event.chat_id == SOURCE_CHANNEL_ID:
        await process_message(event.message.message, event.chat_id, is_finalized=False)

@client.on(events.MessageEdited())
async def handle_edited_message(event):
    if event.chat_id == SOURCE_CHANNEL_ID:
        is_finalized = is_message_finalized(event.message.message)
        await process_message(event.message.message, event.chat_id, is_finalized=is_finalized)

# --- COMMANDES ADMIN ---

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_private:
        await event.respond(
            "🤖 **Bot de Prédiction Baccarat**\n\n"
            "**Commandes :**\n"
            "• `/status` - État actuel\n"
            "• `/setoffset <nb>` - Modifier l'offset\n"
            "• `/setpredchannel <id>` - Définir le canal de destination\n"
            "• `/checkchannels` - Vérifier les accès"
        )

@client.on(events.NewMessage(pattern='/setpredchannel'))
async def cmd_setpredchannel(event):
    global PREDICTION_CHANNEL_ID, prediction_channel_ok
    if not event.is_private or (event.sender_id != ADMIN_ID and ADMIN_ID != 0): return

    try:
        parts = event.message.text.split()
        if len(parts) < 2:
            await event.respond("Usage: `/setpredchannel -100123456789`")
            return

        new_id = int(re.search(r'(-?\d+)', parts[1]).group(1))
        
        # Test d'accès
        entity = await client.get_entity(new_id)
        test_msg = await client.send_message(new_id, "✅ Canal lié avec succès pour les prédictions.")
        
        PREDICTION_CHANNEL_ID = new_id
        prediction_channel_ok = True
        
        await event.respond(f"✅ **Canal mis à jour** : {getattr(entity, 'title', 'Inconnu')} (`{new_id}`)")
        await asyncio.sleep(3)
        await client.delete_messages(new_id, test_msg.id)
    except Exception as e:
        await event.respond(f"❌ Erreur : {e}")

@client.on(events.NewMessage(pattern='/setoffset'))
async def cmd_setoffset(event):
    global PREDICTION_OFFSET
    if not event.is_private or (event.sender_id != ADMIN_ID and ADMIN_ID != 0): return
    try:
        new_offset = int(event.message.text.split()[1])
        PREDICTION_OFFSET = new_offset
        await event.respond(f"✅ Offset modifié : +{new_offset}")
    except:
        await event.respond("Usage: `/setoffset 2`")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if not event.is_private: return
    msg = f"📊 **État**:\nJeu: #{current_game_number}\nOffset: +{PREDICTION_OFFSET}\nCanal Pred: `{PREDICTION_CHANNEL_ID}`"
    if active_prediction:
        msg += f"\n\n🔮 **Active**: #{active_prediction['target_game']} ({active_prediction['suit']})"
    await event.respond(msg)

@client.on(events.NewMessage(pattern='/checkchannels'))
async def cmd_checkchannels(event):
    global source_channel_ok, prediction_channel_ok
    if not event.is_private: return
    res = "🔍 **Vérification**:\n"
    try:
        await client.get_entity(SOURCE_CHANNEL_ID)
        source_channel_ok = True
        res += "✅ Source: OK\n"
    except Exception as e: res += f"❌ Source: {e}\n"
    
    try:
        await client.get_entity(PREDICTION_CHANNEL_ID)
        prediction_channel_ok = True
        res += "✅ Prédiction: OK"
    except Exception as e: res += f"❌ Prédiction: {e}"
    await event.respond(res)

# --- Web Server & Main ---

async def index(request):
    return web.Response(text=f"Bot Running - Game #{current_game_number}", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', index)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()

async def main():
    await start_web_server()
    await client.start(bot_token=BOT_TOKEN)
    logger.info("Bot connecté !")
    
    # Init check
    global source_channel_ok, prediction_channel_ok
    try: await client.get_entity(SOURCE_CHANNEL_ID); source_channel_ok = True
    except: pass
    try: await client.get_entity(PREDICTION_CHANNEL_ID); prediction_channel_ok = True
    except: pass

    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
