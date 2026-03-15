import os
import asyncio
import re
import logging
import sys
from datetime import datetime, timezone
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_MAPPING, ALL_SUITS, SUIT_DISPLAY, SUIT_NAMES
)

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Vérification des credentials
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

# Client Telegram
session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# Variables globales
# Liste des prédictions actives (plusieurs en parallèle)
active_predictions = {}  # {game_number: prediction_data}
recent_games = {}
processed_messages = set()
last_transferred_game = None
current_game_number = 0
source_channel_ok = False
prediction_channel_ok = False
transfer_enabled = True

# Décalage fixe: +2 pour avoir des numéros impairs consécutifs
PREDICTION_OFFSET = 2

def is_odd_number(n: int) -> bool:
    """Vérifie si un nombre est impair"""
    return n % 2 == 1

def get_next_odd_prediction(base: int) -> int:
    """
    Retourne le prochain numéro de prédiction impair.
    Ex: base=51 → 53, base=52 → 53, base=53 → 55
    """
    target = base + PREDICTION_OFFSET  # base + 2
    # Si pair, ajouter 1 pour obtenir impair
    if target % 2 == 0:
        target += 1
    return target

# Mapping des opposés
OPPOSITE_SUIT = {
    '♣️': '♠️', '♣': '♠️',
    '♠️': '♣️', '♠': '♣️',
    '❤️': '♦️', '❤': '♦️',
    '♥️': '♦️', '♥': '♦️',
    '♦️': '❤️', '♦': '❤️'
}

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message"""
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def extract_parentheses_groups(message: str):
    """Extrait les groupes entre parenthèses"""
    return re.findall(r"\(([^)]*)\)", message)

def normalize_suit(suit: str) -> str:
    """Normalise la couleur au format standard"""
    suit = suit.strip()
    if suit in ['♠', '♠️']:
        return '♠️'
    elif suit in ['♥', '♥️', '❤', '❤️']:
        return '❤️'
    elif suit in ['♦', '♦️']:
        return '♦️'
    elif suit in ['♣', '♣️']:
        return '♣️'
    return suit

def get_first_card_suit(group_str: str):
    """Extrait la couleur de la première carte du groupe"""
    match = re.search(r"[0-9AJQKajqk]+([♠♥♦♣❤️️])", group_str)
    if match:
        return normalize_suit(match.group(1))
    return None

def get_opposite_suit(suit: str) -> str:
    """Retourne la couleur opposée"""
    return OPPOSITE_SUIT.get(suit, suit)

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé"""
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message

def has_suit_in_first_group(message_text: str, target_suit: str) -> bool:
    """Vérifie si la couleur cible est dans le premier groupe de parenthèses"""
    groups = extract_parentheses_groups(message_text)
    if not groups:
        return False
    
    first_group = groups[0]
    # Normalise pour comparaison
    normalized_first = first_group.replace('❤️', '♥').replace('❤', '♥')
    normalized_target = target_suit.replace('❤️', '♥').replace('❤', '♥')
    
    # Cherche toutes les couleurs dans le premier groupe
    suits_in_group = []
    for suit in ['♠', '♥', '♦', '♣', '❤']:
        if suit in normalized_first:
            suits_in_group.append(normalize_suit(suit))
    
    # Vérifie si la couleur cible est présente
    for suit in suits_in_group:
        if suit == normalized_target:
            return True
    
    return False

async def send_prediction(game_number: int, first_card_suit: str):
    """Envoie une prédiction au canal - prédiction en continu"""
    global active_predictions
    
    # Calcule le prochain numéro impair (toujours +2)
    target_game = get_next_odd_prediction(game_number)
    
    opposite_suit = get_opposite_suit(first_card_suit)
    suit_name = SUIT_NAMES.get(opposite_suit, opposite_suit)
    
    prediction_msg = f"""🎰 PRÉDICTION #{target_game}
🎯 Couleur: {opposite_suit} {suit_name}
📊 Statut: ⏳⏳"""

    msg_id = 0
    if PREDICTION_CHANNEL_ID and prediction_channel_ok:
        try:
            pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
            msg_id = pred_msg.id
            logger.info(f"✅ Prédiction envoyée: Jeu #{target_game} - {opposite_suit} (base: #{game_number})")
        except Exception as e:
            logger.error(f"❌ Erreur envoi prédiction: {e}")
    else:
        logger.warning(f"⚠️ Canal de prédiction non accessible")

    # Stocke la prédiction active
    active_predictions[target_game] = {
        'game_number': target_game,
        'message_id': msg_id,
        'predicted_suit': opposite_suit,
        'base_game': game_number,
        'first_card_suit': first_card_suit,
        'status': '⏳⏳',
        'check_count': 0,
        'created_at': datetime.now().isoformat()
    }
    
    return msg_id

async def update_prediction_status(game_number: int, status_code: str, status_emoji: str):
    """Met à jour le statut d'une prédiction"""
    global active_predictions
    
    if game_number not in active_predictions:
        return False

    try:
        pred = active_predictions[game_number]
        message_id = pred['message_id']
        suit = pred['predicted_suit']
        suit_name = SUIT_NAMES.get(suit, suit)

        updated_msg = f"""📡 PRÉDICTION #{game_number}
🎯 Couleur: {suit} {suit_name}
🌪️ Statut: {status_emoji}"""

        if PREDICTION_CHANNEL_ID and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
                logger.info(f"✅ Prédiction #{game_number} mise à jour: {status_emoji}")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour: {e}")

        pred['status'] = status_code
        logger.info(f"Prédiction #{game_number} statut: {status_code}")
        
        # Supprime la prédiction terminée
        if status_code in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '❌']:
            del active_predictions[game_number]
            logger.info(f"Prédiction #{game_number} terminée ({status_code})")

        return True

    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        return False

async def check_prediction_result(game_number: int, message_text: str):
    """Vérifie le résultat de toutes les prédictions actives"""
    global active_predictions
    
    if not active_predictions:
        return
    
    # Vérifie chaque prédiction active
    predictions_to_check = list(active_predictions.items())
    
    for pred_game, pred_data in predictions_to_check:
        target_game = pred_data['game_number']
        predicted_suit = pred_data['predicted_suit']
        check_count = pred_data.get('check_count', 0)
        current_status = pred_data.get('status', '⏳⏳')
        
        # Si déjà terminée, skip
        if current_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '❌']:
            continue
        
        # Vérification au numéro exact (N+a)
        if game_number == target_game and check_count == 0:
            if has_suit_in_first_group(message_text, predicted_suit):
                await update_prediction_status(target_game, '✅0️⃣', '🍯✅0️⃣')
            else:
                pred_data['check_count'] = 1
                logger.info(f"Prédiction #{target_game}: {predicted_suit} non trouvé, attente #{target_game+1}")
        
        # Vérification au numéro + 1
        elif game_number == target_game + 1 and check_count == 1:
            if has_suit_in_first_group(message_text, predicted_suit):
                await update_prediction_status(target_game, '✅1️⃣', '🍯✅1️⃣')
            else:
                pred_data['check_count'] = 2
                logger.info(f"Prédiction #{target_game}: {predicted_suit} non trouvé au +1, attente #{target_game+2}")
        
        # Vérification au numéro + 2
        elif game_number == target_game + 2 and check_count == 2:
            if has_suit_in_first_group(message_text, predicted_suit):
                await update_prediction_status(target_game, '✅2️⃣', '🍯✅2️⃣')
            else:
                await update_prediction_status(target_game, '❌', '❌')
        
        # Si on dépasse le numéro + 2 sans trouver
        elif game_number > target_game + 2:
            await update_prediction_status(target_game, '❌', '❌')

async def process_new_message(message_text: str, chat_id: int, is_finalized: bool = False):
    """Traite un nouveau message du canal source"""
    global last_transferred_game, current_game_number
    
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            return

        current_game_number = game_number

        # Évite le traitement en double
        message_hash = f"{game_number}_{message_text[:50]}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)
        if len(processed_messages) > 200:
            processed_messages.clear()

        groups = extract_parentheses_groups(message_text)
        if len(groups) < 2:
            return

        first_group = groups[0]
        second_group = groups[1]

        logger.info(f"Jeu #{game_number} - G1: {first_group}, G2: {second_group}")

        # Transfert des messages finalisés à l'admin
        if is_finalized and transfer_enabled and ADMIN_ID and last_transferred_game != game_number:
            try:
                transfer_msg = f"📨 **Message finalisé:**\n\n{message_text}"
                await client.send_message(ADMIN_ID, transfer_msg)
                last_transferred_game = game_number
            except Exception as e:
                logger.error(f"❌ Erreur transfert: {e}")

        # VÉRIFICATION: Vérifie toutes les prédictions actives (immédiate, pas besoin d'attendre finalisation)
        await check_prediction_result(game_number, message_text)

        # PRÉDICTION EN CONTINU: Si le numéro est impair, prédit immédiatement (sans attendre)
        if is_odd_number(game_number):
            second_group_clean = second_group.strip()
            # Vérifie que le 2ème groupe a des cartes
            if second_group_clean and second_group_clean != '0':
                first_card_suit = get_first_card_suit(second_group)
                if first_card_suit:
                    opposite = get_opposite_suit(first_card_suit)
                    
                    # Vérifie si on a déjà une prédiction pour ce numéro cible
                    target_game = get_next_odd_prediction(game_number)
                    
                    if target_game not in active_predictions:
                        logger.info(f"🎯 NOUVELLE PRÉDICTION - Jeu #{game_number} (impair) → prédit #{target_game}: {opposite}")
                        await send_prediction(game_number, first_card_suit)
                    else:
                        logger.info(f"⏭️ Prédiction #{target_game} existe déjà, on continue")

        # Stockage des données
        recent_games[game_number] = {
            'first_group': first_group,
            'second_group': second_group,
            'timestamp': datetime.now().isoformat()
        }
        
        if len(recent_games) > 100:
            oldest = min(recent_games.keys())
            del recent_games[oldest]

    except Exception as e:
        logger.error(f"Erreur traitement message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# Handlers Telegram
@client.on(events.NewMessage())
async def handle_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id

        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"Message reçu: {message_text[:80]}...")
            
            is_finalized = is_message_finalized(message_text)
            await process_new_message(message_text, chat_id, is_finalized)

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
            await process_new_message(message_text, chat_id, is_finalized)

    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# Commandes
@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    await event.respond("""🤖 **Bot de Prédiction Baccarat - Mode Continu**

**Règles:**
• Prédit **en continu** chaque 2 jeux (impairs)
• **Pas d'attente** entre les prédictions
• Séquence: 51 → 53 → 55 → 57 → 59 → 61...
• Plusieurs prédictions peuvent être actives simultanément

**Commandes:**
• `/status` - Voir les prédictions actives
• `/help` - Aide détaillée
• `/debug` - Informations de débogage
• `/checkchannels` - Vérifier l'accès aux canaux""")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return

    if event.sender_id != ADMIN_ID:
        await event.respond("Commande réservée à l'administrateur")
        return

    status_msg = f"📊 **État des prédictions:**\n\n"
    status_msg += f"🎮 Jeu actuel: #{current_game_number}\n"
    status_msg += f"📐 Décalage: +{PREDICTION_OFFSET} (impairs consécutifs)\n\n"

    if active_predictions:
        status_msg += f"**🔮 Prédictions actives ({len(active_predictions)}):**\n"
        for game_num, pred in sorted(active_predictions.items()):
            status_msg += f"• #{game_num}: {pred['predicted_suit']} - {pred['status']}\n"
    else:
        status_msg += "**🔮 Aucune prédiction active**"

    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/debug'))
async def cmd_debug(event):
    if event.is_group or event.is_channel:
        return

    debug_msg = f"""🔍 **Informations de débogage:**

**Configuration:**
• Source Channel: {SOURCE_CHANNEL_ID}
• Prediction Channel: {PREDICTION_CHANNEL_ID}
• Admin ID: {ADMIN_ID}
• Décalage: +{PREDICTION_OFFSET} (impairs consécutifs)

**Accès aux canaux:**
• Canal source: {'✅ OK' if source_channel_ok else '❌ Non accessible'}
• Canal prédiction: {'✅ OK' if prediction_channel_ok else '❌ Non accessible'}

**État:**
• Jeu actuel: #{current_game_number}
• Prédictions actives: {len(active_predictions)}
• Port: {PORT}

**Mode CONTINU:**
• Prédit chaque 2 jeux SANS attendre
• 51→53→55→57→59→61...
• Vérification en parallèle de toutes les prédictions
"""
    await event.respond(debug_msg)

@client.on(events.NewMessage(pattern='/checkchannels'))
async def cmd_checkchannels(event):
    global source_channel_ok, prediction_channel_ok

    if event.is_group or event.is_channel:
        return

    await event.respond("🔍 Vérification des accès aux canaux...")

    result_msg = "📡 **Résultat de la vérification:**\n\n"

    try:
        source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
        source_title = getattr(source_entity, 'title', 'N/A')
        source_channel_ok = True
        result_msg += f"✅ **Canal source** ({SOURCE_CHANNEL_ID}):\n"
        result_msg += f"   Nom: {source_title}\n\n"
    except Exception as e:
        source_channel_ok = False
        result_msg += f"❌ **Canal source**: {str(e)[:100]}\n\n"

    try:
        pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
        pred_title = getattr(pred_entity, 'title', 'N/A')
        
        test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🔍 Test de connexion...")
        await asyncio.sleep(1)
        await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
        prediction_channel_ok = True
        result_msg += f"✅ **Canal prédiction** ({PREDICTION_CHANNEL_ID}):\n"
        result_msg += f"   Nom: {pred_title}\n"
        result_msg += f"   Écriture: OK"
    except Exception as e:
        prediction_channel_ok = False
        result_msg += f"❌ **Canal prédiction**: {str(e)[:100]}"

    await event.respond(result_msg)

@client.on(events.NewMessage(pattern='/transfert'))
async def cmd_transfert(event):
    global transfer_enabled
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID:
        await event.respond("Commande réservée à l'administrateur")
        return
    transfer_enabled = True
    await event.respond("✅ Transfert activé!")

@client.on(events.NewMessage(pattern='/stoptransfert'))
async def cmd_stoptransfert(event):
    global transfer_enabled
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID:
        await event.respond("Commande réservée à l'administrateur")
        return
    transfer_enabled = False
    await event.respond("⛔ Transfert désactivé.")

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return

    await event.respond(f"""📖 **Aide - Bot de Prédiction (Mode Continu)**

**Logique de prédiction en CONTINU:**

Le bot prédit **sans attendre** la fin des vérifications.

**Exemple de flux:**
Jeu #51 (impair) → 🎰 PRÉDICTION #53 Jeu #53 (impair) → 🎰 PRÉDICTION #55
Jeu #55 (impair) → 🎰 PRÉDICTION #57 Jeu #57 (impair) → 🎰 PRÉDICTION #59 ...
    
**Vérification en parallèle:**
• Quand #53 arrive: vérifie si ♣️ dans G1 → met à jour statut
• Quand #54 arrive: vérifie ♣️ (si pas trouvé) → ✅1️⃣
• Quand #55 arrive: vérifie ♣️ (si pas trouvé) → ✅2️⃣ ou ❌
• En même temps: nouvelle prédiction #57

**Résultat:**
• Plusieurs prédictions actives simultanément
• Aucune attente entre les prédictions
• Vérification continue de toutes les prédictions en cours

**Commandes:**
• `/start` - Démarrer
• `/status` - Voir toutes les prédictions actives
• `/transfert` - Activer transfert
• `/stoptransfert` - Désactiver transfert
• `/checkchannels` - Vérifier canaux
• `/debug` - Infos système""")

# Serveur Web
async def index(request):
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bot Prédiction Baccarat</title>
        <meta charset="utf-8">
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background: #1a1a2e; color: #eee; }}
            h1 {{ color: #00d9ff; }}
            .status {{ background: #16213e; padding: 20px; border-radius: 10px; margin: 20px 0; }}
            .active {{ color: #00ff88; }}
            .inactive {{ color: #ff6b6b; }}
            a {{ color: #00d9ff; }}
        </style>
    </head>
    <body>
        <h1>🎯 Bot de Prédiction Baccarat (Mode Continu)</h1>
        <div class="status">
            <h2>Statut du Bot</h2>
            <p><strong>🎮 Jeu actuel:</strong> #{current_game_number}</p>
            <p><strong>🔮 Prédictions actives:</strong> <span class="{'active' if active_predictions else 'inactive'}">{len(active_predictions)}</span></p>
            <p><strong>📐 Décalage:</strong> +{PREDICTION_OFFSET} (impairs consécutifs)</p>
            <p><strong>📡 Canal Source:</strong> {'✅ Connecté' if source_channel_ok else '❌ Non connecté'}</p>
            <p><strong>🎯 Canal Prédiction:</strong> {'✅ Connecté' if prediction_channel_ok else '❌ Non connecté'}</p>
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
        "source_channel": SOURCE_CHANNEL_ID,
        "source_channel_ok": source_channel_ok,
        "prediction_channel": PREDICTION_CHANNEL_ID,
        "prediction_channel_ok": prediction_channel_ok,
        "current_game": current_game_number,
        "active_predictions_count": len(active_predictions),
        "active_predictions": {k: v['status'] for k, v in active_predictions.items()},
        "prediction_offset": PREDICTION_OFFSET,
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
    logger.info(f"✅ Serveur web démarré sur 0.0.0.0:{PORT}")

async def start_bot():
    global source_channel_ok, prediction_channel_ok
    try:
        logger.info("🚀 Démarrage du bot...")
        await client.start(bot_token=BOT_TOKEN)
        logger.info("✅ Bot Telegram connecté")

        session = client.session.save()
        if session:
            logger.info(f"🔑 Session: {session[:50]}...")

        me = await client.get_me()
        username = getattr(me, 'username', 'Unknown')
        logger.info(f"🤖 Bot opérationnel: @{username}")

        logger.info("🔍 Vérification des canaux...")
        
        try:
            source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
            source_channel_ok = True
            logger.info(f"✅ Canal source: {getattr(source_entity, 'title', 'N/A')}")
        except Exception as e:
            logger.error(f"❌ Canal source inaccessible: {e}")

        try:
            pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
            test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🤖 Bot connecté et prêt!")
            await asyncio.sleep(1)
            await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
            prediction_channel_ok = True
            logger.info(f"✅ Canal prédiction: {getattr(pred_entity, 'title', 'N/A')}")
        except Exception as e:
            logger.error(f"❌ Canal prédiction inaccessible: {e}")

        logger.info(f"📐 Mode CONTINU: +{PREDICTION_OFFSET} (impairs: 51→53→55→57→59→61...)")
        logger.info("👀 Surveillance active du canal source...")

        return True
    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def main():
    try:
        await start_web_server()
        success = await start_bot()
        if not success:
            logger.error("Échec du démarrage, arrêt du bot")
            return
        
        logger.info("🎉 Bot complètement opérationnel en mode CONTINU!")
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"❌ Erreur: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"❌ Erreur fatale: {e}")
        import traceback
        logger.error(traceback.format_exc())
